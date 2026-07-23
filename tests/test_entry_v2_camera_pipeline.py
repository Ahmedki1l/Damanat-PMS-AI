"""Camera-boundary tests for Entry V2 transient and retry behavior."""

import asyncio
from datetime import datetime, timezone
from io import BytesIO
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.config import settings
from app.routers.events import receive_camera_event
import app.services.event_dispatcher as dispatcher
from app.services.event_dispatcher import dispatch_event
from app.services.entry_v2_forwarder import (
    ForwardOutcome,
    ForwardResult,
    _request_parts,
    entry_v2_shadow_status,
    enqueue_entry_v2_shadow,
    is_entry_crossing,
    start_entry_v2_shadow_worker,
    stop_entry_v2_shadow_worker,
)
from app.services.entry_state_lock import EntryStateLockUnavailable
from app.services.entry_exit_service import (
    AnprPostCommitForward,
    SourceTimestampUnavailable,
)
from app.services.event_parser import (
    ParsedCameraEvent,
    TransientImage,
    parse_camera_event,
)


class _Request:
    def __init__(
        self,
        body=b"event",
        *,
        content_type="application/xml",
        client_host="10.0.0.99",
        include_content_length=True,
    ):
        self._body = body
        self.stream_started = False
        self.client = SimpleNamespace(host=client_host)
        self.headers = {"content-type": content_type}
        if include_content_length:
            self.headers["content-length"] = str(len(body))

    async def stream(self):
        self.stream_started = True
        yield self._body


def _event():
    return ParsedCameraEvent(
        camera_id="CAM-ENTRY",
        device_serial="serial-1",
        channel_id=1,
        event_type="ANPR",
        detection_target="vehicle",
        region_id="entry",
        channel_name="Entry",
        trigger_time=datetime(2026, 7, 20, 12, 0),
        raw_xml="<event />",
        plate_number="ABC-1234",
        gate="entry",
    )


def _jpeg(width: int, height: int, color=(60, 120, 180)) -> bytes:
    output = BytesIO()
    with Image.new("RGB", (width, height), color) as image:
        image.save(output, format="JPEG")
    return output.getvalue()


def _noisy_jpeg(width: int, height: int, *, quality: int) -> bytes:
    """Create a poorly-compressible camera frame without external fixtures."""
    output = BytesIO()
    with Image.effect_noise((width, height), 100) as noise:
        with noise.convert("RGB") as image:
            image.save(output, format="JPEG", quality=quality, optimize=False)
    return output.getvalue()


