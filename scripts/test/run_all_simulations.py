"""Run a full batch of test events against the backend to populate the DB with demo data."""
import requests
import time
from datetime import datetime

URL = "http://localhost:8080/api/v1/events/camera"


def ts():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def xml_event(event_type, zone, target, ip):
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
        "<deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>"
        f"<triggerTime>{ts()}</triggerTime><eventType>{event_type}</eventType>"
        f"<ipAddress>{ip}</ipAddress>"
        "<eventDescription>Test</eventDescription>"
        "<DetectionRegionList><DetectionRegionEntry>"
        f"<regionID>{zone}</regionID><detectionTarget>{target}</detectionTarget>"
        "</DetectionRegionEntry></DetectionRegionList>"
        "<channelName>TestChannel</channelName>"
        "</EventNotificationAlert>"
    )
    r = requests.post(
        URL, data=body.encode(),
        headers={"Content-Type": "application/xml", "X-Forwarded-For": ip},
        timeout=15,
    )
    print(f"  {event_type} zone={zone} ip={ip} -> HTTP {r.status_code}: {r.json()}")


def anpr_event(plate, ip):
    payload = {
        "eventType": "AccessControllerEvent",
        "eventState": "active",
        "eventDescription": "Test ANPR",
        "dateTime": ts() + "Z",
        "deviceSerial": "TEST-ANPR-SIM",
        "AccessControllerEvent": {
            "deviceName": "Test ANPR Camera",
            "majorEventType": 1,
            "subEventType": 1,
            "cardNo": plate,
            "userType": "normal",
            "name": "Test Vehicle",
            "employeeNoString": "EMP-TEST",
        },
    }
    r = requests.post(
        URL, json=payload,
        headers={"Content-Type": "application/json", "X-Forwarded-For": ip},
        timeout=15,
    )
    print(f"  ANPR plate={plate} ip={ip} -> HTTP {r.status_code}: {r.json()}")


print("\n--- 1. Occupancy entries (B1-PARKING via CAM-04, CAM-05, CAM-06) ---")
xml_event("fielddetection", "1", "vehicle", "10.1.13.63")
xml_event("fielddetection", "1", "vehicle", "10.1.13.64")
xml_event("fielddetection", "1", "vehicle", "10.1.13.65")

print("\n--- 2. Violation (linedetection via CAM-04) ---")
xml_event("linedetection", "1", "vehicle", "10.1.13.63")

print("\n--- 3. Intrusion (regionEntrance on emergency-exit via CAM-02) ---")
xml_event("regionEntrance", "emergency-exit", "vehicle", "10.1.13.20")

print("\n--- 4. ANPR Entry (CAM-ENTRY gate) ---")
anpr_event("ABC-1234", "10.1.13.100")
anpr_event("XYZ-5678", "10.1.13.100")

print("\n--- 5. Waiting 8 seconds before exit... ---")
time.sleep(8)

print("\n--- 6. ANPR Exit (CAM-EXIT gate) ---")
anpr_event("ABC-1234", "10.1.13.101")
anpr_event("XYZ-5678", "10.1.13.101")

print("\n--- Done! All events fired. ---\n")

