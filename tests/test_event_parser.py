# tests/test_event_parser.py
"""Unit tests for the event parser module."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from app.services.event_parser import parse_camera_event


class TestXMLEventParsing:
    """Phase 1: XML event parsing tests."""

    def test_fielddetection_event(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
          <deviceSerial>DS-2CD3681G2-001</deviceSerial>
          <channelID>1</channelID>
          <triggerTime>2026-02-20T10:30:00Z</triggerTime>
          <eventType>fielddetection</eventType>
          <DetectionRegionList><DetectionRegionEntry>
            <regionID>restricted-vip</regionID>
            <detectionTarget>vehicle</detectionTarget>
          </DetectionRegionEntry></DetectionRegionList>
          <channelName>Parking-Cam01</channelName>
        </EventNotificationAlert>"""

        from app.config import settings
        event = parse_camera_event(xml, settings.CAM_02_IP, "application/xml")
        assert event.event_type == "fielddetection"
        assert event.detection_target == "vehicle"
        assert event.region_id == "restricted-vip"
        assert event.camera_id == "CAM-02"
        assert event.device_serial == "DS-2CD3681G2-001"

    def test_region_entrance_event(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
          <deviceSerial>DS-2CD3783G2-003</deviceSerial>
          <channelID>1</channelID>
          <triggerTime>2026-02-20T11:00:00Z</triggerTime>
          <eventType>regionEntrance</eventType>
          <DetectionRegionList><DetectionRegionEntry>
            <regionID>parking-row-A</regionID>
          </DetectionRegionEntry></DetectionRegionList>
        </EventNotificationAlert>"""

        from unittest.mock import patch
        from app.config import settings
        fake_ip = "192.168.99.35"
        fake_map = {**settings.CAMERA_IP_MAP, fake_ip: "CAM-35"}
        with patch.object(settings, "CAMERA_IP_MAP", fake_map):
            event = parse_camera_event(xml, fake_ip, "application/xml")
        assert event.event_type == "regionEntrance"
        assert event.region_id == "parking-row-A"
        assert event.camera_id == "CAM-35"

    def test_region_exiting_event(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
          <channelID>1</channelID>
          <eventType>regionExiting</eventType>
          <DetectionRegionList><DetectionRegionEntry>
            <regionID>parking-row-B</regionID>
          </DetectionRegionEntry></DetectionRegionList>
        </EventNotificationAlert>"""

        event = parse_camera_event(xml, "192.168.1.103", "application/xml")
        assert event.event_type == "regionExiting"
        assert event.region_id == "parking-row-B"

    def test_linedetection_event(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
          <channelID>1</channelID>
          <eventType>linedetection</eventType>
          <DetectionRegionList><DetectionRegionEntry>
            <regionID>exit-line</regionID>
            <detectionTarget>vehicle</detectionTarget>
          </DetectionRegionEntry></DetectionRegionList>
        </EventNotificationAlert>"""

        from app.config import settings
        event = parse_camera_event(xml, settings.CAM_02_IP, "application/xml")
        assert event.event_type == "linedetection"
        assert event.detection_target == "vehicle"

    def test_unknown_camera_ip(self):
        xml = b"""<?xml version="1.0" encoding="utf-8"?>
        <EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
          <channelID>1</channelID>
          <eventType>fielddetection</eventType>
        </EventNotificationAlert>"""

        event = parse_camera_event(xml, "10.0.0.99", "application/xml")
        assert event.camera_id == "UNKNOWN-10.0.0.99"


class TestJSONEventParsing:
    """Phase 2: JSON/ANPR event parsing tests."""

    def test_anpr_entry_event(self):
        json_body = b"""{
            "eventType": "AccessControllerEvent",
            "dateTime": "2026-02-20T12:00:00Z",
            "deviceSerial": "ANPR-ENTRY-001",
            "channelID": 1,
            "AccessControllerEvent": {
                "deviceName": "Entry Gate ANPR",
                "cardNo": "ABC-1234",
                "userType": "normal",
                "name": "Ahmed",
                "employeeNoString": "EMP-001"
            }
        }"""

        from app.config import settings
        event = parse_camera_event(json_body, settings.CAM_04_IP, "application/json")
        assert event.event_type == "AccessControllerEvent"
        assert event.plate_number == "ABC-1234"
        # Since gate is not defined in Phase 1 Cam config, it will be None
        assert event.gate is None 
        assert event.person_name == "Ahmed"
        assert event.detection_target == "vehicle"

    def test_anpr_exit_event(self):
        json_body = b"""{
            "eventType": "AccessControllerEvent",
            "dateTime": "2026-02-20T14:30:00Z",
            "deviceSerial": "ANPR-EXIT-001",
            "AccessControllerEvent": {
                "cardNo": "XYZ-5678",
                "userType": "visitor"
            }
        }"""

        from app.config import settings
        event = parse_camera_event(json_body, settings.CAM_EXIT_IP, "application/json")
        assert event.event_type == "AccessControllerEvent"
        assert event.plate_number == "XYZ-5678"
        assert event.gate == "exit"
        assert event.user_type == "visitor"

    def test_auto_detect_json(self):
        """Parser should auto-detect JSON even without content-type header."""
        json_body = b'{"eventType": "AccessControllerEvent", "AccessControllerEvent": {"cardNo": "TEST-001"}}'

        event = parse_camera_event(json_body, "192.168.1.104", "")
        assert event.event_type == "AccessControllerEvent"
        assert event.plate_number == "TEST-001"


class TestPlateNormalization:
    """`_normalize_plate` canonicalizes reads + rejects OCR garbage so the same
    physical plate always yields one string (dedup/matching rely on equality)
    and malformed reads aren't stored as fake plates."""

    @pytest.mark.parametrize("raw,expected", [
        # Same physical plate, varied separators/case → ONE canonical form
        # (this is what fixes the "registered twice" duplicate sessions).
        ("9444HUD", "9444-HUD"),
        ("9444 HUD", "9444-HUD"),
        ("9444-HUD", "9444-HUD"),
        ("  9444-hud ", "9444-HUD"),
        # Letter-first reads keep their order.
        ("HUD9444", "HUD-9444"),
        ("HUD-9444", "HUD-9444"),
        # Already-canonical reads pass through unchanged (incl. production
        # digits-letters format and existing test plates).
        ("ABC-1234", "ABC-1234"),
        ("4918-AVD", "4918-AVD"),
        ("TEST-001", "TEST-001"),
    ])
    def test_canonicalizes_consistently(self, raw, expected):
        from app.services.event_parser import _normalize_plate
        assert _normalize_plate(raw) == expected

    @pytest.mark.parametrize("raw", [
        "6466466",   # all-digit OCR misread
        "1211",      # all-digit OCR misread
        "HUDABC",    # all-letter (no digit)
        "UNKNOWN", "N/A", "NONE", "NULL", "", "  ",
    ])
    def test_rejects_implausible_reads(self, raw):
        from app.services.event_parser import _normalize_plate
        assert _normalize_plate(raw) is None