def _anpr_multipart(
    image: bytes,
    *,
    boundary: str = "entry-test",
    timestamp_xml: bytes = b"",
) -> bytes:
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.10</ipAddress><channelID>1</channelID>
      <eventType>ANPR</eventType><ANPR><licensePlate>ABC-1234</licensePlate>
        <pictureInfoList><pictureInfo><vehicelRect>
          <X>20</X><Y>20</Y><width>40</width><height>30</height>
        </vehicelRect></pictureInfo></pictureInfoList>
      </ANPR>
    </EventNotificationAlert>"""
    if timestamp_xml:
        xml = xml.replace(
            b"<eventType>ANPR</eventType>",
            b"<eventType>ANPR</eventType>" + timestamp_xml,
        )
    return (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="FW1"; filename="detectionPicture.jpg"\r\n\r\n'
        + image
        + f"\r\n--{boundary}--\r\n".encode()
    )


@pytest.mark.parametrize(
    ("timestamp_xml", "expected_source"),
    [
        (b"", "pms_receive_missing"),
        (b"<dateTime>broken</dateTime>", "pms_receive_invalid"),
        (
            b"<dateTime>2026-07-21T12:00:00</dateTime>",
            "camera_assumed_facility_timezone",
        ),
    ],
)
def test_entry_v2_multipart_timestamp_fallback_is_aware_and_auditable(
    monkeypatch,
    tmp_path,
    timestamp_xml,
    expected_source,
):
    _configure_authoritative_entry_parser(monkeypatch, tmp_path)
    boundary = "timestamp-fallback"
    body = _anpr_multipart(
        _jpeg(100, 80),
        boundary=boundary,
        timestamp_xml=timestamp_xml,
    )
    event = parse_camera_event(
        body,
        "10.0.0.10",
        f"multipart/form-data; boundary={boundary}",
    )

    _, data, _ = _request_parts(event)
    captured_at = datetime.fromisoformat(data["captured_at"])
    metadata = json.loads(data["metadata_json"])

    assert captured_at.tzinfo is not None
    assert captured_at.utcoffset() is not None
    assert event.trigger_time_source == expected_source
    assert metadata["timestamp_source"] == expected_source
    if expected_source.startswith("pms_receive_"):
        retry_event = parse_camera_event(
            body,
            "10.0.0.10",
            f"multipart/form-data; boundary={boundary}",
        )
        _, retry_data, _ = _request_parts(retry_event)
        assert retry_data["attempt_id"] == data["attempt_id"]
        assert retry_data["source_event_id"] == data["source_event_id"]


def _configure_authoritative_entry_parser(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.10": "CAM-ENTRY"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-ENTRY": {"gate": "entry"}})
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))


@pytest.mark.asyncio
async def test_camera_source_allowlist_accepts_configured_cidr(monkeypatch):
    monkeypatch.setattr(
        settings,
        "CAMERA_EVENT_ALLOWED_SOURCE_CIDRS",
        "10.0.0.0/24",
    )
    request = _Request(client_host="10.0.0.99")

    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        response = await receive_camera_event(request, MagicMock())

    assert response == {"status": "ok", "event_type": "ANPR"}
    assert request.stream_started is True


@pytest.mark.asyncio
async def test_camera_source_allowlist_rejects_before_reading_body(monkeypatch):
    monkeypatch.setattr(
        settings,
        "CAMERA_EVENT_ALLOWED_SOURCE_CIDRS",
        "10.1.20.0/24,192.168.1.104",
    )
    request = _Request(client_host="203.0.113.9")

    with patch("app.routers.events.parse_camera_event") as parse:
        response = await receive_camera_event(request, MagicMock())

    assert response.status_code == 403
    assert request.stream_started is False
    parse.assert_not_called()


@pytest.mark.asyncio
async def test_authoritative_camera_source_allowlist_cannot_be_empty(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_EVENT_ALLOWED_SOURCE_CIDRS", "")
    request = _Request(client_host="10.0.0.99")

    with patch("app.routers.events.parse_camera_event") as parse:
        response = await receive_camera_event(request, MagicMock())

    assert response.status_code == 403
    assert request.stream_started is False
    parse.assert_not_called()


@pytest.mark.asyncio
async def test_declared_oversized_camera_body_is_rejected_before_stream(monkeypatch):
    monkeypatch.setattr(settings, "CAMERA_EVENT_MAX_BODY_BYTES", 4)
    request = _Request(body=b"12345")

    with patch("app.routers.events.parse_camera_event") as parse:
        response = await receive_camera_event(request, MagicMock())

    assert response.status_code == 413
    assert request.stream_started is False
    parse.assert_not_called()


@pytest.mark.asyncio
async def test_chunked_oversized_camera_body_is_bounded_while_streaming(monkeypatch):
    monkeypatch.setattr(settings, "CAMERA_EVENT_MAX_BODY_BYTES", 4)
    request = _Request(body=b"12345", include_content_length=False)

    with patch("app.routers.events.parse_camera_event") as parse:
        response = await receive_camera_event(request, MagicMock())

    assert response.status_code == 413
    assert request.stream_started is True
    parse.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"<EventNotificationAlert>", "application/xml"),
        (b'{"eventType":', "application/json"),
        (b"[]", "application/json"),
        (b'{"eventType": []}', "application/json"),
        (
            b'{"eventType":"ANPR","AccessControllerEvent":[]}',
            "application/json",
        ),
        (
            b'{"eventType":"ANPR","VehicleMatchResult":{"PlateInfo":[]}}',
            "application/json",
        ),
        (b"not-a-multipart-body", "multipart/form-data"),
        (
            b"--bad\r\nContent-Type: image/jpeg\r\n\r\nimage\r\n--bad--\r\n",
            "multipart/form-data; boundary=bad",
        ),
    ],
    ids=(
        "xml",
        "json-syntax",
        "json-root",
        "json-scalar-field",
        "json-nested-field",
        "json-plate-info",
        "missing-boundary",
        "multipart-without-metadata",
    ),
)
async def test_authoritative_malformed_camera_payload_is_terminally_acked(
    monkeypatch,
    body,
    content_type,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    db = MagicMock()
    with patch("app.routers.events.dispatch_event", new_callable=AsyncMock) as dispatch:
        response = await receive_camera_event(
            _Request(body=body, content_type=content_type),
            db,
        )

    assert response == {
        "status": "rejected",
        "detail": "malformed camera payload",
    }
    dispatch.assert_not_awaited()
    db.commit.assert_not_called()
    db.rollback.assert_not_called()


@pytest.mark.asyncio
async def test_authoritative_unexpected_parser_failure_remains_retryable(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    db = MagicMock()
    with patch(
        "app.routers.events.parse_camera_event",
        side_effect=RuntimeError("worker unavailable"),
    ):
        response = await receive_camera_event(_Request(), db)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert json.loads(response.body)["detail"] == (
        "camera event processing unavailable"
    )


@pytest.mark.asyncio
async def test_authoritative_unexpected_parser_value_error_remains_retryable(
    monkeypatch,
    tmp_path,
):
    _configure_authoritative_entry_parser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings,
        "CAMERA_EVENT_ALLOWED_SOURCE_CIDRS",
        "10.0.0.0/24",
    )
    body = _anpr_multipart(_jpeg(100, 80))
    db = MagicMock()
    with (
        patch(
            "app.services.event_parser._prepare_v2_vehicle_images",
            side_effect=ValueError("internal regression"),
        ),
        patch("app.routers.events.dispatch_event", new_callable=AsyncMock) as dispatch,
    ):
        response = await receive_camera_event(
            _Request(
                body=body,
                content_type="multipart/form-data; boundary=entry-test",
                client_host="10.0.0.10",
            ),
            db,
        )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    dispatch.assert_not_awaited()
    db.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_camera_parse_and_crop_work_runs_off_the_event_loop(monkeypatch):
    event_loop_thread = threading.get_ident()
    parser_threads = []

    def parse_in_worker(*_args):
        parser_threads.append(threading.get_ident())
        return _event()

    with (
        patch("app.routers.events.parse_camera_event", side_effect=parse_in_worker),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        response = await receive_camera_event(_Request(), MagicMock())

    assert response == {"status": "ok", "event_type": "ANPR"}
    assert parser_threads
    assert parser_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_authoritative_backpressure_returns_camera_503(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    db = MagicMock()
    result = ForwardResult(
        outcome=ForwardOutcome.UNAVAILABLE,
        evidence_id="attempt-1",
        status_code=503,
        retry_after="3",
    )
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch("app.routers.events.dispatch_event", new_callable=AsyncMock) as dispatch,
    ):
        response = await receive_camera_event(_Request(), db)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "3"
    dispatch.assert_not_awaited()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_authoritative_invalid_evidence_is_terminally_acked(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    db = MagicMock()
    result = ForwardResult(
        outcome=ForwardOutcome.INVALID,
        evidence_id="crossing-1",
        status_code=422,
        detail="no valid vehicle crop",
    )
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ) as dispatch,
    ):
        response = await receive_camera_event(_Request(), db)

    assert response == {"status": "ok", "event_type": "ANPR"}
    dispatch.assert_awaited_once()
    db.commit.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_order"),
    [
        ("off", ["dispatch", "commit"]),
        ("shadow", ["dispatch", "commit", "enqueue"]),
        ("authoritative", ["v2", "dispatch", "commit"]),
    ],
)
async def test_entry_v2_mode_preserves_legacy_ordering_contract(
    monkeypatch,
    mode,
    expected_order,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", mode)
    order = []
    db = MagicMock()
    db.commit.side_effect = lambda: order.append("commit")

    async def forward_v2(_event):
        order.append("v2")
        return None

    async def dispatch_legacy(_event, _db):
        order.append("dispatch")
        return {}

    def enqueue_shadow(_event):
        order.append("enqueue")
        return True

    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            side_effect=forward_v2,
        ) as v2,
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            side_effect=dispatch_legacy,
        ),
        patch(
            "app.routers.events.enqueue_entry_v2_shadow",
            side_effect=enqueue_shadow,
        ) as enqueue,
    ):
        response = await receive_camera_event(_Request(), db)

    assert response == {"status": "ok", "event_type": "ANPR"}
    assert order == expected_order
    if mode == "authoritative":
        v2.assert_awaited_once()
        enqueue.assert_not_called()
    elif mode == "shadow":
        v2.assert_not_awaited()
        enqueue.assert_called_once()
    else:
        v2.assert_not_awaited()
        enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_uses_prelegacy_evidence_after_all_legacy_work(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    parsed_event = _event()
    order = []
    forwarded_events = []
    db = MagicMock()
    db.commit.side_effect = lambda: order.append("commit")
    post_commit_forward = SimpleNamespace(
        deliver=AsyncMock(side_effect=lambda: order.append("deliver"))
    )

    async def dispatch_legacy(event, _db):
        order.append("dispatch")
        # Legacy FIFO rescue is allowed to mutate its own event. Shadow must
        # still receive the pre-legacy camera/plate evidence.
        event.camera_id = "CAM-LEGACY-RESCUED"
        event.plate_number = "LEGACY-9999"
        return {
            "occupancy_cache_keys": [("CAM-ENTRY", "ANPR", "entry")],
            "anpr_forwards": [post_commit_forward],
        }

    def enqueue_shadow(event):
        order.append("enqueue")
        forwarded_events.append(event)
        return True

    with (
        patch("app.routers.events.parse_camera_event", return_value=parsed_event),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            side_effect=dispatch_legacy,
        ),
        patch(
            "app.routers.events.record_event_in_cache",
            side_effect=lambda _key: order.append("cache"),
        ),
        patch(
            "app.routers.events.enqueue_entry_v2_shadow",
            side_effect=enqueue_shadow,
        ),
    ):
        response = await receive_camera_event(_Request(), db)

    assert response == {"status": "ok", "event_type": "ANPR"}
    assert order == ["dispatch", "commit", "cache", "deliver", "enqueue"]
    assert len(forwarded_events) == 1
    assert forwarded_events[0] is not parsed_event
    assert forwarded_events[0].camera_id == "CAM-ENTRY"
    assert forwarded_events[0].plate_number == "ABC-1234"
    assert parsed_event.camera_id == "CAM-LEGACY-RESCUED"
    assert parsed_event.plate_number == "LEGACY-9999"


@pytest.mark.asyncio
async def test_shadow_backpressure_preserves_legacy_response(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    db = MagicMock()
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ) as dispatch,
        patch(
            "app.routers.events.enqueue_entry_v2_shadow",
            return_value=False,
        ) as enqueue,
    ):
        response = await receive_camera_event(_Request(), db)

    assert response == {"status": "ok", "event_type": "ANPR"}
    dispatch.assert_awaited_once()
    db.commit.assert_called_once_with()
    enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_exit_forward_runs_only_after_camera_transaction_commit(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    order = []
    db = MagicMock()
    db.commit.side_effect = lambda: order.append("commit")
    forward = SimpleNamespace(
        deliver=AsyncMock(side_effect=lambda: order.append("deliver"))
    )
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={"anpr_forwards": [forward]},
        ),
        patch(
            "app.routers.events.enqueue_entry_v2_shadow",
            side_effect=lambda _event: order.append("enqueue"),
        ),
    ):
        response = await receive_camera_event(_Request(), db)

    assert response == {"status": "ok", "event_type": "ANPR"}
    assert order == ["commit", "deliver", "enqueue"]


@pytest.mark.asyncio
async def test_entry_state_lock_contention_returns_camera_retry(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    db = MagicMock()
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            side_effect=EntryStateLockUnavailable("busy"),
        ),
    ):
        response = await receive_camera_event(_Request(), db)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert db.commit.call_count == 0
    db.rollback.assert_called()


@pytest.mark.asyncio
async def test_missing_exit_source_time_returns_camera_retry(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    db = MagicMock()
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            side_effect=SourceTimestampUnavailable("missing"),
        ),
    ):
        response = await receive_camera_event(_Request(), db)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert json.loads(response.body)["detail"] == "exit source time unavailable"
    db.rollback.assert_called()


@pytest.mark.asyncio
async def test_authoritative_unexpected_dispatch_failure_returns_camera_retry(
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    db = MagicMock()
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("database unavailable"),
        ),
    ):
        response = await receive_camera_event(_Request(), db)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert json.loads(response.body)["detail"] == (
        "camera event processing unavailable"
    )
    db.commit.assert_not_called()
    db.rollback.assert_called_once()


@pytest.mark.asyncio
async def test_shadow_unexpected_dispatch_failure_preserves_legacy_ack(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    db = MagicMock()
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.enqueue_entry_v2_shadow",
        ) as enqueue,
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            side_effect=RuntimeError("legacy failure"),
        ),
    ):
        response = await receive_camera_event(_Request(), db)

    assert response == {"status": "error", "detail": "legacy failure"}
    db.rollback.assert_called_once()
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_commit_failure_does_not_forward_uncommitted_evidence(
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    db = MagicMock()
    db.commit.side_effect = RuntimeError("commit failed")
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.enqueue_entry_v2_shadow",
        ) as enqueue,
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        response = await receive_camera_event(_Request(), db)

    assert response == {"status": "error", "detail": "commit failed"}
    db.rollback.assert_called_once()
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_queue_drop_cannot_change_committed_legacy_success(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    db = MagicMock()
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.enqueue_entry_v2_shadow",
            return_value=False,
        ) as enqueue,
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ) as dispatch,
    ):
        response = await receive_camera_event(_Request(), db)

    assert response == {"status": "ok", "event_type": "ANPR"}
    dispatch.assert_awaited_once()
    db.commit.assert_called_once()
    enqueue.assert_called_once()


@pytest.mark.asyncio
async def test_shadow_webhook_ack_does_not_wait_for_va(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "ENTRY_V2_SHADOW_QUEUE_CAPACITY", 2)
    monkeypatch.setattr(
        settings,
        "ENTRY_V2_SHADOW_SHUTDOWN_TIMEOUT_SECONDS",
        1.0,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_forward(_event):
        started.set()
        await release.wait()
        return ForwardResult(
            outcome=ForwardOutcome.UNAVAILABLE,
            evidence_id="attempt-delayed",
            status_code=503,
        )

    await stop_entry_v2_shadow_worker()
    baseline = entry_v2_shadow_status()
    await start_entry_v2_shadow_worker()
    try:
        with (
            patch(
                "app.services.entry_v2_forwarder.forward_entry_v2_event",
                new_callable=AsyncMock,
                side_effect=blocked_forward,
            ),
            patch("app.routers.events.parse_camera_event", return_value=_event()),
            patch(
                "app.routers.events.dispatch_event",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            response = await asyncio.wait_for(
                receive_camera_event(_Request(), MagicMock()),
                timeout=1.0,
            )
            assert response == {"status": "ok", "event_type": "ANPR"}
            await asyncio.wait_for(started.wait(), timeout=1.0)
            assert not release.is_set()
    finally:
        release.set()
        await stop_entry_v2_shadow_worker()

    status = entry_v2_shadow_status()
    assert status["enqueued"] == baseline["enqueued"] + 1
    assert status["failed"] == baseline["failed"] + 1


@pytest.mark.asyncio
async def test_shadow_fifo_is_bounded_and_drops_only_shadow(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "ENTRY_V2_SHADOW_QUEUE_CAPACITY", 1)
    monkeypatch.setattr(
        settings,
        "ENTRY_V2_SHADOW_SHUTDOWN_TIMEOUT_SECONDS",
        1.0,
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    delivered = []
    active = 0
    max_active = 0

    first = _event()
    first.plate_number = "FIRST"
    second = _event()
    second.plate_number = "SECOND"
    overflow = _event()
    overflow.plate_number = "OVERFLOW"

    async def ordered_forward(event):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            delivered.append(event.plate_number)
            if event.plate_number == "FIRST":
                first_started.set()
                await release_first.wait()
            return None
        finally:
            active -= 1

    await stop_entry_v2_shadow_worker()
    baseline = entry_v2_shadow_status()
    await start_entry_v2_shadow_worker()
    try:
        with patch(
            "app.services.entry_v2_forwarder.forward_entry_v2_event",
            new_callable=AsyncMock,
            side_effect=ordered_forward,
        ):
            assert enqueue_entry_v2_shadow(first) is True
            await asyncio.wait_for(first_started.wait(), timeout=1.0)
            assert enqueue_entry_v2_shadow(second) is True
            assert enqueue_entry_v2_shadow(overflow) is False
            release_first.set()
            await stop_entry_v2_shadow_worker()
    finally:
        release_first.set()
        await stop_entry_v2_shadow_worker()

    status = entry_v2_shadow_status()
    assert delivered == ["FIRST", "SECOND"]
    assert max_active == 1
    assert status["enqueued"] == baseline["enqueued"] + 2
    assert status["dropped"] == baseline["dropped"] + 1


@pytest.mark.asyncio
async def test_shadow_worker_survives_one_forwarding_exception(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "ENTRY_V2_SHADOW_QUEUE_CAPACITY", 2)
    monkeypatch.setattr(
        settings,
        "ENTRY_V2_SHADOW_SHUTDOWN_TIMEOUT_SECONDS",
        1.0,
    )
    attempts = []
    first = _event()
    first.plate_number = "FAIL"
    second = _event()
    second.plate_number = "PASS"

    async def fail_once(event):
        attempts.append(event.plate_number)
        if event.plate_number == "FAIL":
            raise RuntimeError("VA client regression")
        return None

    await stop_entry_v2_shadow_worker()
    baseline = entry_v2_shadow_status()
    await start_entry_v2_shadow_worker()
    try:
        with patch(
            "app.services.entry_v2_forwarder.forward_entry_v2_event",
            new_callable=AsyncMock,
            side_effect=fail_once,
        ):
            assert enqueue_entry_v2_shadow(first) is True
            assert enqueue_entry_v2_shadow(second) is True
            await stop_entry_v2_shadow_worker()
    finally:
        await stop_entry_v2_shadow_worker()

    status = entry_v2_shadow_status()
    assert attempts == ["FAIL", "PASS"]
    assert status["failed"] == baseline["failed"] + 1
    assert status["completed"] == baseline["completed"] + 1


@pytest.mark.asyncio
async def test_shadow_shutdown_timeout_releases_queued_events(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(settings, "ENTRY_V2_SHADOW_QUEUE_CAPACITY", 1)
    monkeypatch.setattr(
        settings,
        "ENTRY_V2_SHADOW_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
    )
    started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_forward(_event):
        started.set()
        await never_release.wait()
        return None

    await stop_entry_v2_shadow_worker()
    baseline = entry_v2_shadow_status()
    await start_entry_v2_shadow_worker()
    with patch(
        "app.services.entry_v2_forwarder.forward_entry_v2_event",
        new_callable=AsyncMock,
        side_effect=blocked_forward,
    ):
        assert enqueue_entry_v2_shadow(_event()) is True
        await asyncio.wait_for(started.wait(), timeout=1.0)
        assert enqueue_entry_v2_shadow(_event()) is True
        await stop_entry_v2_shadow_worker()

    status = entry_v2_shadow_status()
    assert status["worker_alive"] is False
    assert status["queue_depth"] == 0
    # One in-flight event is cancelled and one queued event is discarded.
    assert status["dropped"] == baseline["dropped"] + 2


@pytest.mark.asyncio
async def test_authoritative_commit_failure_returns_camera_retry(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    db = MagicMock()
    db.commit.side_effect = RuntimeError("commit failed")
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        response = await receive_camera_event(_Request(), db)

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert json.loads(response.body)["detail"] == (
        "camera event processing unavailable"
    )
    db.commit.assert_called_once()
    db.rollback.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("shadow", 200), ("authoritative", 503)],
)
async def test_exit_delivery_and_spool_failure_respects_mode_retry_contract(
    monkeypatch,
    mode,
    expected_status,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", mode)
    monkeypatch.setattr(settings, "PMS_API_URL", "http://va:8000")
    forward = AnprPostCommitForward(
        plate="ABC-1234",
        direction="exit",
        image_path=None,
        captured_at=datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc),
    )
    db = MagicMock()
    with (
        patch("app.routers.events.parse_camera_event", return_value=_event()),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={"anpr_forwards": [forward]},
        ),
        patch(
            "app.utils.core_backend_client._deliver_anpr_payload",
            new_callable=AsyncMock,
            return_value="retry",
        ),
        patch(
            "app.utils.core_backend_client._spool_payload",
            return_value=False,
        ),
    ):
        response = await receive_camera_event(_Request(), db)

    if expected_status == 200:
        assert response == {"status": "ok", "event_type": "ANPR"}
    else:
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert json.loads(response.body)["detail"] == (
            "camera event processing unavailable"
        )
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_exact_vmr_camera_identity_is_resolved_before_v2_forward(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    dispatcher._VMR_GATE_HINTS.clear()
    dispatcher._VMR_RECENT.clear()
    dispatcher._VMR_GATE_HINTS["ABC-1234"] = (
        "CAM-ENTRY",
        "entry",
        float("inf"),
    )
    event = _event()
    event.camera_id = "UNKNOWN-10.1.20.60"
    event.gate = None
    accepted = ForwardResult(
        outcome=ForwardOutcome.ACCEPTED,
        evidence_id="attempt-1",
        status_code=201,
    )

    with (
        patch("app.routers.events.parse_camera_event", return_value=event),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            return_value=accepted,
        ) as forward,
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        await receive_camera_event(_Request(), MagicMock())

    assert forward.await_args.args[0].camera_id == "CAM-ENTRY"
    assert forward.await_args.args[0].gate == "entry"
    dispatcher._VMR_GATE_HINTS.clear()


@pytest.mark.asyncio
async def test_exact_exit_vmr_identity_is_not_forwarded_as_entry(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-EXIT": {"gate": "exit"}})
    dispatcher._VMR_GATE_HINTS.clear()
    dispatcher._VMR_RECENT.clear()
    dispatcher._VMR_GATE_HINTS["ABC-1234"] = (
        "CAM-EXIT",
        "exit",
        float("inf"),
    )
    event = _event()
    event.camera_id = "UNKNOWN-10.1.20.60"
    event.gate = None

    with (
        patch("app.routers.events.parse_camera_event", return_value=event),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch("app.services.entry_v2_forwarder.httpx.AsyncClient") as http_client,
    ):
        await receive_camera_event(_Request(), MagicMock())

    assert event.camera_id == "CAM-EXIT"
    assert event.gate == "exit"
    http_client.assert_not_called()
    dispatcher._VMR_GATE_HINTS.clear()


@pytest.mark.asyncio
async def test_trusted_entry_alias_forwards_even_when_vmr_plate_disagrees(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-ENTRY": {"gate": "entry"}})
    monkeypatch.setattr(
        settings,
        "ENTRY_V2_CAMERA_ALIASES",
        "UNKNOWN-10.1.20.60=CAM-ENTRY",
    )
    dispatcher._VMR_GATE_HINTS.clear()
    dispatcher._VMR_RECENT.clear()
    dispatcher._VMR_GATE_HINTS["CORRECT-123"] = (
        "CAM-ENTRY",
        "entry",
        float("inf"),
    )
    event = _event()
    event.camera_id = "UNKNOWN-10.1.20.60"
    event.gate = None
    event.plate_number = "MISREAD-999"
    accepted = ForwardResult(
        outcome=ForwardOutcome.ACCEPTED,
        evidence_id="attempt-1",
        status_code=201,
    )

    with (
        patch("app.routers.events.parse_camera_event", return_value=event),
        patch(
            "app.routers.events.forward_entry_v2_event",
            new_callable=AsyncMock,
            return_value=accepted,
        ) as forward,
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ),
    ):
        await receive_camera_event(_Request(), MagicMock())

    forwarded = forward.await_args.args[0]
    assert (forwarded.camera_id, forwarded.gate) == ("CAM-ENTRY", "entry")
    assert forwarded.plate_number == "MISREAD-999"
    dispatcher._VMR_GATE_HINTS.clear()


@pytest.mark.asyncio
async def test_trusted_exit_alias_preserves_inline_snapshot_and_never_forwards(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-EXIT": {"gate": "exit"}})
    monkeypatch.setattr(
        settings,
        "ENTRY_V2_CAMERA_ALIASES",
        "exit-serial=CAM-EXIT",
    )
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))
    event = _event()
    event.camera_id = "UNKNOWN-10.1.20.60"
    event.device_serial = "exit-serial"
    event.gate = None
    event.pending_legacy_image = TransientImage(
        data=b"inline-exit-image",
        content_type="image/jpeg",
        filename="exit.jpg",
    )
    event.transient_images = (
        TransientImage(
            data=b"derived-vehicle-crop",
            content_type="image/jpeg",
            filename="vehicle.jpg",
            role="vehicle",
        ),
    )

    with (
        patch("app.routers.events.parse_camera_event", return_value=event),
        patch(
            "app.routers.events.dispatch_event",
            new_callable=AsyncMock,
            return_value={},
        ) as dispatch,
        patch("app.services.entry_v2_forwarder.httpx.AsyncClient") as http_client,
    ):
        await receive_camera_event(_Request(), MagicMock())

    assert (event.camera_id, event.gate) == ("CAM-EXIT", "exit")
    assert event.transient_images == ()
    assert event.local_snapshot_path is not None
    assert (tmp_path / event.local_snapshot_path.rsplit("/", 1)[-1]).read_bytes() == (
        b"inline-exit-image"
    )
    dispatch.assert_awaited_once()
    http_client.assert_not_called()


@pytest.mark.parametrize("quoted_boundary", [False, True])
def test_authoritative_entry_multipart_stays_in_memory(
    tmp_path,
    monkeypatch,
    quoted_boundary,
):
    boundary = "entry-boundary"
    xml = b"""<?xml version="1.0" encoding="utf-8"?>
    <EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.10</ipAddress>
      <deviceSerial>entry-serial</deviceSerial>
      <channelID>1</channelID>
      <eventType>ANPR</eventType>
      <dateTime>2026-07-20T12:00:00+03:00</dateTime>
      <ANPR>
        <licensePlate>ABC-1234</licensePlate>
        <pictureInfoList><pictureInfo>
          <fileName>detectionPicture.jpg</fileName>
          <type>detectionPicture</type>
          <vehicelRect><X>20</X><Y>20</Y><width>40</width><height>30</height></vehicelRect>
        </pictureInfo></pictureInfoList>
      </ANPR>
    </EventNotificationAlert>"""
    image = _jpeg(100, 80)
    plate_image = _jpeg(20, 10)
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="FW1"; filename="detectionPicture.jpg"\r\n'
        + b"Content-ID: detectionPicture\r\n\r\n"
        + image
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="FW2"; filename="licensePlatePicture.jpg"\r\n'
        + b"Content-ID: licensePlatePicture\r\n\r\n"
        + plate_image
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.10": "CAM-ENTRY"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {"entry-serial": "CAM-ENTRY"})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-ENTRY": {"gate": "entry"}})
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    event = parse_camera_event(
        body,
        "10.0.0.10",
        (
            f'multipart/form-data; boundary="{boundary}"'
            if quoted_boundary
            else f"multipart/form-data; boundary={boundary}"
        ),
    )

    assert event.snapshot_path is None
    assert event.local_snapshot_path is None
    assert len(event.transient_images) == 1
    assert event.transient_images[0].role == "vehicle"
    assert event.transient_images[0].source_name == "detectionpicture.jpg"
    assert event.transient_images[0].data != image
    with Image.open(BytesIO(event.transient_images[0].data)) as crop:
        assert crop.size == (50, 38)
    assert list(tmp_path.iterdir()) == []


def test_five_vehicle_parts_are_deterministically_bounded_to_va_contract(
    tmp_path,
    monkeypatch,
):
    boundary = "five-vehicle-parts"
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.10</ipAddress><channelID>1</channelID>
      <eventType>ANPR</eventType><dateTime>2026-07-21T12:00:00+03:00</dateTime>
      <ANPR><licensePlate>ABC-1234</licensePlate></ANPR>
    </EventNotificationAlert>"""
    image = _jpeg(40, 30)
    body = f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode() + xml
    for index in range(1, 6):
        body += (
            f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
            + (
                'Content-Disposition: form-data; name="FW"; '
                f'filename="vehiclePicture{index}.jpg"\r\n\r\n'
            ).encode()
            + image
        )
    body += f"\r\n--{boundary}--\r\n".encode()
    _configure_authoritative_entry_parser(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "ENTRY_V2_MAX_IMAGES", 4)

    event = parse_camera_event(
        body,
        "10.0.0.10",
        f"multipart/form-data; boundary={boundary}",
    )
    _, _, files = _request_parts(event)

    assert len(event.transient_images) == 4
    assert len(files) == 4
    assert [image.source_name for image in event.transient_images] == [
        f"vehiclepicture{index}.jpg" for index in range(1, 5)
    ]
    assert list(tmp_path.iterdir()) == []


