"""Stage 1 — a camera event must never be deleted by a 503.

Hikvision HTTP push is fire-and-forget: it ignores Retry-After and never
re-POSTs (332 of 332 production picture IDs were distinct). So every camera-facing
503 deleted the event rather than deferring it. These tests pin the replacement
contract: retryable failures are persisted and acknowledged 200, and the spool
degrades to the old 503 only when it genuinely cannot store the event.

Scenario numbers match CAMERA_EVENT_LOSS_PLAN.md, Stage 1.
"""

import json
import os

import pytest

from app.config import settings
from app.routers.events import CameraEventOutcome, _outcome_to_camera_response
from app.services import camera_ingest_spool as spool

BODY = b"<EventNotificationAlert><eventType>ANPR</eventType></EventNotificationAlert>"
GATEWAY = "10.1.20.60"
CT = "application/xml"


@pytest.fixture(autouse=True)
def spool_dir(tmp_path, monkeypatch):
    target = tmp_path / "camera_ingest_spool"
    monkeypatch.setattr(settings, "CAMERA_INGEST_SPOOL_DIR", str(target), raising=False)
    monkeypatch.setattr(settings, "CAMERA_INGEST_SPOOL_ENABLED", True, raising=False)
    monkeypatch.setattr(
        settings, "CAMERA_INGEST_SPOOL_MAX_BYTES", 10 * 1024 * 1024, raising=False
    )
    monkeypatch.setattr(settings, "CAMERA_INGEST_SPOOL_MIN_FREE_BYTES", 0, raising=False)
    monkeypatch.setattr(settings, "CAMERA_INGEST_SPOOL_MAX_ATTEMPTS", 3, raising=False)
    spool._reported_degradations.clear()
    spool._durability.update({"checked": False, "durable": None, "detail": "not checked"})
    return target


def _retryable(detail="entry validation unavailable", evidence_id="ev-1"):
    return CameraEventOutcome(status="retry", detail=detail, evidence_id=evidence_id)


def _respond(outcome, body=BODY):
    return _outcome_to_camera_response(outcome, body, GATEWAY, CT)


class TestTheCameraIsNeverToldToRetry:
    """Scenarios 1-4: every retryable failure is spooled and acknowledged."""

    @pytest.mark.parametrize(
        "reason",
        [
            "entry validation unavailable",   # 1/2 VA capacity + VA timeout
            "entry state is busy",            # 3   EntryStateLockUnavailable
            "camera event processing unavailable",  # 4 DB transaction log full
        ],
    )
    def test_retryable_failure_returns_200_and_spools(self, reason):
        response = _respond(_retryable(reason))
        assert not hasattr(response, "status_code"), "must not be a 503 response"
        assert response["status"] == "accepted"
        assert len(list(spool.iter_spooled_records())) == 1

    def test_the_spooled_record_round_trips_byte_for_byte(self):
        _respond(_retryable())
        path = next(iter(spool.iter_spooled_records()))
        header, body = spool.read_record(path)
        assert body == BODY, "replay must see exactly what the camera sent"
        assert header["camera_ip"] == GATEWAY
        assert header["content_type"] == CT
        assert header["evidence_id"] == "ev-1"

    def test_binary_multipart_body_survives(self):
        """Real ANPR events are multipart with JPEGs — no text assumptions."""
        binary = b"--b\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xff\xe0\x00\x10JFIF\r\n--b--"
        _respond(_retryable(), body=binary)
        _, body = spool.read_record(next(iter(spool.iter_spooled_records())))
        assert body == binary

    def test_body_containing_newlines_is_not_truncated(self):
        """The header is one line; the body must not be parsed as part of it."""
        multiline = b"<a>\n<b>line two</b>\n</a>"
        _respond(_retryable(), body=multiline)
        _, body = spool.read_record(next(iter(spool.iter_spooled_records())))
        assert body == multiline


class TestNonRetryableIsUnchanged:
    def test_scenario_5_malformed_payload_is_not_spooled(self):
        """Poison must never enter the spool — it would retry forever."""
        response = _respond(CameraEventOutcome(status="rejected", detail="malformed"))
        assert response["status"] == "rejected"
        assert list(spool.iter_spooled_records()) == []

    def test_success_is_not_spooled(self):
        response = _respond(CameraEventOutcome(status="ok", event_type="ANPR"))
        assert response["status"] == "ok"
        assert response["event_type"] == "ANPR"
        assert list(spool.iter_spooled_records()) == []


class TestDegradation:
    def test_scenario_6_full_spool_falls_back_to_503(self, monkeypatch):
        monkeypatch.setattr(settings, "CAMERA_INGEST_SPOOL_MAX_BYTES", 1, raising=False)
        _respond(_retryable())  # first write fills the 1-byte cap
        response = _respond(_retryable())
        assert getattr(response, "status_code", None) == 503

    def test_free_space_floor_protects_the_shared_volume(self, monkeypatch):
        """The spool shares its volume with the live snapshot store."""
        monkeypatch.setattr(
            settings, "CAMERA_INGEST_SPOOL_MIN_FREE_BYTES", 1 << 62, raising=False
        )
        response = _respond(_retryable())
        assert getattr(response, "status_code", None) == 503
        assert list(spool.iter_spooled_records()) == []

    def test_unwritable_spool_falls_back_and_never_raises(self, monkeypatch):
        monkeypatch.setattr(
            spool, "_capacity_refusal", lambda: None, raising=False
        )
        monkeypatch.setattr(
            spool.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("denied"))
        )
        response = _respond(_retryable())
        assert getattr(response, "status_code", None) == 503

    def test_disabled_flag_preserves_todays_exact_behaviour(self, monkeypatch):
        monkeypatch.setattr(settings, "CAMERA_INGEST_SPOOL_ENABLED", False, raising=False)
        response = _respond(_retryable())
        assert getattr(response, "status_code", None) == 503
        assert response.headers["Retry-After"] == "1"
        assert list(spool.iter_spooled_records()) == []


