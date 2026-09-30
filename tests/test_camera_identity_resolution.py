"""Stage 4 — camera identity must come from the body, not the NAT gateway.

Every camera reaches PMS-AI through one gateway (10.1.20.60 in production), so
the HTTP client IP identifies nothing. These tests pin the two behaviours that
cost us 207/207 entry ANPR events: a serial that differs only cosmetically from
the configured one must still resolve, and a genuine failure must name the
values that failed rather than the gateway every camera shares.
"""

import pytest

from app.config import settings
from app.services import event_parser
from app.services.event_parser import parse_camera_event, resolve_camera_identity

GATEWAY_IP = "10.1.20.60"
ENTRY_IP = "10.1.13.100"
EXIT_IP = "10.1.13.101"
ENTRY_SERIAL = "DS-TCG406-E 20250221AIFW6259223"
EXIT_SERIAL = "DS-TCG406-E 20250221AIFW6259222"


@pytest.fixture(autouse=True)
def camera_maps(monkeypatch):
    """Mirror the production shape: model+space+serial, cameras behind one gateway."""
    monkeypatch.setattr(
        settings,
        "CAMERA_SERIAL_MAP",
        {ENTRY_SERIAL: "CAM-ENTRY", EXIT_SERIAL: "CAM-EXIT"},
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "CAMERA_IP_MAP",
        {ENTRY_IP: "CAM-ENTRY", EXIT_IP: "CAM-EXIT"},
        raising=False,
    )
    # These caches only suppress duplicate log lines; clear them so each test
    # observes its own reporting behaviour.
    event_parser._reported_unresolved_identities.clear()
    event_parser._reported_loose_serial_matches.clear()


class TestSerialMatching:
    def test_exact_serial_resolves(self):
        assert resolve_camera_identity(ENTRY_SERIAL, None, GATEWAY_IP) == "CAM-ENTRY"

    def test_serial_without_the_space_resolves(self):
        """A camera reporting model+serial unspaced is the same camera."""
        assert (
            resolve_camera_identity("DS-TCG406-E20250221AIFW6259223", None, GATEWAY_IP)
            == "CAM-ENTRY"
        )

    def test_bare_serial_token_resolves(self):
        """A camera reporting only the serial, with no model prefix."""
        assert (
            resolve_camera_identity("20250221AIFW6259223", None, GATEWAY_IP)
            == "CAM-ENTRY"
        )

    def test_case_and_punctuation_differences_resolve(self):
        assert (
            resolve_camera_identity("ds_tcg406_e_20250221aifw6259223", None, GATEWAY_IP)
            == "CAM-ENTRY"
        )

    def test_entry_and_exit_are_not_confused(self):
        """The two gate serials differ only in the final digit — never alias them."""
        assert resolve_camera_identity("20250221AIFW6259223", None, GATEWAY_IP) == "CAM-ENTRY"
        assert resolve_camera_identity("20250221AIFW6259222", None, GATEWAY_IP) == "CAM-EXIT"

    def test_ambiguous_serial_does_not_resolve(self, monkeypatch):
        """If a normalized serial could be two cameras, refuse rather than guess."""
        monkeypatch.setattr(
            settings,
            "CAMERA_SERIAL_MAP",
            {"SHARED 0001": "CAM-A", "SHARED-0001": "CAM-B"},
            raising=False,
        )
        monkeypatch.setattr(settings, "CAMERA_IP_MAP", {}, raising=False)
        assert resolve_camera_identity("SHARED0001", None, GATEWAY_IP).startswith("UNKNOWN")

    def test_short_fragment_does_not_use_containment(self, monkeypatch):
        """Containment is length-guarded so a short fragment cannot alias a camera."""
        monkeypatch.setattr(
            settings, "CAMERA_SERIAL_MAP", {"CAMERA-ALPHA-0001": "CAM-A"}, raising=False
        )
        monkeypatch.setattr(settings, "CAMERA_IP_MAP", {}, raising=False)
        assert resolve_camera_identity("0001", None, GATEWAY_IP).startswith("UNKNOWN")

    def test_model_prefix_alone_cannot_claim_a_camera(self):
        """Both gate cameras are DS-TCG406-E — the model must never identify one."""
        assert resolve_camera_identity("DS-TCG406-E", None, GATEWAY_IP).startswith("UNKNOWN")

    def test_model_prefix_is_not_indexed_even_for_a_lone_camera(self, monkeypatch):
        """Uniqueness alone must not let a model name become an identity."""
        monkeypatch.setattr(
            settings, "CAMERA_SERIAL_MAP", {ENTRY_SERIAL: "CAM-ENTRY"}, raising=False
        )
        monkeypatch.setattr(settings, "CAMERA_IP_MAP", {}, raising=False)
        assert resolve_camera_identity("DS-TCG406-E", None, GATEWAY_IP).startswith("UNKNOWN")


class TestIpFallback:
    def test_declared_ip_resolves_when_serial_is_absent(self):
        assert resolve_camera_identity("unknown", ENTRY_IP, GATEWAY_IP) == "CAM-ENTRY"

    def test_serial_wins_over_a_conflicting_declared_ip(self):
        """Serial is the stronger identity; a stale/NAT-rewritten IP must not override it."""
        assert resolve_camera_identity(ENTRY_SERIAL, EXIT_IP, GATEWAY_IP) == "CAM-ENTRY"

    def test_mapped_client_ip_still_resolves(self):
        """Direct-connected cameras (no NAT) keep working unchanged."""
        assert resolve_camera_identity("unknown", None, ENTRY_IP) == "CAM-ENTRY"


