# tests/test_pms_forward_reliability.py
"""B2 — reliable VA delivery: single-attempt tri-state + durable spool + drain.

The old forward was fire-and-forget with no retry, so whenever VA was briefly
unreachable the identity image was lost permanently (the "some images didn't
arrive to VA" symptom). The new path:

  * ``_deliver_anpr_payload`` does ONE attempt and returns "ok" / "drop"
    (deterministic payload errors) / "retry" (transport, server, active-V2
    boundary, or semantic acknowledgement errors) — no inline retry loop.
  * ``notify_pms_anpr`` spools the payload to disk on "retry", discards on
    "drop", nothing on "ok".
  * ``drain_pms_forward_spool`` re-POSTs oldest-first, removing on "ok"/"drop"
    (a bad payload never wedges the queue) and stopping only on "retry"; stale
    legacy payloads are dropped, active exits never age out, malformed JSON is
    quarantined, and filesystem work stays off the event loop.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from unittest.mock import AsyncMock, patch

from app.config import Settings
import app.utils.core_backend_client as cbc


# ── Fake httpx client ─────────────────────────────────────────────────────────

_NO_JSON = object()


class _FakeResp:
    def __init__(self, status, text="", json_body=_NO_JSON):
        self.status_code = status
        self._json_body = json_body
        self.text = (
            json.dumps(json_body)
            if json_body is not _NO_JSON and not text
            else text
        )

    def json(self):
        if self._json_body is _NO_JSON:
            raise ValueError("response has no JSON body")
        return self._json_body


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


def _settings(tmp, *, max_age=3600.0, api="http://va:8000", mode="off"):
    return Settings(
        _env_file=None,
        PMS_API_URL=api,
        PMS_FORWARD_SPOOL_DIR=tmp,
        PMS_FORWARD_SPOOL_MAX_AGE_SECONDS=max_age,
        ENTRY_V2_MODE=mode,
        ENTRY_V2_SERVICE_KEY="pms-va-secret",
        CAMERA_EVENT_ALLOWED_SOURCE_CIDRS="127.0.0.1/32",
        CAM23_ENTRY_LINE="park-entry",
    )


def _spool_files(d):
    return [f for f in os.listdir(d) if f.endswith(".json")]


def _exit_ack(plate="DJS-7842", direction="exit", timestamp=None):
    payload = {
        "status": "ok",
        "plate": plate,
        "direction": direction,
        "image_saved": False,
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp
    return payload


# ── _deliver_anpr_payload tri-state ───────────────────────────────────────────

class TestDeliverTriState:
    @pytest.mark.asyncio
    async def test_app_lifecycle_reuses_one_legacy_va_client(self):
        created = []
        calls = []

        class PersistentClient:
            def __init__(self, *args, **kwargs):
                self.closed = False

            async def post(self, *args, **kwargs):
                calls.append((args, kwargs))
                return _FakeResp(200)

            async def aclose(self):
                self.closed = True

        def factory(*args, **kwargs):
            client = PersistentClient(*args, **kwargs)
            created.append(client)
            return client

        await cbc.close_core_backend_http_client()
        with patch.object(cbc, "settings", _settings("/x")), patch.object(
            cbc.httpx,
            "AsyncClient",
            factory,
        ):
            try:
                await cbc.start_core_backend_http_client()
                await cbc.start_core_backend_http_client()
                assert await cbc._deliver_anpr_payload({"plate": "A"}) == "ok"
                assert await cbc._deliver_anpr_payload({"plate": "B"}) == "ok"
            finally:
                await cbc.close_core_backend_http_client()

        assert len(created) == 1
        assert len(calls) == 2
        assert created[0].closed is True

    @pytest.mark.asyncio
    async def test_request_carries_entry_service_key(self):
        captured = {}

        class CapturingClient(_FakeClient):
            async def post(self, *args, **kwargs):
                captured.update(kwargs)
                return await super().post(*args, **kwargs)

        with patch.object(cbc, "settings", _settings("/x")), patch.object(
            cbc.httpx,
            "AsyncClient",
            lambda *args, **kwargs: CapturingClient(_FakeResp(200)),
        ):
            assert await cbc._deliver_anpr_payload({"plate": "A"}) == "ok"

        assert captured["headers"]["X-Service-Key"] == "pms-va-secret"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status,expected", [(200, "ok"), (201, "ok")])
    async def test_legacy_2xx_is_ok(self, status, expected):
        with patch.object(cbc, "settings", _settings("/x")), \
             patch.object(cbc.httpx, "AsyncClient", _client_factory(_FakeResp(status))):
            assert await cbc._deliver_anpr_payload({"plate": "A"}) == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["shadow", "authoritative"])
    async def test_active_v2_exit_requires_semantic_json_ack(self, mode):
        body = {
            "plate": "DJS-7842",
            "direction": "exit",
            "captured_at": "2026-07-21T09:00:00+03:00",
        }
        with patch.object(cbc, "settings", _settings("/x", mode=mode)), patch.object(
            cbc.httpx,
            "AsyncClient",
            _client_factory(_FakeResp(200, "not-json")),
        ):
            assert await cbc._deliver_anpr_payload(body) == "retry"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [200, 201])
    async def test_active_v2_exit_accepts_matching_semantic_ack(self, status):
        source = "2026-07-21T09:00:00.123456+03:00"
        body = {
            "plate": "9444HUD",
            "direction": "EXIT",
            "captured_at": source,
        }
        response = _FakeResp(
            status,
            json_body=_exit_ack(
                plate="HUD-9444",
                direction="exit",
                timestamp="2026-07-21T06:00:00.123456+00:00",
            ),
        )
        with patch.object(
            cbc,
            "settings",
            _settings("/x", mode="authoritative"),
        ), patch.object(
            cbc.httpx,
            "AsyncClient",
            _client_factory(response),
        ):
            assert await cbc._deliver_anpr_payload(body) == "ok"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response_body",
        [
            {"status": "accepted", "plate": "DJS-7842", "direction": "exit"},
            {"status": "ok", "plate": "DJS-7842", "direction": "exit"},
            {"status": "ok", "plate": "WRONG-9999", "direction": "exit"},
            {"status": "ok", "plate": "DJS-7842", "direction": "entry"},
            {
                "status": "ok",
                "plate": "DJS-7842",
                "direction": "exit",
                "timestamp": "2026-07-21T09:00:01+03:00",
            },
        ],
    )
    async def test_active_v2_exit_mismatched_ack_is_retryable(self, response_body):
        body = {
            "plate": "DJS-7842",
            "direction": "exit",
            "captured_at": "2026-07-21T09:00:00+03:00",
        }
        with patch.object(
            cbc,
            "settings",
            _settings("/x", mode="authoritative"),
        ), patch.object(
            cbc.httpx,
            "AsyncClient",
            _client_factory(_FakeResp(200, json_body=response_body)),
        ):
            assert await cbc._deliver_anpr_payload(body) == "retry"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 404, 422])
    async def test_legacy_mode_4xx_is_drop(self, status):
        with patch.object(cbc, "settings", _settings("/x")), \
             patch.object(cbc.httpx, "AsyncClient", _client_factory(_FakeResp(status, "bad"))):
            assert await cbc._deliver_anpr_payload({"plate": "A"}) == "drop"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["shadow", "authoritative"])
    @pytest.mark.parametrize("status", [401, 403, 404, 405, 408, 425, 429])
    async def test_v2_boundary_4xx_is_retry(self, mode, status):
        with patch.object(cbc, "settings", _settings("/x", mode=mode)), \
             patch.object(cbc.httpx, "AsyncClient", _client_factory(_FakeResp(status, "bad"))):
            assert await cbc._deliver_anpr_payload({"plate": "A"}) == "retry"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["shadow", "authoritative"])
    @pytest.mark.parametrize("status", [400, 413, 422])
    async def test_v2_payload_4xx_is_drop(self, mode, status):
        with patch.object(cbc, "settings", _settings("/x", mode=mode)), \
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
    def test_spool_write_fsyncs_file_and_directory_before_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                cbc,
                "settings",
                _settings(tmp, mode="authoritative"),
            ), patch.object(cbc.os, "fsync", wraps=os.fsync) as fsync:
                assert cbc._spool_payload(
                    {
                        "plate": "DJS-7842",
                        "direction": "exit",
                        "captured_at": "2026-07-21T09:00:00+03:00",
                    }
                )

            assert fsync.call_count == 2
            assert len(_spool_files(tmp)) == 1
            assert not any(name.endswith(".tmp") for name in os.listdir(tmp))

    def test_failed_file_fsync_is_not_reported_as_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                cbc,
                "settings",
                _settings(tmp, mode="authoritative"),
            ), patch.object(
                cbc.os,
                "fsync",
                side_effect=OSError("fsync failed"),
            ):
                assert not cbc._spool_payload(
                    {
                        "plate": "DJS-7842",
                        "direction": "exit",
                        "captured_at": "2026-07-21T09:00:00+03:00",
                    }
                )

            assert _spool_files(tmp) == []
            assert not any(name.endswith(".tmp") for name in os.listdir(tmp))

    @pytest.mark.asyncio
    async def test_local_image_read_and_base64_run_off_event_loop(self):
        event_loop_thread = threading.get_ident()
        worker_threads = []
        original = cbc._encode_local_image

        def observed_encode(path):
            worker_threads.append(threading.get_ident())
            return original(path)

        with tempfile.TemporaryDirectory() as tmp:
            image_path = os.path.join(tmp, "exit.jpg")
            with open(image_path, "wb") as image_file:
                image_file.write(b"exit-pixels")
            deliver = AsyncMock(return_value="ok")
            with patch.object(cbc, "settings", _settings(tmp)), patch.object(
                cbc,
                "_encode_local_image",
                observed_encode,
            ), patch.object(cbc, "_deliver_anpr_payload", deliver):
                await cbc.notify_pms_anpr(
                    "DJS-7842",
                    "exit",
                    image_path=image_path,
                )

        assert worker_threads
        assert worker_threads[0] != event_loop_thread
        assert deliver.await_args.args[0]["image_base64"]

    @pytest.mark.asyncio
    async def test_spool_write_runs_off_event_loop(self):
        event_loop_thread = threading.get_ident()
        worker_threads = []

        def observed_spool(_body):
            worker_threads.append(threading.get_ident())
            return True

        with patch.object(cbc, "settings", _settings("/x")), patch.object(
            cbc,
            "_deliver_anpr_payload",
            AsyncMock(return_value="retry"),
        ), patch.object(cbc, "_spool_payload", observed_spool):
            await cbc.notify_pms_anpr("DJS-7842", "exit")

        assert worker_threads
        assert worker_threads[0] != event_loop_thread

    @pytest.mark.asyncio
    async def test_active_v2_exit_raises_when_delivery_and_spool_both_fail(self):
        captured_at = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)
        with patch.object(
            cbc,
            "settings",
            _settings("/unwritable", mode="authoritative"),
        ), patch.object(
            cbc,
            "_deliver_anpr_payload",
            AsyncMock(return_value="retry"),
        ), patch.object(cbc, "_spool_payload", return_value=False):
            with pytest.raises(cbc.AnprForwardUnavailable, match="spool write"):
                await cbc.notify_pms_anpr(
                    "DJS-7842",
                    "exit",
                    captured_at=captured_at,
                )

    @pytest.mark.asyncio
    async def test_inline_exit_sends_exact_source_timestamp(self):
        captured = {}
        captured_at = datetime(
            2026,
            7,
            21,
            9,
            0,
            0,
            123456,
            tzinfo=timezone(timedelta(hours=3)),
        )

        class CapturingClient(_FakeClient):
            async def post(self, *args, **kwargs):
                captured.update(kwargs)
                return await super().post(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                cbc,
                "settings",
                _settings(tmp, mode="authoritative"),
            ), patch.object(
                cbc.httpx,
                "AsyncClient",
                lambda *args, **kwargs: CapturingClient(
                    _FakeResp(
                        200,
                        json_body=_exit_ack(
                            timestamp="2026-07-21T06:00:00.123456+00:00"
                        ),
                    )
                ),
            ):
                await cbc.notify_pms_anpr(
                    "DJS-7842",
                    "exit",
                    image_path=None,
                    captured_at=captured_at,
                )

            assert _spool_files(tmp) == []
        assert captured["json"]["captured_at"] == (
            "2026-07-21T09:00:00.123456+03:00"
        )

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
    async def test_retry_spool_preserves_exit_source_timestamp(self):
        captured_at = datetime(2026, 7, 21, 9, 0, tzinfo=timezone(timedelta(hours=3)))
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(cbc, "settings", _settings(tmp)), \
                 patch.object(cbc.httpx, "AsyncClient",
                              _client_factory(httpx.ConnectError("down"))):
                await cbc.notify_pms_anpr(
                    "DJS-7842",
                    "exit",
                    image_path=None,
                    captured_at=captured_at,
                )

            files = _spool_files(tmp)
            assert len(files) == 1
            with open(os.path.join(tmp, files[0]), encoding="utf-8") as spool_file:
                body = json.load(spool_file)
            assert body["captured_at"] == "2026-07-21T09:00:00+03:00"

    @pytest.mark.asyncio
    async def test_authoritative_wrong_service_key_exit_is_spooled(self):
        captured_at = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)

        class AuthRejectingClient(_FakeClient):
            async def post(self, *args, **kwargs):
                assert kwargs["headers"]["X-Service-Key"] == "wrong-key"
                return _FakeResp(401, "invalid service key")

        with tempfile.TemporaryDirectory() as tmp:
            authoritative_settings = _settings(tmp, mode="authoritative")
            authoritative_settings.ENTRY_V2_SERVICE_KEY = "wrong-key"
            image_path = os.path.join(tmp, "exit.jpg")
            with open(image_path, "wb") as image_file:
                image_file.write(b"exit-pixels")
            with patch.object(cbc, "settings", authoritative_settings), patch.object(
                cbc.httpx,
                "AsyncClient",
                lambda *args, **kwargs: AuthRejectingClient(_FakeResp(401)),
            ):
                await cbc.notify_pms_anpr(
                    "DJS-7842",
                    "exit",
                    image_path=image_path,
                    captured_at=captured_at,
                )

            files = _spool_files(tmp)
            assert len(files) == 1
            with open(os.path.join(tmp, files[0]), encoding="utf-8") as spool_file:
                body = json.load(spool_file)
            assert body["plate"] == "DJS-7842"
            assert body["direction"] == "exit"
            assert body["captured_at"] == "2026-07-21T09:00:00+00:00"
            assert body["image_base64"] == ""

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

def _write_spool(
    d,
    plate,
    spooled_at=None,
    direction="entry",
    captured_at=None,
):
    os.makedirs(d, exist_ok=True)
    rec = {"plate": plate, "direction": direction, "image_base64": ""}
    if captured_at is not None:
        rec["captured_at"] = captured_at
    rec["_spooled_at"] = (spooled_at or datetime.now()).isoformat()
    # timestamped name so sorted() == oldest-first
    fname = f"anpr_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{plate}.json"
    with open(os.path.join(d, fname), "w", encoding="utf-8") as fh:
        json.dump(rec, fh)


class TestDrain:
    @pytest.mark.asyncio
    async def test_transient_spool_read_error_retains_active_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = "2026-07-21T09:00:00+03:00"
            _write_spool(
                tmp,
                "DJS-7842",
                direction="exit",
                captured_at=source,
            )
            original_file = os.path.join(tmp, _spool_files(tmp)[0])
            deliver = AsyncMock(return_value="ok")
            with patch.object(
                cbc,
                "settings",
                _settings(tmp, mode="authoritative"),
            ), patch.object(
                cbc,
                "_read_spool_record",
                side_effect=PermissionError("volume temporarily unavailable"),
            ), patch.object(cbc, "_deliver_anpr_payload", deliver), patch.object(
                cbc.logger,
                "warning",
            ) as warning:
                await cbc.drain_pms_forward_spool()

            assert os.path.exists(original_file)
            assert _spool_files(tmp) == [os.path.basename(original_file)]
            deliver.assert_not_awaited()
            assert any(
                "retained for retry" in str(call.args[0])
                for call in warning.call_args_list
            )

    @pytest.mark.asyncio
    async def test_malformed_spool_is_atomically_quarantined_and_visible(self):
        event_loop_thread = threading.get_ident()
        move_threads = []
        original_quarantine = cbc._quarantine_spool_file

        def observed_quarantine(path):
            move_threads.append(threading.get_ident())
            return original_quarantine(path)

        with tempfile.TemporaryDirectory() as tmp:
            malformed_path = os.path.join(tmp, "anpr_bad.json")
            malformed_bytes = b'{"plate":"DJS-7842","direction":"exit"'
            with open(malformed_path, "wb") as malformed_file:
                malformed_file.write(malformed_bytes)

            with patch.object(
                cbc,
                "settings",
                _settings(tmp, mode="authoritative"),
            ), patch.object(
                cbc,
                "_quarantine_spool_file",
                observed_quarantine,
            ), patch.object(cbc.logger, "error") as error:
                await cbc.drain_pms_forward_spool()

            assert not os.path.exists(malformed_path)
            quarantined = [name for name in os.listdir(tmp) if name.endswith(".corrupt")]
            assert len(quarantined) == 1
            with open(os.path.join(tmp, quarantined[0]), "rb") as quarantined_file:
                assert quarantined_file.read() == malformed_bytes
            assert move_threads and move_threads[0] != event_loop_thread
            assert any(
                "Quarantined malformed forward spool" in str(call.args[0])
                for call in error.call_args_list
            )

    @pytest.mark.asyncio
    async def test_scan_read_and_remove_run_off_event_loop(self):
        event_loop_thread = threading.get_ident()
        worker_threads = {"scan": [], "read": [], "remove": []}
        original_scan = cbc._list_spool_paths
        original_read = cbc._read_spool_record
        original_remove = cbc._remove_spool_file

        def observed_scan(directory):
            worker_threads["scan"].append(threading.get_ident())
            return original_scan(directory)

        def observed_read(path):
            worker_threads["read"].append(threading.get_ident())
            return original_read(path)

        def observed_remove(path):
            worker_threads["remove"].append(threading.get_ident())
            return original_remove(path)

        with tempfile.TemporaryDirectory() as tmp:
            _write_spool(tmp, "DJS-7842", direction="entry")
            with patch.object(cbc, "settings", _settings(tmp)), patch.object(
                cbc,
                "_list_spool_paths",
                observed_scan,
            ), patch.object(
                cbc,
                "_read_spool_record",
                observed_read,
            ), patch.object(
                cbc,
                "_remove_spool_file",
                observed_remove,
            ), patch.object(
                cbc,
                "_deliver_anpr_payload",
                AsyncMock(return_value="ok"),
            ):
                await cbc.drain_pms_forward_spool()

            assert _spool_files(tmp) == []
            assert all(worker_threads.values())
            assert all(
                thread_id != event_loop_thread
                for observed in worker_threads.values()
                for thread_id in observed
            )

    @pytest.mark.asyncio
    async def test_malformed_active_exit_2xx_ack_keeps_spool_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_spool(
                tmp,
                "DJS-7842",
                direction="exit",
                captured_at="2026-07-21T09:00:00+03:00",
            )
            with patch.object(
                cbc,
                "settings",
                _settings(tmp, mode="authoritative"),
            ), patch.object(
                cbc.httpx,
                "AsyncClient",
                _client_factory(_FakeResp(200, "gateway fallback page")),
            ):
                await cbc.drain_pms_forward_spool()

            assert len(_spool_files(tmp)) == 1

    @pytest.mark.asyncio
    async def test_quarantined_spool_warning_is_emitted_once_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_spool(tmp, "ENTRY", direction="entry")
            cbc._reported_quarantined_spools.clear()
            with patch.object(
                cbc,
                "settings",
                _settings(tmp, mode="authoritative"),
            ), patch.object(cbc.logger, "warning") as warning:
                await cbc.drain_pms_forward_spool()
                await cbc.drain_pms_forward_spool()

            quarantine_warnings = [
                call
                for call in warning.call_args_list
                if "Quarantined legacy entry spool" in str(call.args[0])
            ]
            assert len(quarantine_warnings) == 1
            assert len(_spool_files(tmp)) == 1
            cbc._reported_quarantined_spools.clear()

    @pytest.mark.asyncio
    async def test_repeated_spool_retry_warning_is_suppressed_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_spool(tmp, "RETRY", direction="exit")
            deliver = AsyncMock(return_value="retry")
            cbc._reported_retrying_spools.clear()
            with patch.object(cbc, "settings", _settings(tmp)), patch.object(
                cbc,
                "_deliver_anpr_payload",
                deliver,
            ), patch.object(cbc.logger, "warning") as warning:
                await cbc.drain_pms_forward_spool()
                await cbc.drain_pms_forward_spool()

            assert deliver.await_count == 2
            retry_warnings = [
                call
                for call in warning.call_args_list
                if "future identical warnings are suppressed" in str(call.args[0])
            ]
            assert len(retry_warnings) == 1
            assert len(_spool_files(tmp)) == 1
            cbc._reported_retrying_spools.clear()

    @pytest.mark.asyncio
    async def test_replay_preserves_original_exit_source_timestamp_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = "2026-07-21T09:00:00.123456+03:00"
            _write_spool(
                tmp,
                "EXIT",
                direction="exit",
                captured_at=source,
            )
            deliver = AsyncMock(return_value="ok")
            with patch.object(cbc, "settings", _settings(tmp)), \
                 patch.object(cbc, "_deliver_anpr_payload", deliver):
                await cbc.drain_pms_forward_spool()

            deliver.assert_awaited_once()
            assert deliver.await_args.args[0]["captured_at"] == source
            assert _spool_files(tmp) == []

    @pytest.mark.asyncio
    async def test_authoritative_quarantines_entry_but_still_drains_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = "2026-07-21T09:00:00+03:00"
            _write_spool(tmp, "ENTRY", direction="entry")
            _write_spool(
                tmp,
                "EXIT",
                direction="exit",
                captured_at=source,
            )
            with patch.object(cbc, "settings", _settings(tmp, mode="authoritative")), \
                 patch.object(
                     cbc.httpx,
                     "AsyncClient",
                     _client_factory(
                         _FakeResp(
                             200,
                             json_body=_exit_ack(plate="EXIT", timestamp=source),
                         )
                     ),
                 ):
                await cbc.drain_pms_forward_spool()

            remaining = _spool_files(tmp)
            assert len(remaining) == 1
            with open(
                os.path.join(tmp, remaining[0]),
                encoding="utf-8",
            ) as spool_file:
                quarantined = json.load(spool_file)
            assert quarantined["plate"] == "ENTRY"
            assert quarantined["direction"] == "entry"

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

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["shadow", "authoritative"])
    async def test_active_v2_exit_never_ages_out(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            source = "2026-07-21T09:00:00+03:00"
            _write_spool(
                tmp,
                "OLD-EXIT",
                spooled_at=datetime.now() - timedelta(days=7),
                direction="exit",
                captured_at=source,
            )
            deliver = AsyncMock(return_value="ok")
            with patch.object(
                cbc,
                "settings",
                _settings(tmp, max_age=3600.0, mode=mode),
            ), patch.object(cbc, "_deliver_anpr_payload", deliver):
                await cbc.drain_pms_forward_spool()

            deliver.assert_awaited_once()
            assert deliver.await_args.args[0]["captured_at"] == source
            assert _spool_files(tmp) == []