def test_anpr_picture_info_rectangles_match_exact_multipart_images(
    tmp_path,
    monkeypatch,
):
    boundary = "mapped-picture-info"
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.10</ipAddress><channelID>1</channelID>
      <eventType>ANPR</eventType><eventState>active</eventState>
      <ANPR><licensePlate>ABC-1234</licensePlate><pictureInfoList>
        <pictureInfo><fileName>detectionPictureA.jpg</fileName>
          <vehicelRect><X>10</X><Y>10</Y><width>20</width><height>10</height></vehicelRect>
        </pictureInfo>
        <pictureInfo><fileName>detectionPictureB.jpg</fileName>
          <vehicelRect><X>50</X><Y>40</Y><width>10</width><height>30</height></vehicelRect>
        </pictureInfo>
      </pictureInfoList></ANPR>
    </EventNotificationAlert>"""
    image = _jpeg(100, 100)
    body = f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode() + xml
    for filename in (
        "detectionPictureB.jpg",
        "detectionPictureA.jpg",
        "detectionPictureUnknown.jpg",
    ):
        body += (
            f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
            + (
                'Content-Disposition: form-data; name="FW"; '
                f'filename="{filename}"\r\n\r\n'
            ).encode()
            + image
        )
    body += f"\r\n--{boundary}--\r\n".encode()
    _configure_authoritative_entry_parser(monkeypatch, tmp_path)

    event = parse_camera_event(
        body,
        "10.0.0.10",
        f"multipart/form-data; boundary={boundary}",
    )

    assert [image.source_name for image in event.transient_images] == [
        "detectionpictureb.jpg",
        "detectionpicturea.jpg",
    ]
    crop_sizes = []
    for transient in event.transient_images:
        with Image.open(BytesIO(transient.data)) as crop:
            crop_sizes.append(crop.size)
    assert crop_sizes == [(14, 38), (26, 14)]
    assert list(tmp_path.iterdir()) == []


def test_authoritative_crossing_crops_normalized_target_rect(tmp_path, monkeypatch):
    boundary = "line-boundary"
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.23</ipAddress><channelID>1</channelID>
      <eventType>linedetection</eventType><eventState>active</eventState>
      <dateTime>2026-07-20T12:00:02+03:00</dateTime>
      <DetectionRegionList><DetectionRegionEntry>
        <regionID>1</regionID><detectionTarget>vehicle</detectionTarget>
        <TargetRect><X>0.8</X><Y>0.7</Y><width>0.4</width><height>0.4</height></TargetRect>
      </DetectionRegionEntry></DetectionRegionList>
      <direction>B-to-A</direction>
    </EventNotificationAlert>"""
    image = _jpeg(100, 100)
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="lineCrossImage"; filename="line.jpg"\r\n'
        + b"Content-ID: lineCrossImage\r\n\r\n"
        + image
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.23": "CAM-23"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-23": {}})
    monkeypatch.setattr(settings, "ENTRY_CONFIRM_CAMERAS", "CAM-23,CAM-03")
    monkeypatch.setattr(settings, "CAM23_ENTRY_LINE", "1")
    monkeypatch.setattr(settings, "CAM23_ENTRY_DIRECTION", "B-to-A")
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    event = parse_camera_event(
        body,
        "10.0.0.23",
        f"multipart/form-data; boundary={boundary}",
    )

    assert len(event.transient_images) == 1
    assert event.transient_images[0].role == "vehicle"
    with Image.open(BytesIO(event.transient_images[0].data)) as crop:
        assert crop.size == (25, 35)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("mode", "expects_vehicle_image"),
    [("shadow", True), ("authoritative", False)],
)
def test_empty_cam23_filters_are_calibration_only(
    tmp_path,
    monkeypatch,
    mode,
    expects_vehicle_image,
):
    boundary = "cam23-empty-filter"
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.23</ipAddress><channelID>1</channelID>
      <eventType>linedetection</eventType><eventState>active</eventState>
      <dateTime>2026-07-20T12:00:02+03:00</dateTime>
      <regionID>unknown-line</regionID><detectionTarget>vehicle</detectionTarget>
      <direction>unknown-direction</direction>
    </EventNotificationAlert>"""
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="lineCrossImage"; filename="line.jpg"\r\n\r\n'
        + _jpeg(100, 80)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", mode)
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.23": "CAM-23"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-23": {}})
    monkeypatch.setattr(settings, "ENTRY_CONFIRM_CAMERAS", "CAM-23,CAM-03")
    monkeypatch.setattr(settings, "CAM23_ENTRY_LINE", "")
    monkeypatch.setattr(settings, "CAM23_ENTRY_DIRECTION", "")
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    event = parse_camera_event(
        body,
        "10.0.0.23",
        f"multipart/form-data; boundary={boundary}",
    )

    assert bool(event.transient_images) is expects_vehicle_image
    if expects_vehicle_image:
        assert event.transient_images[0].filename == "vehicle-full-frame-1.jpg"
        assert event.snapshot_path is not None
        assert len(list(tmp_path.iterdir())) == 1
    else:
        assert event.snapshot_path is None
        assert list(tmp_path.iterdir()) == []


def test_authoritative_cam23_filter_precedes_explicit_vehicle_crop(
    tmp_path,
    monkeypatch,
):
    boundary = "cam23-explicit-crop-mismatch"
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.23</ipAddress><channelID>1</channelID>
      <eventType>linedetection</eventType><eventState>active</eventState>
      <dateTime>2026-07-20T12:00:02+03:00</dateTime>
      <regionID>other-line</regionID><detectionTarget>vehicle</detectionTarget>
      <direction>A-to-B</direction>
    </EventNotificationAlert>"""
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="vehiclePicture"; filename="vehiclePicture.jpg"\r\n\r\n'
        + _jpeg(100, 80)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.23": "CAM-23"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-23": {}})
    monkeypatch.setattr(settings, "ENTRY_CONFIRM_CAMERAS", "CAM-23,CAM-03")
    monkeypatch.setattr(settings, "CAM23_ENTRY_LINE", "park-entry")
    monkeypatch.setattr(settings, "CAM23_ENTRY_DIRECTION", "B-to-A")
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    event = parse_camera_event(
        body,
        "10.0.0.23",
        f"multipart/form-data; boundary={boundary}",
    )

    assert event.transient_images == ()
    assert event.snapshot_path is None
    assert list(tmp_path.iterdir()) == []


