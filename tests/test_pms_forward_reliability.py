# tests/test_pms_forward_reliability.py
"""B2 — reliable VA delivery: single-attempt tri-state + durable spool + drain.

The old forward was fire-and-forget with no retry, so whenever VA was briefly
unreachable the identity image was lost permanently (the "some images didn't
arrive to VA" symptom). The new path:

  * ``_deliver_anpr_payload`` does ONE attempt and returns "ok" / "drop" (4xx,
    never retryable) / "retry" (connect error / 5xx) — no inline retry loop, so
    the webhook that awaits it never blocks for seconds when VA is down.
  * ``notify_pms_anpr`` spools the payload to disk on "retry", discards on
    "drop", nothing on "ok".
  * ``drain_pms_forward_spool`` re-POSTs oldest-first, removing on "ok"/"drop"
    (a bad payload never wedges the queue) and stopping only on "retry"; stale
    payloads are dropped without delivery.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import tempfile
from datetime import datetime, timedelta

import httpx
import pytest
from unittest.mock import patch

import app.utils.core_backend_client as cbc


# ── Fake httpx client ─────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


class _FakeClient:
    """One-shot async context-manager client: its single ``post`` returns the
    configured response or raises the configured exception."""

    def __init__(self, outcome):
        self._outcome = outcome

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _client_factory(*outcomes):
    """Return an ``httpx.AsyncClient`` replacement that yields one _FakeClient
    per call, walking through ``outcomes`` in order."""
    it = iter(outcomes)

    def factory(*args, **kwargs):
        return _FakeClient(next(it))

    return factory


def _settings(tmp, *, max_age=3600.0, api="http://va:8000"):
    s = type("S", (), {})()
    s.PMS_API_URL = api
    s.PMS_FORWARD_SPOOL_DIR = tmp
    s.PMS_FORWARD_SPOOL_MAX_AGE_SECONDS = max_age
    return s


def _spool_files(d):
    return [f for f in os.listdir(d) if f.endswith(".json")]


# ── _deliver_anpr_payload tri-state ───────────────────────────────────────────

class TestDeliverTriState:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status,expected", [(200, "ok"), (201, "ok")])
    async def test_2xx_is_ok(self, status, expected):
        with patch.object(cbc, "settings", _settings("/x")), \
             patch.object(cbc.httpx, "AsyncClient", _client_factory(_FakeResp(status))):
            assert await cbc._deliver_anpr_payload({"plate": "A"}) == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 404, 422])
    async def test_4xx_is_drop(self, status):
        with patch.object(cbc, "settings", _settings("/x")), \
             patch.object(cbc.httpx, "AsyncClient", _client_factory(_FakeResp(status, "bad"))):
            assert await cbc._deliver_anpr_payload({"plate": "A"}) == "drop"

    @pytest.mark.asyncio
    async def test_5xx_is_retry(self):
        with patch.object(cbc, "settings", _settings("/x")), \
             patch.object(cbc.httpx, "AsyncClient", _client_factory(_FakeResp(503, "down"))):
            assert await cbc._deliver_anpr_payload({"plate": "A"}) == "retry"

    @pytest.mark.asyncio
    async def test_connect_error_is_retry(self):
        with patch.object(cbc, "settings", _settings("/x")), \
             patch.object(cbc.httpx, "AsyncClient",
                          _client_factory(httpx.ConnectError("refused"))):
            assert await cbc._deliver_anpr_payload({"plate": "A"}) == "retry"

    @pytest.mark.asyncio
    async def test_generic_exception_is_retry(self):
        with patch.object(cbc, "settings", _settings("/x")), \
             patch.object(cbc.httpx, "AsyncClient",
                          _client_factory(RuntimeError("boom"))):
            assert await cbc._deliver_anpr_payload({"plate": "A"}) == "retry"


# ── notify_pms_anpr: spool on retry, discard on drop, nothing on ok ───────────

class TestNotifySpoolPolicy:
    @pytest.mark.asyncio
    async def test_retry_spools_the_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cbc, "settings", _settings(tmp)), \
                 patch.object(cbc.httpx, "AsyncClient",
                              _client_factory(httpx.ConnectError("down"))):
                await cbc.notify_pms_anpr("DJS-7842", "entry", image_path=None)
            files = _spool_files(tmp)
            assert len(files) == 1
            body = json.load(open(os.path.join(tmp, files[0]), encoding="utf-8"))
            assert body["plate"] == "DJS-7842"
            assert body["direction"] == "entry"
            assert "_spooled_at" in body

    @pytest.mark.asyncio
    async def test_drop_does_not_spool(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cbc, "settings", _settings(tmp)), \
                 patch.object(cbc.httpx, "AsyncClient",
                              _client_factory(_FakeResp(400, "bad"))):
                await cbc.notify_pms_anpr("DJS-7842", "entry", image_path=None)
            assert _spool_files(tmp) == []

    @pytest.mark.asyncio
    async def test_ok_does_not_spool(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cbc, "settings", _settings(tmp)), \
                 patch.object(cbc.httpx, "AsyncClient",
                              _client_factory(_FakeResp(200))):
                await cbc.notify_pms_anpr("DJS-7842", "entry", image_path=None)
            assert _spool_files(tmp) == []


# ── drain_pms_forward_spool ───────────────────────────────────────────────────

def _write_spool(d, plate, spooled_at=None):
    os.makedirs(d, exist_ok=True)
    rec = {"plate": plate, "direction": "entry", "image_base64": ""}
    rec["_spooled_at"] = (spooled_at or datetime.now()).isoformat()
    # timestamped name so sorted() == oldest-first
    fname = f"anpr_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{plate}.json"
    with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


class TestDrain:
    @pytest.mark.asyncio
    async def test_all_delivered_are_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_spool(tmp, "AAA")
            _write_spool(tmp, "BBB")
            with patch.object(cbc, "settings", _settings(tmp)), \
                 patch.object(cbc.httpx, "AsyncClient",
                              _client_factory(_FakeResp(200), _FakeResp(201))):
                await cbc.drain_pms_forward_spool()
            assert _spool_files(tmp) == []

    @pytest.mark.asyncio
    async def test_retry_stops_and_leaves_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_spool(tmp, "AAA")
            _write_spool(tmp, "BBB")
            # First delivers, second says VA is down → drain stops, one remains.
            with patch.object(cbc, "settings", _settings(tmp)), \
                 patch.object(cbc.httpx, "AsyncClient",
                              _client_factory(_FakeResp(200),
                                              httpx.ConnectError("down"))):
                await cbc.drain_pms_forward_spool()
            assert len(_spool_files(tmp)) == 1

    @pytest.mark.asyncio
    async def test_poison_4xx_is_removed_not_wedged(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_spool(tmp, "AAA")  # oldest — VA rejects permanently
            _write_spool(tmp, "BBB")  # must still be delivered behind it
            with patch.object(cbc, "settings", _settings(tmp)), \
                 patch.object(cbc.httpx, "AsyncClient",
                              _client_factory(_FakeResp(400, "bad"), _FakeResp(200))):
                await cbc.drain_pms_forward_spool()
            assert _spool_files(tmp) == []

    @pytest.mark.asyncio
    async def test_stale_payload_dropped_without_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_spool(tmp, "OLD",
                         spooled_at=datetime.now() - timedelta(seconds=99999))
            # No outcome supplied: if the drain tried to POST it, next() would
            # raise StopIteration and fail the test — proving it never delivered.
            with patch.object(cbc, "settings", _settings(tmp, max_age=3600.0)), \
                 patch.object(cbc.httpx, "AsyncClient", _client_factory()):
                await cbc.drain_pms_forward_spool()
            assert _spool_files(tmp) == []
