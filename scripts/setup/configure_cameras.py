# scripts/setup/configure_cameras.py
"""
Configure Hikvision cameras to push events to the Damanat backend.
Sets up HTTP host notification on each camera via ISAPI.

Usage:
    python scripts/setup/configure_cameras.py --phase 1       # Phase 1 cameras only
    python scripts/setup/configure_cameras.py --phase 2       # Phase 2 ANPR cameras only
    python scripts/setup/configure_cameras.py --phase all     # All cameras
"""

import sys
import os
import argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import requests
from requests.auth import HTTPDigestAuth
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

HTTP_HOST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<HttpHostNotification version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <id>1</id>
  <url>/api/v1/events/camera</url>
  <protocolType>HTTP</protocolType>
  <parameterFormatType>XML</parameterFormatType>
  <addressingFormatType>ipaddress</addressingFormatType>
  <ipAddress>{backend_ip}</ipAddress>
  <portNo>{backend_port}</portNo>
  <httpAuthenticationMethod>none</httpAuthenticationMethod>
</HttpHostNotification>"""

EVENT_TRIGGER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<EventTrigger version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <id>{event_id}</id>
  <eventType>{event_type}</eventType>
  <EventTriggerNotificationList>
    <EventTriggerNotification>
      <id>1</id>
      <notificationMethod>HTTP</notificationMethod>
      <notificationRecurrence>beginning</notificationRecurrence>
    </EventTriggerNotification>
  </EventTriggerNotificationList>
</EventTrigger>"""


# Phase 1 event types to enable on each camera
PHASE1_EVENTS = {
    "CAM-02": [("1", "fielddetection"), ("2", "linedetection"), ("3", "VMD")],
    "CAM-04": [("1", "fielddetection"), ("2", "linedetection"), ("3", "VMD")],
    "CAM-35": [("1", "regionEntrance"), ("2", "regionExiting"), ("3", "fielddetection"), ("4", "VMD")],
}

# Phase 2 ANPR event types
PHASE2_EVENTS = {
    "CAM-ENTRY": [("1", "AccessControllerEvent")],
    "CAM-EXIT":  [("1", "AccessControllerEvent")],
}


def configure_camera(cam_id: str, cam: dict, events: list):
    """Configure a single camera with HTTP push + event triggers."""
    ip = cam["ip"]
    auth = HTTPDigestAuth(cam["user"], cam["password"])
    base = f"http://{ip}"

    print(f"\n{'='*50}")
    print(f"Configuring {cam_id} ({ip})")
    print(f"{'='*50}")

    # Step 1: Set HTTP host notification
    print(f"  → Setting HTTP host → {settings.BACKEND_IP}:{settings.BACKEND_PORT}")
    try:
        xml = HTTP_HOST_XML.format(
            backend_ip=settings.BACKEND_IP,
            backend_port=settings.BACKEND_PORT,
        )
        resp = requests.put(
            f"{base}/ISAPI/Event/notification/httpHosts/1",
            data=xml.encode("utf-8"),
            auth=auth,
            headers={"Content-Type": "application/xml"},
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"  ✅ HTTP host configured")
        else:
            print(f"  ⚠️  HTTP host response: {resp.status_code}")
            print(f"      {resp.text[:200]}")
    except Exception as e:
        print(f"  ❌ Failed: {e}")
        return

    # Step 2: Enable event triggers
    for event_id, event_type in events:
        print(f"  → Enabling event: {event_type}")
        try:
            xml = EVENT_TRIGGER_XML.format(event_id=event_id, event_type=event_type)
            resp = requests.put(
                f"{base}/ISAPI/Event/triggers/{event_type}-1",
                data=xml.encode("utf-8"),
                auth=auth,
                headers={"Content-Type": "application/xml"},
                timeout=10,
            )
            if resp.status_code == 200:
                print(f"  ✅ {event_type} enabled")
            else:
                print(f"  ⚠️  {event_type}: {resp.status_code}")
        except Exception as e:
            print(f"  ❌ {event_type} failed: {e}")

    print(f"  🎉 {cam_id} configuration complete")


def main():
    parser = argparse.ArgumentParser(description="Configure Hikvision cameras for Damanat backend")
    parser.add_argument("--phase", choices=["1", "2", "all"], default="all",
                        help="Which phase cameras to configure")
    args = parser.parse_args()
    target_phases = {1, 2} if args.phase == "all" else {int(args.phase)}

    print("📷 Damanat Camera Configuration")
    print(f"   Backend: http://{settings.BACKEND_IP}:{settings.BACKEND_PORT}")
    print(f"   Phase(s): {target_phases}")

    configured = 0

    if 1 in target_phases:
        for cam_id, events in PHASE1_EVENTS.items():
            cam = settings.CAMERAS.get(cam_id)
            if cam:
                configure_camera(cam_id, cam, events)
                configured += 1

    if 2 in target_phases:
        for cam_id, events in PHASE2_EVENTS.items():
            cam = settings.CAMERAS.get(cam_id)
            if cam:
                configure_camera(cam_id, cam, events)
                configured += 1

    print(f"\n✅ Configuration complete — {configured} cameras configured")


if __name__ == "__main__":
    main()