def test_missing_vehicle_rect_never_forwards_full_frame(tmp_path, monkeypatch):
    boundary = "missing-rect"
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.10</ipAddress><channelID>1</channelID>
      <eventType>ANPR</eventType><ANPR><licensePlate>ABC-1234</licensePlate></ANPR>
    </EventNotificationAlert>"""
    image = _jpeg(100, 80)
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="FW1"; filename="detectionPicture.jpg"\r\n\r\n'
        + image
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.10": "CAM-ENTRY"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-ENTRY": {"gate": "entry"}})
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    event = parse_camera_event(
        body,
        "10.0.0.10",
        f"multipart/form-data; boundary={boundary}",
    )

    assert event.transient_images == ()
    assert event.snapshot_path is None
    assert list(tmp_path.iterdir()) == []


def test_inward_cam23_without_bbox_uses_bounded_full_frame(
    tmp_path,
    monkeypatch,
):
    boundary = "cam23-no-bbox"
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.23</ipAddress><channelID>1</channelID>
      <eventType>linedetection</eventType><eventState>active</eventState>
      <dateTime>2026-07-20T12:00:02+03:00</dateTime>
      <regionID>park-entry</regionID><detectionTarget>vehicle</detectionTarget>
      <direction>B-to-A</direction>
    </EventNotificationAlert>"""
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="lineCrossImage"; filename="line.jpg"\r\n\r\n'
        + _jpeg(100, 80)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.23": "CAM-23"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-23": {}})
    monkeypatch.setattr(settings, "CAM23_ENTRY_LINE", "park-entry")
    monkeypatch.setattr(settings, "CAM23_ENTRY_DIRECTION", "B-to-A")
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    with patch("app.services.event_parser.logger.warning") as warning:
        event = parse_camera_event(
            body,
            "10.0.0.23",
            f"multipart/form-data; boundary={boundary}",
        )

    assert len(event.transient_images) == 1
    assert event.transient_images[0].filename == "vehicle-full-frame-1.jpg"
    assert event.transient_images[0].role == "vehicle"
    with Image.open(BytesIO(event.transient_images[0].data)) as crop:
        assert crop.size == (100, 80)
    assert any(
        "[full_frame_fallback]" in str(call.args[0])
        for call in warning.call_args_list
    )
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("line_id", "direction"),
    [("other-line", "B-to-A"), ("park-entry", "A-to-B")],
)
def test_cam23_full_frame_fallback_respects_inward_filter(
    tmp_path,
    monkeypatch,
    line_id,
    direction,
):
    boundary = "cam23-filter-mismatch"
    xml = f"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.23</ipAddress><channelID>1</channelID>
      <eventType>linedetection</eventType><eventState>active</eventState>
      <regionID>{line_id}</regionID><detectionTarget>vehicle</detectionTarget>
      <direction>{direction}</direction>
    </EventNotificationAlert>""".encode()
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="lineCrossImage"; filename="line.jpg"\r\n\r\n'
        + _jpeg(100, 80)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.23": "CAM-23"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-23": {}})
    monkeypatch.setattr(settings, "CAM23_ENTRY_LINE", "park-entry")
    monkeypatch.setattr(settings, "CAM23_ENTRY_DIRECTION", "B-to-A")
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    event = parse_camera_event(
        body,
        "10.0.0.23",
        f"multipart/form-data; boundary={boundary}",
    )

    assert event.transient_images == ()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("line_id", "direction", "expected_image"),
    [
        ("1", "B-to-A", True),
        ("1", "A-to-B", False),
        ("2", "B-to-A", False),
        ("2", "A-to-B", False),
    ],
)
def test_cam03_full_frame_fallback_requires_inward_calibration(
    tmp_path,
    monkeypatch,
    line_id,
    direction,
    expected_image,
):
    boundary = "cam03-filter"
    xml = f"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.3</ipAddress><channelID>1</channelID>
      <eventType>linedetection</eventType><eventState>active</eventState>
      <regionID>{line_id}</regionID><detectionTarget>vehicle</detectionTarget>
      <direction>{direction}</direction>
    </EventNotificationAlert>""".encode()
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="lineCrossImage"; filename="line.jpg"\r\n\r\n'
        + _jpeg(100, 80)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.3": "CAM-03"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-03": {"gate": "entry"}})
    monkeypatch.setattr(settings, "ENTRY_CONFIRM_CAMERAS", "CAM-23,CAM-03")
    monkeypatch.setattr(
        settings,
        "ENTRY_CONFIRM_DIRECTIONS",
        "CAM-23:ramp-entry,CAM-03:B-entry",
    )
    monkeypatch.setattr(settings, "OCCUPANCY_ENTRANCE_ZONES", "1")
    monkeypatch.setattr(settings, "FORWARD_DIRECTION_FIELD", "B-to-A")
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    event = parse_camera_event(
        body,
        "10.0.0.3",
        f"multipart/form-data; boundary={boundary}",
    )

    assert bool(event.transient_images) is expected_image
    if expected_image:
        assert event.transient_images[0].filename == "vehicle-full-frame-1.jpg"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("image_name", "rectangle_xml"),
    [
        ("vehiclePicture", ""),
        (
            "lineCrossImage",
            """<DetectionRegionList><DetectionRegionEntry>
              <regionID>2</regionID><detectionTarget>vehicle</detectionTarget>
              <TargetRect><X>0.2</X><Y>0.2</Y><width>0.4</width><height>0.4</height></TargetRect>
            </DetectionRegionEntry></DetectionRegionList>""",
        ),
    ],
    ids=("explicit-vehicle-picture", "usable-target-bbox"),
)
def test_cam03_non_entry_crossing_cannot_bypass_calibration(
    tmp_path,
    monkeypatch,
    image_name,
    rectangle_xml,
):
    boundary = "cam03-non-entry-bypass"
    xml = f"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.3</ipAddress><channelID>1</channelID>
      <eventType>linedetection</eventType><eventState>active</eventState>
      <dateTime>2026-07-20T12:00:02+03:00</dateTime>
      <regionID>2</regionID><detectionTarget>vehicle</detectionTarget>
      <direction>A-to-B</direction>{rectangle_xml}
    </EventNotificationAlert>""".encode()
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + (
            f'Content-Disposition: form-data; name="{image_name}"; '
            f'filename="{image_name}.jpg"\r\n\r\n'
        ).encode()
        + _jpeg(100, 80)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.3": "CAM-03"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-03": {"gate": "entry"}})
    monkeypatch.setattr(settings, "ENTRY_CONFIRM_CAMERAS", "CAM-23,CAM-03")
    monkeypatch.setattr(
        settings,
        "ENTRY_CONFIRM_DIRECTIONS",
        "CAM-23:ramp-entry,CAM-03:B-entry",
    )
    monkeypatch.setattr(settings, "OCCUPANCY_ENTRANCE_ZONES", "1")
    monkeypatch.setattr(settings, "FORWARD_DIRECTION_FIELD", "B-to-A")
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    event = parse_camera_event(
        body,
        "10.0.0.3",
        f"multipart/form-data; boundary={boundary}",
    )

    assert event.transient_images == ()
    assert is_entry_crossing(event) is False
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("image", "limit_name", "limit"),
    [
        (b"not-a-jpeg", None, None),
        (_jpeg(100, 80), "ENTRY_V2_MAX_IMAGE_BYTES", 20),
        (_jpeg(100, 80), "ENTRY_V2_MAX_SOURCE_DECODED_PIXELS", 100),
    ],
    ids=("malformed", "encoded-size-limit", "decoded-pixel-limit"),
)
def test_invalid_or_oversized_vehicle_image_fails_closed(
    tmp_path,
    monkeypatch,
    image,
    limit_name,
    limit,
):
    _configure_authoritative_entry_parser(monkeypatch, tmp_path)
    if limit_name is not None:
        monkeypatch.setattr(settings, limit_name, limit)

    event = parse_camera_event(
        _anpr_multipart(image),
        "10.0.0.10",
        "multipart/form-data; boundary=entry-test",
    )

    assert event.transient_images == ()
    assert event.snapshot_path is None
    assert list(tmp_path.iterdir()) == []


def test_high_entropy_source_is_adaptively_fitted_to_outbound_byte_limit(
    tmp_path,
    monkeypatch,
):
    boundary = "adaptive-byte-fit"
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.10</ipAddress><channelID>1</channelID>
      <eventType>ANPR</eventType><dateTime>2026-07-21T12:00:00+03:00</dateTime>
      <ANPR><licensePlate>ABC-1234</licensePlate></ANPR>
    </EventNotificationAlert>"""
    source = _noisy_jpeg(3464, 3464, quality=50)
    assert 4 * 1024 * 1024 < len(source) < 16 * 1024 * 1024
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="FW1"; filename="vehiclePicture.jpg"\r\n\r\n'
        + source
        + f"\r\n--{boundary}--\r\n".encode()
    )
    _configure_authoritative_entry_parser(monkeypatch, tmp_path)

    event = parse_camera_event(
        body,
        "10.0.0.10",
        f"multipart/form-data; boundary={boundary}",
    )

    assert len(event.transient_images) == 1
    fitted = event.transient_images[0]
    assert len(fitted.data) <= settings.ENTRY_V2_MAX_IMAGE_BYTES
    with Image.open(BytesIO(fitted.data)) as image:
        assert image.width * image.height <= settings.ENTRY_V2_MAX_DECODED_PIXELS
        assert max(image.size) <= settings.ENTRY_V2_MAX_IMAGE_DIMENSION
        assert image.size != (3464, 3464)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_extreme_finite_pixel_bbox_is_terminal_invalid_not_retryable(
    tmp_path,
    monkeypatch,
):
    _configure_authoritative_entry_parser(monkeypatch, tmp_path)
    monkeypatch.setattr(
        settings,
        "CAMERA_EVENT_ALLOWED_SOURCE_CIDRS",
        "10.0.0.0/24",
    )
    body = _anpr_multipart(_jpeg(100, 80)).replace(
        b"<width>40</width>",
        b"<width>1.7e308</width>",
    )
    db = MagicMock()
    with patch(
        "app.routers.events.dispatch_event",
        new_callable=AsyncMock,
        return_value={},
    ):
        response = await receive_camera_event(
            _Request(
                body=body,
                content_type="multipart/form-data; boundary=entry-test",
                client_host="10.0.0.10",
            ),
            db,
        )

    assert response == {"status": "ok", "event_type": "ANPR"}
    db.commit.assert_called_once()


