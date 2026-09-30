"""HikCentral transport over the Artemis OpenAPI: signing, request shape,
record parsing, image decoding, failure policy.

The client's contract is that it NEVER raises into the event path — every
failure degrades to an empty result — so most of these tests assert on what
comes back rather than on an exception.
"""

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings
from app.services.hikcentral import client as hik_client
from app.services.hikcentral.models import VehicleLogRecord

FACILITY_TZ = timezone(timedelta(hours=3))
WIN_BEGIN = datetime(2026, 7, 28, 14, 0, 0, tzinfo=FACILITY_TZ)
WIN_END = datetime(2026, 7, 28, 14, 30, 0, tzinfo=FACILITY_TZ)

APP_KEY = "56519745"
APP_SECRET = "testsecret123"


@pytest.fixture(autouse=True)
def hik_configured(monkeypatch):
    """Give the client OpenAPI credentials without touching a real host."""
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_BASE_URL", "https://hik.test")
    monkeypatch.setattr(settings, "HIK_APP_KEY", APP_KEY)
    monkeypatch.setattr(settings, "HIK_APP_SECRET", APP_SECRET)
    monkeypatch.setattr(settings, "HIK_ENTRY_RESOURCE_IDS", "447")
    monkeypatch.setattr(settings, "HIK_IMAGE_MAX_BYTES", 8 * 1024 * 1024)
    yield
    monkeypatch.setattr(hik_client, "_http_client", None, raising=False)


def _install_transport(monkeypatch, handler):
    """Point the module-level client at an in-process transport."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        hik_client,
        "_http_client",
        httpx.AsyncClient(transport=transport, base_url="https://hik.test"),
        raising=False,
    )


def _expected_signature(request: httpx.Request) -> str:
    """Independently recompute the AK/SK signature from a captured request,
    following OpenAPI guide §3.2 — an oracle for the client's own signing."""
    h = request.headers
    signed_names = h["X-Ca-Signature-Headers"].split(",")
    signed = {n: h[n] for n in signed_names}
    lines = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    sts = (
        f"POST\n{h['Accept']}\n{h['Content-MD5']}\n{h['Content-Type']}\n"
        f"{lines}{request.url.raw_path.decode()}"
    )
    return base64.b64encode(
        hmac.new(APP_SECRET.encode(), sts.encode(), hashlib.sha256).digest()
    ).decode()


_ONE_RECORD = {
    "code": "0", "msg": "Success",
    "data": {"total": 1, "pageNo": 1, "pageSize": 5, "list": [{
        "crossRecordSyscode": "E01DEEC852794D87A58846FFA43F233A",
        "cameraIndexCode": "447", "plateNo": "5625JKA",
        "crossTime": "2026-07-28T14:20:16+03:00",
        "vehiclePicUri": "Vsm://veh", "vehicleDirectionType": 1, "vehicleType": 9,
    }]},
}


# ── Signing ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_is_signed_per_the_openapi_spec(monkeypatch):
    """Every call carries the X-Ca-* headers and a signature matching §3.2."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["req"] = request
        return httpx.Response(200, json=_ONE_RECORD)

    _install_transport(monkeypatch, handler)
    await hik_client.query_vehicle_logs(WIN_BEGIN, WIN_END, "447", 5)

    req = captured["req"]
    assert req.headers["X-Ca-Key"] == APP_KEY
    # Content-MD5 must be Base64(MD5(body)).
    body = req.content
    assert req.headers["Content-MD5"] == base64.b64encode(
        hashlib.md5(body).digest()
    ).decode()
    # The signature must match an independent recomputation.
    assert req.headers["X-Ca-Signature"] == _expected_signature(req)


# ── crossRecords request + parsing ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_sends_crossrecords_body_and_parses_records(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ONE_RECORD)

    _install_transport(monkeypatch, handler)
    records = await hik_client.query_vehicle_logs(WIN_BEGIN, WIN_END, "447", 5)

    assert captured["path"] == "/artemis/api/pms/v1/crossRecords/page"
    body = captured["body"]
    assert body["cameraIndexCode"] == "447"
    assert body["startTime"] == "2026-07-28T14:00:00+03:00"
    assert body["endTime"] == "2026-07-28T14:30:00+03:00"
    assert body["pageSize"] == 5

    assert len(records) == 1
    r = records[0]
    assert r.guid == "E01DEEC852794D87A58846FFA43F233A"
    assert r.canonical_plate == "JKA-5625"  # digits-first -> letters-first
    assert r.pass_time.utcoffset() == timedelta(hours=3)
    assert r.vehicle_image_url == "Vsm://veh"


@pytest.mark.asyncio
async def test_start_end_times_carry_no_fractional_seconds(monkeypatch):
    """The platform rejects microseconds ('startTime parameter error'), and
    facility_now_naive() carries them — so they must be stripped."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": "0", "data": {"list": []}})

    _install_transport(monkeypatch, handler)
    begin = datetime(2026, 7, 28, 14, 0, 0, 123456, tzinfo=FACILITY_TZ)
    end = datetime(2026, 7, 28, 14, 30, 0, 987654, tzinfo=FACILITY_TZ)
    await hik_client.query_vehicle_logs(begin, end, "447", 5)

    assert captured["body"]["startTime"] == "2026-07-28T14:00:00+03:00"
    assert captured["body"]["endTime"] == "2026-07-28T14:30:00+03:00"
    assert "." not in captured["body"]["startTime"]


