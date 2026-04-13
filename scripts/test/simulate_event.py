# scripts/test/simulate_event.py
"""Send test events to backend. Supports Phase 1 (XML) and Phase 2 (JSON/ANPR)."""

import argparse
import requests
import json
from datetime import datetime

BACKEND_URL = "http://localhost:8080/api/v1/events/camera"

XML_TEMPLATES = {
    "fielddetection": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>fielddetection</eventType>
  <ipAddress>{ip}</ipAddress>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID><detectionTarget>{target}</detectionTarget>
  </DetectionRegionEntry></DetectionRegionList>
  <channelName>TestChannel</channelName>
</EventNotificationAlert>""",

    "regionEntrance": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>regionEntrance</eventType>
  <ipAddress>{ip}</ipAddress>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID>
  </DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>""",

    "regionExiting": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>regionExiting</eventType>
  <ipAddress>{ip}</ipAddress>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID>
  </DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>""",

    "linedetection": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>linedetection</eventType>
  <ipAddress>{ip}</ipAddress>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID><detectionTarget>{target}</detectionTarget>
  </DetectionRegionEntry></DetectionRegionList>
  <direction>{direction}</direction>
</EventNotificationAlert>""",
}

JSON_ANPR_TEMPLATE = {
    "eventType": "AccessControllerEvent",
    "eventState": "active",
    "eventDescription": "Test ANPR event",
    "dateTime": "",
    "deviceSerial": "TEST-ANPR-SIM",
    "AccessControllerEvent": {
        "deviceName": "Test ANPR Camera",
        "majorEventType": 1,
        "subEventType": 1,
        "cardNo": "",       # plate number
        "userType": "normal",
        "name": "Test Vehicle",
        "employeeNoString": "EMP-TEST",
    }
}


def simulate_xml(event_type, zone, target, source_ip, direction):
    body = XML_TEMPLATES[event_type].format(
        time=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
        zone=zone, target=target, ip=source_ip, direction=direction
    )
    resp = requests.post(BACKEND_URL, data=body.encode(),
                         headers={"Content-Type": "application/xml",
                                  "X-Forwarded-For": source_ip}, timeout=30)  # 30s: snapshot takes 4-8s
    print(f"✅ {event_type} (XML) ip={source_ip} dir={direction} → HTTP {resp.status_code}: {resp.json()}")


def simulate_anpr(plate, gate_ip):
    payload = json.loads(json.dumps(JSON_ANPR_TEMPLATE))  # deep copy
    payload["dateTime"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["AccessControllerEvent"]["cardNo"] = plate
    resp = requests.post(BACKEND_URL, json=payload,
                         headers={"Content-Type": "application/json",
                                  "X-Forwarded-For": gate_ip}, timeout=30)  # 30s: snapshot takes 4-8s
    print(f"✅ ANPR (JSON) plate={plate} gate={gate_ip} → HTTP {resp.status_code}: {resp.json()}")


if __name__ == "__main__":
    # Camera name → IP mapping (matches .env / config.py)
    CAMERA_IP_MAP = {
        "CAM-03": "10.1.13.62",
        "CAM-08": "10.1.13.67",
        "CAM-09": "10.1.13.68",
        "CAM-10": "10.1.13.69",
    }

    parser = argparse.ArgumentParser(description="Simulate camera events for testing")
    parser.add_argument("--event", default="linedetection",
                        choices=list(XML_TEMPLATES.keys()) + ["anpr"])
    # --zone / --line  (same meaning: which region/line ID to trigger)
    parser.add_argument("--zone", "--line", dest="zone", default="1")
    parser.add_argument("--target", default="vehicle")
    # --ip  is the raw IP; --cam  resolves via CAMERA_IP_MAP
    parser.add_argument("--ip", default=None)
    parser.add_argument("--cam", default=None, choices=list(CAMERA_IP_MAP.keys()),
                        help="Camera name (e.g. CAM-03). Overrides --ip.")
    parser.add_argument("--plate", default="ABC-1234")
    # --direction / --dir  (same meaning)
    parser.add_argument("--direction", "--dir", dest="direction",
                        default="B-to-A", choices=["B-to-A", "A-to-B"])
    args = parser.parse_args()

    # Resolve IP
    if args.cam:
        source_ip = CAMERA_IP_MAP.get(args.cam, "192.168.1.103")
    elif args.ip:
        source_ip = args.ip
    else:
        source_ip = "192.168.1.103"

    if args.event == "anpr":
        simulate_anpr(args.plate, source_ip)
    else:
        simulate_xml(args.event, args.zone, args.target, source_ip, args.direction)


