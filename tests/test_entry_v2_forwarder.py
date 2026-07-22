"""Focused tests for transient Entry V2 forwarding and backpressure."""

from datetime import datetime, timezone
import json
import threading

import pytest

from app.config import settings
from app.services.entry_v2_forwarder import (
    ForwardOutcome,
    _request_parts,
    close_entry_v2_http_client,
    forward_entry_v2_event,
    resolve_entry_v2_camera_alias,
    start_entry_v2_http_client,
)
import app.services.entry_v2_forwarder as forwarder
from app.services.event_parser import ParsedCameraEvent, TransientImage


class _Response:
    def __init__(self, status_code: int, text: str = "", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class _Client:
    def __init__(self, response, calls, **_kwargs):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if callable(self.response):
            return self.response(url, kwargs)
        return self.response


def _event(camera_id="CAM-ENTRY", event_type="ANPR"):
    return ParsedCameraEvent(
        camera_id=camera_id,
        device_serial="serial-1",
        channel_id=1,
        event_type=event_type,
        detection_target="vehicle",
        region_id="1" if event_type == "linedetection" else "entry",
        channel_name="Entry",
        trigger_time=datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        raw_xml="<event />",
        event_state="active" if event_type == "linedetection" else None,
        plate_number="ABC-1234" if event_type == "ANPR" else None,
        gate="entry" if event_type == "ANPR" else None,
        crossing_direction="B-to-A" if event_type == "linedetection" else None,
        plate_confidence=93,
        transient_images=(
            TransientImage(b"image-one", "image/jpeg", "one.jpg", role="vehicle"),
            TransientImage(b"image-two", "image/jpeg", "two.jpg", role="vehicle"),
        ),
    )


def _install_client(monkeypatch, response):
    calls = []

    def factory(**kwargs):
        return _Client(response, calls, **kwargs)

    monkeypatch.setattr(
        "app.services.entry_v2_forwarder.httpx.AsyncClient", factory
    )
    return calls


def _semantic_ack(
    *,
    mode="authoritative",
    status="accepted",
    id_override=None,
    duplicate=None,
    omit_mode=False,
    http_status=None,
):
    def response(_url, request):
        data = request["data"]
        evidence_id = data.get("attempt_id") or data["crossing_id"]
        payload = {
            "status": status,
            "id": id_override or evidence_id,
            "duplicate": status == "duplicate" if duplicate is None else duplicate,
        }
        if not omit_mode:
            payload["mode"] = mode
        return _Response(
            http_status or (200 if status == "duplicate" else 201),
            json.dumps(payload),
        )

    return response


@pytest.mark.asyncio
async def test_off_mode_does_not_forward(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "off")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    calls = _install_client(monkeypatch, _Response(201))

    result = await forward_entry_v2_event(_event())

    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_unresolved_gate_fails_closed_without_forwarding(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {})
    calls = _install_client(monkeypatch, _Response(201))
    event = _event(camera_id="UNKNOWN-10.1.20.60")
    event.gate = None

    result = await forward_entry_v2_event(event)

    assert result.outcome is ForwardOutcome.UNAVAILABLE
    assert result.status_code is None
    assert result.retryable is True
    assert calls == []


def test_camera_alias_supports_camera_id_and_device_serial(monkeypatch):
    monkeypatch.setattr(
        settings,
        "CAMERAS",
        {
            "CAM-ENTRY": {"gate": "entry"},
            "CAM-EXIT": {"gate": "exit"},
        },
    )
    monkeypatch.setattr(
        settings,
        "ENTRY_V2_CAMERA_ALIASES",
        "UNKNOWN-10.1.20.60=CAM-ENTRY,exit-serial=CAM-EXIT",
    )
    by_camera = _event(camera_id="UNKNOWN-10.1.20.60")
    by_camera.gate = None
    by_serial = _event(camera_id="UNKNOWN-proxy")
    by_serial.device_serial = "exit-serial"
    by_serial.gate = None

    assert resolve_entry_v2_camera_alias(by_camera) is True
    assert (by_camera.camera_id, by_camera.gate) == ("CAM-ENTRY", "entry")
    assert resolve_entry_v2_camera_alias(by_serial) is True
    assert (by_serial.camera_id, by_serial.gate) == ("CAM-EXIT", "exit")


def test_camera_alias_rejects_non_gate_target(monkeypatch):
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-23": {"gate": "entry"}})
    monkeypatch.setattr(
        settings,
        "ENTRY_V2_CAMERA_ALIASES",
        "UNKNOWN-10.1.20.60=CAM-23",
    )
    event = _event(camera_id="UNKNOWN-10.1.20.60")
    event.gate = None

    assert resolve_entry_v2_camera_alias(event) is False
    assert event.camera_id == "UNKNOWN-10.1.20.60"


@pytest.mark.asyncio
async def test_resolved_exit_is_never_forwarded_as_entry(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-EXIT": {"gate": "exit"}})
    calls = _install_client(monkeypatch, _Response(201))
    event = _event(camera_id="CAM-EXIT")
    event.gate = "exit"

    result = await forward_entry_v2_event(event)

    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_attempt_uses_frozen_multipart_contract_and_stable_id(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    calls = _install_client(monkeypatch, _semantic_ack(mode="shadow"))
    event = _event()

    first = await forward_entry_v2_event(event)
    second = await forward_entry_v2_event(event)

    assert first.outcome is ForwardOutcome.ACCEPTED
    assert first.evidence_id == second.evidence_id
    assert calls[0][0] == "http://va:8000/api/v2/entry-attempts"
    request = calls[0][1]
    assert request["headers"] == {
        "X-Service-Key": "secret",
        "X-Entry-V2-Mode": "shadow",
    }
    assert request["data"]["attempt_id"] == first.evidence_id
    assert request["data"]["reported_plate"] == "ABC-1234"
    assert request["data"]["reported_confidence"] == "93"
    descriptors = json.loads(request["data"]["metadata_json"])["images"]
    assert json.loads(request["data"]["metadata_json"])["image_roles"] == [
        "vehicle",
        "vehicle",
    ]
    assert [item["role"] for item in descriptors] == ["vehicle", "vehicle"]
    assert [part[0] for part in request["files"]] == ["images", "images"]
    assert [part[1][1] for part in request["files"]] == [b"image-one", b"image-two"]


def test_request_metadata_hashes_each_evidence_buffer_once(monkeypatch):
    original_sha256 = forwarder.hashlib.sha256
    observed = {b"<event />": 0, b"image-one": 0, b"image-two": 0}

    def counting_sha256(value=b""):
        if value in observed:
            observed[value] += 1
        return original_sha256(value)

    monkeypatch.setattr(forwarder.hashlib, "sha256", counting_sha256)

    _request_parts(_event())

    assert observed == {b"<event />": 1, b"image-one": 1, b"image-two": 1}


@pytest.mark.asyncio
async def test_request_hashing_runs_off_the_event_loop(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    calls = _install_client(monkeypatch, _semantic_ack(mode="shadow"))
    event_loop_thread = threading.get_ident()
    worker_threads = []
    original_request_parts = forwarder._request_parts

    def request_parts_in_worker(event):
        worker_threads.append(threading.get_ident())
        return original_request_parts(event)

    monkeypatch.setattr(forwarder, "_request_parts", request_parts_in_worker)

    result = await forward_entry_v2_event(_event())

    assert result.outcome is ForwardOutcome.ACCEPTED
    assert calls
    assert worker_threads
    assert worker_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_app_lifecycle_reuses_one_entry_v2_http_client(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    created = []
    calls = []

    class PersistentClient(_Client):
        def __init__(self, **kwargs):
            super().__init__(_semantic_ack(mode="shadow"), calls, **kwargs)
            self.closed = False

        async def aclose(self):
            self.closed = True

    def factory(**kwargs):
        client = PersistentClient(**kwargs)
        created.append(client)
        return client

    await close_entry_v2_http_client()
    monkeypatch.setattr(forwarder.httpx, "AsyncClient", factory)
    try:
        await start_entry_v2_http_client()
        await start_entry_v2_http_client()
        first = await forward_entry_v2_event(_event())
        second = await forward_entry_v2_event(_event())

        assert first.outcome is ForwardOutcome.ACCEPTED
        assert second.outcome is ForwardOutcome.ACCEPTED
        assert len(created) == 1
        assert len(calls) == 2
    finally:
        await close_entry_v2_http_client()

    assert created[0].closed is True


@pytest.mark.asyncio
async def test_crossing_uses_role_and_crossing_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    calls = _install_client(monkeypatch, _semantic_ack(mode="shadow"))

    result = await forward_entry_v2_event(_event("CAM-23", "linedetection"))

    assert result.outcome is ForwardOutcome.ACCEPTED
    assert calls[0][0] == "http://va:8000/api/v2/entry-crossings"
    assert calls[0][1]["data"]["role"] == "primary"
    assert calls[0][1]["data"]["line_id"] == "1"
    assert calls[0][1]["data"]["direction"] == "B-to-A"
    metadata = json.loads(calls[0][1]["data"]["metadata_json"])
    assert metadata["direction_source"] == "camera"


@pytest.mark.asyncio
async def test_cam03_fallback_forwards_raw_hikvision_line_and_direction(monkeypatch):
    """VA must allowlist these raw values alongside its canonical local zone."""
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    calls = _install_client(monkeypatch, _semantic_ack(mode="shadow"))
    event = _event("CAM-03", "linedetection")
    event.region_id = "1"
    event.crossing_direction = "B-to-A"

    result = await forward_entry_v2_event(event)

    assert result.outcome is ForwardOutcome.ACCEPTED
    assert calls[0][1]["data"]["role"] == "fallback"
    assert calls[0][1]["data"]["line_id"] == "1"
    assert calls[0][1]["data"]["direction"] == "B-to-A"
    metadata = json.loads(calls[0][1]["data"]["metadata_json"])
    assert metadata["raw_line_id"] == "1"
    assert metadata["raw_direction"] == "B-to-A"


@pytest.mark.asyncio
async def test_crossing_uses_configured_semantic_direction_when_raw_is_absent(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    monkeypatch.setattr(
        settings,
        "ENTRY_CONFIRM_DIRECTIONS",
        "CAM-23:ramp-entry,CAM-03:B-entry",
    )
    monkeypatch.setattr(settings, "ENTRY_V2_ONE_WAY_LINES", "CAM-23:1")
    calls = _install_client(monkeypatch, _semantic_ack(mode="shadow"))
    event = _event("CAM-23", "linedetection")
    event.crossing_direction = None

    await forward_entry_v2_event(event)

    assert calls[0][1]["data"]["direction"] == "ramp-entry"
    metadata = json.loads(calls[0][1]["data"]["metadata_json"])
    assert metadata["raw_direction"] is None
    assert metadata["direction_source"] == "configured_one_way_line"


@pytest.mark.asyncio
async def test_missing_direction_without_one_way_policy_is_not_forwarded(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "ENTRY_V2_ONE_WAY_LINES", "")
    monkeypatch.setattr(settings, "CAM23_ENTRY_LINE", "1")
    monkeypatch.setattr(settings, "CAM23_ENTRY_DIRECTION", "")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    calls = _install_client(monkeypatch, _Response(201))
    event = _event("CAM-23", "linedetection")
    event.crossing_direction = None

    result = await forward_entry_v2_event(event)

    assert result.outcome is ForwardOutcome.INVALID
    assert result.status_code == 422
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_state", "target"),
    [("inactive", "vehicle"), ("active", "human"), (None, "vehicle")],
)
async def test_only_active_vehicle_crossings_are_forwarded(
    monkeypatch,
    event_state,
    target,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    calls = _install_client(monkeypatch, _Response(201))
    event = _event("CAM-23", "linedetection")
    event.event_state = event_state
    event.detection_target = target

    result = await forward_entry_v2_event(event)

    assert result is None
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [301, 307, 401, 429, 500, 503])
async def test_delivery_and_server_failures_are_retryable(monkeypatch, status_code):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    _install_client(monkeypatch, _Response(status_code, "busy", {"Retry-After": "4"}))

    result = await forward_entry_v2_event(_event())

    assert result.outcome is ForwardOutcome.UNAVAILABLE
    assert result.retryable is True
    assert result.retry_after == "4"


@pytest.mark.asyncio
async def test_deterministic_validation_failure_is_not_retryable(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    _install_client(monkeypatch, _Response(422, "invalid direction"))

    result = await forward_entry_v2_event(_event())

    assert result.outcome is ForwardOutcome.INVALID
    assert result.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize("response_mode", ["off", "shadow", None])
async def test_authoritative_rejects_va_mode_mismatch(monkeypatch, response_mode):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    _install_client(
        monkeypatch,
        _semantic_ack(
            mode=response_mode or "authoritative",
            omit_mode=response_mode is None,
        ),
    )

    result = await forward_entry_v2_event(_event())

    assert result.outcome is ForwardOutcome.UNAVAILABLE
    assert result.retryable is True
    assert "mode mismatch" in result.detail


@pytest.mark.asyncio
async def test_authoritative_accepts_matching_va_mode(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    _install_client(monkeypatch, _semantic_ack())

    result = await forward_entry_v2_event(_event())

    assert result.outcome is ForwardOutcome.ACCEPTED


@pytest.mark.asyncio
@pytest.mark.parametrize("producer_mode", ["off", "shadow", "authoritative"])
@pytest.mark.parametrize("va_mode", ["off", "shadow", "authoritative"])
async def test_mode_matrix_never_accepts_cross_mode_ack(
    monkeypatch,
    producer_mode,
    va_mode,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", producer_mode)
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    calls = _install_client(monkeypatch, _semantic_ack(mode=va_mode))

    result = await forward_entry_v2_event(_event())

    if producer_mode == "off":
        assert result is None
        assert calls == []
    elif producer_mode == va_mode:
        assert result.outcome is ForwardOutcome.ACCEPTED
    else:
        assert result.outcome is ForwardOutcome.UNAVAILABLE
        assert "mode mismatch" in result.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _semantic_ack(id_override="wrong-id"),
        _semantic_ack(status="ok", duplicate=False),
        _semantic_ack(status="duplicate", duplicate=False),
        _semantic_ack(status="duplicate", duplicate=True, http_status=201),
        _Response(201, "not-json"),
    ],
    ids=(
        "wrong-id",
        "wrong-status",
        "duplicate-conflict",
        "http-status-conflict",
        "invalid-json",
    ),
)
async def test_authoritative_rejects_nonsemantic_2xx_ack(monkeypatch, response):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    _install_client(monkeypatch, response)

    result = await forward_entry_v2_event(_event())

    assert result.outcome is ForwardOutcome.UNAVAILABLE
    assert result.retryable is True


@pytest.mark.asyncio
async def test_missing_vehicle_crop_is_rejected_without_network_call(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    calls = _install_client(monkeypatch, _Response(201))
    event = _event()
    event.transient_images = ()

    result = await forward_entry_v2_event(event)

    assert result.outcome is ForwardOutcome.INVALID
    assert result.status_code == 422
    assert result.retryable is False
    assert calls == []


@pytest.mark.asyncio
async def test_outbound_filter_never_sends_non_vehicle_images(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "secret")
    calls = _install_client(monkeypatch, _semantic_ack(mode="shadow"))
    event = _event()
    event.transient_images = (
        TransientImage(b"raw-overview", "image/jpeg", "overview.jpg"),
        TransientImage(b"plate", "image/jpeg", "plate.jpg", role="plate"),
        TransientImage(b"vehicle", "image/jpeg", "vehicle.jpg", role="vehicle"),
    )

    result = await forward_entry_v2_event(event)

    assert result.outcome is ForwardOutcome.ACCEPTED
    assert [part[1][1] for part in calls[0][1]["files"]] == [b"vehicle"]
    metadata = json.loads(calls[0][1]["data"]["metadata_json"])
    assert [item["role"] for item in metadata["images"]] == ["vehicle"]