class TestUnresolvedIsDiagnosable:
    def test_placeholder_names_the_declared_ip_not_the_gateway(self):
        """The old code labelled this UNKNOWN-<gateway>, hiding the real value."""
        resolved = resolve_camera_identity("unknown", "10.9.9.9", GATEWAY_IP)
        assert resolved == "UNKNOWN-10.9.9.9"

    def test_placeholder_falls_back_to_gateway_when_body_is_silent(self):
        assert resolve_camera_identity("unknown", None, GATEWAY_IP) == f"UNKNOWN-{GATEWAY_IP}"

    def test_unresolved_logs_serial_and_ip_once_per_signature(self, caplog):
        with caplog.at_level("WARNING"):
            resolve_camera_identity("MYSTERY-SERIAL", "10.9.9.9", GATEWAY_IP)
            resolve_camera_identity("MYSTERY-SERIAL", "10.9.9.9", GATEWAY_IP)
        identity_logs = [r for r in caplog.records if "[Identity] unresolved" in r.getMessage()]
        assert len(identity_logs) == 1, "must report once per distinct signature, not per event"
        message = identity_logs[0].getMessage()
        assert "MYSTERY-SERIAL" in message
        assert "10.9.9.9" in message

    def test_a_different_signature_is_reported_separately(self, caplog):
        with caplog.at_level("WARNING"):
            resolve_camera_identity("SERIAL-A", None, GATEWAY_IP)
            resolve_camera_identity("SERIAL-B", None, GATEWAY_IP)
        identity_logs = [r for r in caplog.records if "[Identity] unresolved" in r.getMessage()]
        assert len(identity_logs) == 2

    def test_loose_serial_match_warns_to_fix_the_config(self, caplog):
        with caplog.at_level("WARNING"):
            resolve_camera_identity("20250221AIFW6259223", None, GATEWAY_IP)
            resolve_camera_identity("20250221AIFW6259223", None, GATEWAY_IP)
        fix_logs = [r for r in caplog.records if "matched by" in r.getMessage()]
        assert len(fix_logs) == 1, "resolving loosely is a config smell — say so once"
        assert "CAM-ENTRY" in fix_logs[0].getMessage()


class TestThroughTheParser:
    """The scenarios as they actually arrive: XML and JSON, behind the gateway."""

    def _anpr_xml(self, serial: str | None, ip: str | None, plate: str | None) -> bytes:
        parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<EventNotificationAlert>"]
        if ip:
            parts.append(f"<ipAddress>{ip}</ipAddress>")
        if serial:
            parts.append(f"<deviceSerial>{serial}</deviceSerial>")
        parts.append("<eventType>ANPR</eventType>")
        parts.append("<dateTime>2026-09-18T08:00:00+03:00</dateTime>")
        if plate:
            parts.append(f"<licensePlateNumber>{plate}</licensePlateNumber>")
        parts.append("</EventNotificationAlert>")
        return "".join(parts).encode()

    def test_scenario_1_anpr_with_mapped_serial_resolves(self):
        event = parse_camera_event(
            self._anpr_xml(ENTRY_SERIAL, None, "ABC-1234"), GATEWAY_IP, "application/xml"
        )
        assert event.camera_id == "CAM-ENTRY"

    def test_scenario_2_anpr_with_no_plate_still_resolves(self):
        """The 13 events lost every week: the plate rescue cannot help here."""
        event = parse_camera_event(
            self._anpr_xml(ENTRY_SERIAL, None, None), GATEWAY_IP, "application/xml"
        )
        assert event.camera_id == "CAM-ENTRY"
        assert event.plate_number is None

    def test_scenario_3_unmapped_camera_degrades_and_names_itself(self):
        event = parse_camera_event(
            self._anpr_xml("NEW-CAMERA-9", "10.1.13.199", "ABC-1234"),
            GATEWAY_IP,
            "application/xml",
        )
        assert event.camera_id == "UNKNOWN-10.1.13.199"

    def test_scenario_4_exit_anpr_unchanged(self):
        event = parse_camera_event(
            self._anpr_xml(EXIT_SERIAL, EXIT_IP, "XYZ-9999"), GATEWAY_IP, "application/xml"
        )
        assert event.camera_id == "CAM-EXIT"

    def test_json_vehiclematchresult_path_uses_the_same_resolver(self):
        body = (
            b'{"eventType":"vehicleMatchResult",'
            b'"deviceSerial":"20250221AIFW6259223",'
            b'"dateTime":"2026-09-18T08:00:00+03:00",'
            b'"VehicleMatchResult":{"PlateInfo":{"plate":"ABC-1234"}}}'
        )
        event = parse_camera_event(body, GATEWAY_IP, "application/json")
        assert event.camera_id == "CAM-ENTRY"

    def test_body_identity_beats_the_gateway_for_every_camera(self):
        """All cameras share the gateway; only the body can tell them apart."""
        entry = parse_camera_event(
            self._anpr_xml(ENTRY_SERIAL, None, "A-1"), GATEWAY_IP, "application/xml"
        )
        exit_ = parse_camera_event(
            self._anpr_xml(EXIT_SERIAL, None, "B-2"), GATEWAY_IP, "application/xml"
        )
        assert (entry.camera_id, exit_.camera_id) == ("CAM-ENTRY", "CAM-EXIT")