@pytest.mark.parametrize(
    ("width", "height", "max_pixels", "max_dimension", "expected_size"),
    [
        (100, 80, 8_000, 100, (100, 80)),
        (100, 80, 7_999, 100, None),
        (101, 80, 8_100, 100, None),
    ],
    ids=("exact-envelope", "one-pixel-over", "one-dimension-over"),
)
def test_outbound_crop_enforces_exact_va_boundaries(
    tmp_path,
    monkeypatch,
    width,
    height,
    max_pixels,
    max_dimension,
    expected_size,
):
    _configure_authoritative_entry_parser(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "ENTRY_V2_MAX_DECODED_PIXELS", max_pixels)
    monkeypatch.setattr(settings, "ENTRY_V2_MAX_IMAGE_DIMENSION", max_dimension)
    body = _anpr_multipart(_jpeg(width, height)).replace(
        b"detectionPicture.jpg",
        b"vehiclePicture.jpg",
    )

    event = parse_camera_event(
        body,
        "10.0.0.10",
        "multipart/form-data; boundary=entry-test",
    )

    assert len(event.transient_images) == 1
    with Image.open(BytesIO(event.transient_images[0].data)) as crop:
        if expected_size is not None:
            assert crop.size == expected_size
        assert crop.width <= max_dimension
        assert crop.height <= max_dimension
        assert crop.width * crop.height <= max_pixels


