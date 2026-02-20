# scripts/test/simulate_event.py
"""Send test events to backend. Supports Phase 1 (XML) and Phase 2 (JSON/ANPR)."""

import argparse
import requests
import json
from datetime import datetime

BACKEND_URL = "http://192.168.1.50:8080/api/v1/events/camera"

XML_TEMPLATES = {
    "fielddetection": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>fielddetection</eventType>
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
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID>
  </DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>""",

    "regionExiting": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>regionExiting</eventType>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID>
  </DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>""",

    "linedetection": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>linedetection</eventType>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID><detectionTarget>{target}</detectionTarget>
  </DetectionRegionEntry></DetectionRegionList>
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


def simulate_xml(event_type, zone, target, source_ip):
    body = XML_TEMPLATES[event_type].format(
        time=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        zone=zone, target=target,
    )
    resp = requests.post(BACKEND_URL, data=body.encode(),
                         headers={"Content-Type": "application/xml",
                                  "X-Forwarded-For": source_ip}, timeout=10)
    print(f"✅ {event_type} (XML) → HTTP {resp.status_code}: {resp.json()}")


def simulate_anpr(plate, gate_ip):
    payload = json.loads(json.dumps(JSON_ANPR_TEMPLATE))  # deep copy
    payload["dateTime"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["AccessControllerEvent"]["cardNo"] = plate
    resp = requests.post(BACKEND_URL, json=payload,
                         headers={"Content-Type": "application/json",
                                  "X-Forwarded-For": gate_ip}, timeout=10)
    print(f"✅ ANPR (JSON) plate={plate} gate={gate_ip} → HTTP {resp.status_code}: {resp.json()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulate camera events for testing")
    parser.add_argument("--event", default="fielddetection",
                        choices=list(XML_TEMPLATES.keys()) + ["anpr"])
    parser.add_argument("--zone", default="zone-A")
    parser.add_argument("--target", default="vehicle")
    parser.add_argument("--ip", default="192.168.1.103")
    parser.add_argument("--plate", default="ABC-1234")
    args = parser.parse_args()

    if args.event == "anpr":
        simulate_anpr(args.plate, args.ip)
    else:
        simulate_xml(args.event, args.zone, args.target, args.ip)
