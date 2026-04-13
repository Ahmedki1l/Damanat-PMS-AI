"""Fire a linedetection event and verify the snapshot CDN URL is stored in the alert."""
import requests
import time
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

URL = "http://localhost:8081/api/v1/events/camera"

ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
# Use regionEntrance on emergency-exit zone (CAM-02 / 10.1.13.20) — triggers an intrusion alert
body = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">'
    "<deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>"
    f"<triggerTime>{ts}</triggerTime><eventType>regionEntrance</eventType>"
    "<ipAddress>10.1.13.20</ipAddress>"
    "<eventDescription>Test</eventDescription>"
    "<DetectionRegionList><DetectionRegionEntry>"
    "<regionID>emergency-exit</regionID><detectionTarget>vehicle</detectionTarget>"
    "</DetectionRegionEntry></DetectionRegionList>"
    "<channelName>TestChannel</channelName>"
    "</EventNotificationAlert>"
)

print("Firing regionEntrance (intrusion) event on emergency-exit via CAM-02...")
r = requests.post(
    URL, data=body.encode(),
    headers={"Content-Type": "application/xml", "X-Forwarded-For": "10.1.13.20"},
    timeout=30,
)
print(f"Event response: {r.status_code} {r.json()}")

time.sleep(2)

from app.database import SessionLocal
from app.models.alert import Alert
from app.models.camera_event import CameraEvent

db = SessionLocal()

alert = db.query(Alert).order_by(Alert.triggered_at.desc()).first()
event = db.query(CameraEvent).order_by(CameraEvent.created_at.desc()).first()

print(f"\n--- Latest Alert ---")
print(f"  id={alert.id}  type={alert.alert_type}  camera={alert.camera_id}")
print(f"  snapshot_path={alert.snapshot_path}")

print(f"\n--- Latest CameraEvent ---")
print(f"  id={event.id}  type={event.event_type}  camera={event.camera_id}")
print(f"  snapshot_path={event.snapshot_path}")

db.close()

