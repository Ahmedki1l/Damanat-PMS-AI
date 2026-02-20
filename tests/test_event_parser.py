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

        event = parse_camera_event(xml, "192.168.1.101", "application/xml")
        assert event.event_type == "fielddetection"
        assert event.detection_target == "vehicle"
        assert event.region_id == "restricted-vip"
        assert event.camera_id == "CAM-01"
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

        event = parse_camera_event(xml, "192.168.1.103", "application/xml")
        assert event.event_type == "regionEntrance"
        assert event.region_id == "parking-row-A"
        assert event.camera_id == "CAM-03"

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

        event = parse_camera_event(xml, "192.168.1.101", "application/xml")
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

        event = parse_camera_event(json_body, "192.168.1.104", "application/json")
        assert event.event_type == "AccessControllerEvent"
        assert event.plate_number == "ABC-1234"
        assert event.gate == "entry"
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

        event = parse_camera_event(json_body, "192.168.1.105", "application/json")
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