class TestOrdering:
    """Scenario 9: an entry burst must replay in the order the cameras produced it."""

    def test_records_drain_oldest_first_by_received_at_not_directory_order(self):
        for marker in (b"CAM-23", b"CAM-03", b"ANPR"):
            _respond(_retryable(), body=marker)

        paths = list(spool.iter_spooled_records())
        assert len(paths) == 3
        bodies = [spool.read_record(p)[1] for p in paths]
        assert bodies == [b"CAM-23", b"CAM-03", b"ANPR"]

    def test_mtime_shuffling_does_not_reorder_the_burst(self):
        for marker in (b"first", b"second", b"third"):
            _respond(_retryable(), body=marker)
        # Reverse the filesystem timestamps; ordering must follow the header.
        paths = sorted(os.listdir(settings.CAMERA_INGEST_SPOOL_DIR))
        for index, name in enumerate(reversed(paths)):
            full = os.path.join(settings.CAMERA_INGEST_SPOOL_DIR, name)
            os.utime(full, (1_000_000 + index, 1_000_000 + index))
        bodies = [spool.read_record(p)[1] for p in spool.iter_spooled_records()]
        assert bodies == [b"first", b"second", b"third"]


class TestCrashSafety:
    def test_partial_temp_files_are_never_read_as_records(self, spool_dir):
        _respond(_retryable())
        spool_dir.joinpath("cam_half_written.evt.tmp").write_bytes(b'{"broken"')
        assert len(list(spool.iter_spooled_records())) == 1

    def test_unreadable_record_is_quarantined_not_retried_forever(self, spool_dir):
        spool_dir.mkdir(parents=True, exist_ok=True)
        spool_dir.joinpath("cam_corrupt.evt").write_bytes(b"no header terminator")
        assert list(spool.iter_spooled_records()) == []
        assert spool_dir.joinpath("quarantine", "cam_corrupt.evt").exists()

    def test_attempt_count_persists_and_preserves_the_body(self):
        _respond(_retryable())
        path = next(iter(spool.iter_spooled_records()))
        header, _ = spool.read_record(path)
        assert spool.record_attempt(path, header) == 1
        assert spool.record_attempt(path, spool.read_record(path)[0]) == 2
        header, body = spool.read_record(path)
        assert header["attempts"] == 2
        assert body == BODY, "the body must survive an attempt-count rewrite"


class TestDurabilitySelfCheck:
    """P1 answered by the service instead of by kubectl."""

    def test_first_boot_is_honestly_reported_as_unknown(self):
        result = spool.check_spool_durability()
        assert result["durable"] is None
        assert "ephemeral" in result["detail"]

    def test_second_boot_on_the_same_directory_proves_durability(self):
        spool.check_spool_durability()
        result = spool.check_spool_durability()
        assert result["durable"] is True
        assert result["previous_boot"]

    def test_wiped_directory_reports_not_durable_again(self, spool_dir):
        spool.check_spool_durability()
        spool.check_spool_durability()
        # Simulate a pod restart onto ephemeral storage.
        os.remove(spool_dir / spool.BOOT_MARKER_NAME)
        assert spool.check_spool_durability()["durable"] is None

    def test_unwritable_directory_reports_not_durable_and_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(
            spool.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("ro"))
        )
        result = spool.check_spool_durability()
        assert result["durable"] is False

    def test_marker_is_not_mistaken_for_a_spooled_event(self):
        spool.check_spool_durability()
        assert list(spool.iter_spooled_records()) == []


class TestStats:
    def test_stats_report_depth_and_durability(self):
        _respond(_retryable())
        _respond(_retryable())
        stats = spool.spool_stats()
        assert stats["depth"] == 2
        assert stats["enabled"] is True
        assert stats["bytes"] > 0
        assert stats["oldest_age_seconds"] is not None

    def test_stats_on_a_missing_directory_do_not_raise(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            settings, "CAMERA_INGEST_SPOOL_DIR", str(tmp_path / "nope"), raising=False
        )
        assert spool.spool_stats()["depth"] == 0


class TestHealthExposure:
    """Regression guard for two mistakes made while wiring this up.

    The helper was first inserted BETWEEN @router.get("/health") and
    health_check, so the decorator bound the route to the helper instead of the
    real handler. The suite still passed, because nothing asserted that /health
    returns a health payload. Then the field was filtered out entirely by
    response_model=HealthResponse until it was declared on the schema.
    """

    def test_health_returns_the_real_handler_payload_with_the_spool_block(self):
        from fastapi.testclient import TestClient

        import app.main as main_module

        with TestClient(main_module.app) as client:
            response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        # If the decorator ever binds to the wrong function again, these vanish.
        for key in ("status", "backend", "database", "cameras"):
            assert key in body, f"/health lost {key} — is the decorator on the right function?"
        assert "camera_ingest_spool" in body, "declare it on HealthResponse or it is filtered out"
        assert "depth" in body["camera_ingest_spool"]
        assert "durability" in body["camera_ingest_spool"]