@pytest.mark.asyncio
async def test_only_the_first_camera_index_code_is_used(monkeypatch):
    """crossRecords takes ONE camera; a stray comma-list uses the first code."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": "0", "data": {"list": []}})

    _install_transport(monkeypatch, handler)
    await hik_client.query_vehicle_logs(WIN_BEGIN, WIN_END, "447,999", 5)
    assert captured["body"]["cameraIndexCode"] == "447"


@pytest.mark.asyncio
async def test_records_outside_the_requested_window_are_dropped(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "0", "data": {"list": [
            {"crossRecordSyscode": "A", "cameraIndexCode": "447",
             "plateNo": "5625JKA", "crossTime": "2026-07-28T14:20:16+03:00"},
            {"crossRecordSyscode": "B", "cameraIndexCode": "447",
             "plateNo": "9640RDJ", "crossTime": "2026-07-28T17:58:22+03:00"},
        ]}})

    _install_transport(monkeypatch, handler)
    records = await hik_client.query_vehicle_logs(WIN_BEGIN, WIN_END, "447", 5)
    assert [r.guid for r in records] == ["A"]


@pytest.mark.asyncio
async def test_non_zero_code_is_not_read_as_no_vehicles(monkeypatch):
    """code 69 (UnAuthorized) must degrade to [], not look like 'no records'."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "69", "msg": "UnAuthorized API"})

    _install_transport(monkeypatch, handler)
    assert await hik_client.query_vehicle_logs(WIN_BEGIN, WIN_END, "447", 5) == []


@pytest.mark.asyncio
async def test_blank_camera_index_code_makes_no_request(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_ONE_RECORD)

    _install_transport(monkeypatch, handler)
    assert await hik_client.query_vehicle_logs(WIN_BEGIN, WIN_END, "", 5) == []
    assert calls == []


# ── Image download / decode ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_picture_decodes_data_uri(monkeypatch):
    jpeg = b"\xff\xd8\xff" + b"body-bytes"
    data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/artemis/api/pms/v1/image"
        assert json.loads(request.content)["picUri"] == "Vsm://veh"
        return httpx.Response(200, content=data_uri.encode(),
                              headers={"content-type": "image/jpeg"})

    _install_transport(monkeypatch, handler)
    assert await hik_client.download_picture("Vsm://veh") == jpeg


@pytest.mark.asyncio
async def test_download_picture_accepts_raw_jpeg_bytes(monkeypatch):
    jpeg = b"\xff\xd8\xff\xe0rawjpeg"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=jpeg,
                              headers={"content-type": "image/jpeg"})

    _install_transport(monkeypatch, handler)
    assert await hik_client.download_picture("Vsm://veh") == jpeg


@pytest.mark.asyncio
async def test_download_picture_rejects_a_json_error_envelope(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "69", "msg": "UnAuthorized API"})

    _install_transport(monkeypatch, handler)
    assert await hik_client.download_picture("Vsm://veh") is None


@pytest.mark.asyncio
async def test_download_picture_enforces_the_size_cap(monkeypatch):
    monkeypatch.setattr(settings, "HIK_IMAGE_MAX_BYTES", 4)
    jpeg = b"\xff\xd8\xff" + b"x" * 100
    data_uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data_uri.encode(),
                              headers={"content-type": "image/jpeg"})

    _install_transport(monkeypatch, handler)
    assert await hik_client.download_picture("Vsm://veh") is None


@pytest.mark.asyncio
async def test_empty_url_downloads_nothing():
    assert await hik_client.download_picture("") is None


# ── Record mapping ──────────────────────────────────────────────────────────


def test_from_openapi_record_maps_the_openapi_field_names():
    rec = VehicleLogRecord.from_openapi_record({
        "crossRecordSyscode": "GUID1", "cameraIndexCode": "453",
        "plateNo": "1372ZZR", "crossTime": "2026-07-28T18:42:25+03:00",
        "vehiclePicUri": "Vsm://v", "vehicleDirectionType": 1, "vehicleType": 15,
    })
    assert rec.guid == "GUID1"
    assert rec.canonical_plate == "ZZR-1372"
    assert rec.resource_id == "453"
    assert rec.vehicle_direction_type == "1"
    assert rec.vehicle_type == "15"
    assert rec.pass_time.utcoffset() == timedelta(hours=3)


def test_record_without_guid_or_time_is_dropped():
    assert VehicleLogRecord.from_openapi_record(
        {"plateNo": "X", "crossTime": "2026-07-28T18:42:25+03:00"}
    ) is None
    assert VehicleLogRecord.from_openapi_record(
        {"crossRecordSyscode": "G", "plateNo": "X"}
    ) is None