def test_large_camera_crop_is_downscaled_to_va_default_envelope(
    tmp_path,
    monkeypatch,
):
    _configure_authoritative_entry_parser(monkeypatch, tmp_path)
    monkeypatch.setattr(settings, "ENTRY_V2_MAX_DECODED_PIXELS", 12_000_000)
    monkeypatch.setattr(settings, "ENTRY_V2_MAX_IMAGE_DIMENSION", 8192)
    monkeypatch.setattr(
        settings,
        "ENTRY_V2_MAX_SOURCE_DECODED_PIXELS",
        30_000_000,
    )
    body = _anpr_multipart(_jpeg(4000, 4000)).replace(
        b"detectionPicture.jpg",
        b"vehiclePicture.jpg",
    )

    event = parse_camera_event(
        body,
        "10.0.0.10",
        "multipart/form-data; boundary=entry-test",
    )

    assert len(event.transient_images) == 1
    _, _, files = _request_parts(event)
    assert len(files) == 1
    with Image.open(BytesIO(files[0][1][1])) as crop:
        assert crop.size == (3464, 3464)
        assert crop.width * crop.height <= 12_000_000


def test_out_of_range_normalized_vehicle_rect_fails_closed(tmp_path, monkeypatch):
    boundary = "invalid-normalized-rect"
    xml = b"""<EventNotificationAlert xmlns="http://www.isapi.org/ver20/XMLSchema">
      <ipAddress>10.0.0.23</ipAddress><channelID>1</channelID>
      <eventType>linedetection</eventType><eventState>active</eventState>
      <DetectionRegionList><DetectionRegionEntry>
        <regionID>1</regionID><detectionTarget>vehicle</detectionTarget>
        <TargetRect><X>-0.1</X><Y>0.2</Y><width>0.4</width><height>0.4</height></TargetRect>
      </DetectionRegionEntry></DetectionRegionList>
    </EventNotificationAlert>"""
    body = (
        f"--{boundary}\r\nContent-Type: application/xml\r\n\r\n".encode()
        + xml
        + f"\r\n--{boundary}\r\nContent-Type: image/jpeg\r\n".encode()
        + b'Content-Disposition: form-data; name="lineCrossImage"; filename="line.jpg"\r\n\r\n'
        + _jpeg(100, 100)
        + f"\r\n--{boundary}--\r\n".encode()
    )
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERA_IP_MAP", {"10.0.0.23": "CAM-23"})
    monkeypatch.setattr(settings, "CAMERA_SERIAL_MAP", {})
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-23": {}})
    monkeypatch.setattr("app.services.event_parser.SNAPSHOT_DIR", str(tmp_path))

    event = parse_camera_event(
        body,
        "10.0.0.23",
        f"multipart/form-data; boundary={boundary}",
    )

    assert event.transient_images == ()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_authoritative_dispatch_bypasses_legacy_entry_and_snapshot_fetch(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-ENTRY": {"gate": "entry"}})
    monkeypatch.setattr(settings, "ENTRY_CONFIRM_CAMERAS", "CAM-23,CAM-03")
    monkeypatch.setattr(settings, "LOG_CAMERA_FILTER", "")
    monkeypatch.setattr(settings, "LOG_CAMERA_EXCLUDE", "")
    event = _event()
    event.snapshot_path = None
    event.local_snapshot_path = None

    with (
        patch(
            "app.services.event_dispatcher.handle_anpr_event",
            new_callable=AsyncMock,
        ) as legacy_entry,
        patch(
            "app.services.event_dispatcher.fetch_snapshot",
            new_callable=AsyncMock,
        ) as snapshot_fetch,
        patch("app.services.event_dispatcher.add_event_to_feed"),
    ):
        await dispatch_event(event, MagicMock())

    legacy_entry.assert_not_awaited()
    snapshot_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_authoritative_dispatch_does_not_fifo_correct_misread(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-ENTRY": {"gate": "entry"}})
    monkeypatch.setattr(settings, "ENTRY_CONFIRM_CAMERAS", "")
    monkeypatch.setattr(settings, "ANPR_BURST_MAX_SECONDS", 20.0)
    monkeypatch.setattr(settings, "LOG_CAMERA_FILTER", "")
    monkeypatch.setattr(settings, "LOG_CAMERA_EXCLUDE", "")
    dispatcher._VMR_GATE_HINTS.clear()
    dispatcher._VMR_RECENT.clear()
    vmr = _event()
    vmr.event_type = "vehicleMatchResult"
    vmr.plate_number = "RGR-6466"
    misread = _event()
    misread.camera_id = "UNKNOWN-10.1.20.60"
    misread.gate = None
    misread.plate_number = "66466RA"

    with (
        patch("app.services.event_dispatcher.add_event_to_feed"),
        patch(
            "app.services.event_dispatcher.handle_anpr_event",
            new_callable=AsyncMock,
        ) as legacy_entry,
    ):
        await dispatch_event(vmr, MagicMock())
        await dispatch_event(misread, MagicMock())

    legacy_entry.assert_not_awaited()
    assert misread.camera_id == "UNKNOWN-10.1.20.60"
    assert misread.plate_number == "66466RA"
