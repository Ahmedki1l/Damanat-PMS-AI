# scripts/setup/configure_cameras.py
"""
Configure Hikvision cameras to push events to the Damanat backend.
Sets up HTTP host notification and smart detection targets via ISAPI.
"""

import sys
import os
import argparse
import requests
import re as _re
from requests.auth import HTTPDigestAuth

# Add project root to sys.path to allow importing app settings
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

# XML Template to link the camera to the Backend
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

# ISAPI Endpoints for each event type
SMART_EVENT_ENDPOINT = {
    "fielddetection":  "/ISAPI/Smart/FieldDetection/1",
    "linedetection":   "/ISAPI/Smart/LineDetection/1",
    "regionEntrance":  "/ISAPI/Smart/RegionEntrance/1",
    "regionExiting":   "/ISAPI/Smart/RegionExiting/1",
}

def _patch_detection_xml(xml_text: str) -> str:
    """Modify camera XML to enable detection and set target to 'vehicle' only."""
    # Enable the detection rule (Set Enabled = true)
    xml_out = _re.sub(r"<enabled>.*?</enabled>", "<enabled>true</enabled>", xml_text, flags=_re.S)
    
    # Set detectionTarget to 'vehicle' to reduce false alarms from humans or animals
    if "<detectionTarget>" in xml_out:
        xml_out = _re.sub(r"<detectionTarget>.*?</detectionTarget>",
                          "<detectionTarget>vehicle</detectionTarget>",
                          xml_out, flags=_re.S)
    else:
        xml_out = xml_out.replace(
            "<enabled>true</enabled>",
            "<enabled>true</enabled>\n  <detectionTarget>vehicle</detectionTarget>",
            1
        )
    return xml_out

# Define cameras and events for each phase
PHASE1_EVENTS = {
    "CAM-02": ["fielddetection", "linedetection"],
    "CAM-04": ["fielddetection", "linedetection"],
    "CAM-35": ["fielddetection", "linedetection"],
    "CAM-12": ["fielddetection", "linedetection"],
    "CAM-13": ["fielddetection", "linedetection"],
    "CAM-14": ["fielddetection", "linedetection"],
    "CAM-11": ["fielddetection", "linedetection"],
    "CAM-10": ["fielddetection", "linedetection"],
    "CAM-09": ["fielddetection", "linedetection"],
    "CAM-07": ["fielddetection", "linedetection"],
    "CAM-06": ["fielddetection", "linedetection"],
    "CAM-05": ["fielddetection", "linedetection"],
}

PHASE2_EVENTS = {
    "CAM-ENTRY": ["AccessControllerEvent"], # ANPR Events
    "CAM-EXIT":  ["AccessControllerEvent"], # ANPR Events
}

def configure_camera(cam_id: str, cam: dict, events: list):
    """Apply configuration settings to a single camera."""
    ip = cam["ip"]
    auth = HTTPDigestAuth(cam["user"], cam["password"])
    base = f"http://{ip}"

    print(f"\n{'='*50}\nConfiguring {cam_id} ({ip})\n{'='*50}")

    # Step 1: Link camera to the Backend (HTTP Host)
    print(f"  → Setting HTTP host → {settings.BACKEND_IP}:{settings.BACKEND_PORT}")
    try:
        xml = HTTP_HOST_XML.format(backend_ip=settings.BACKEND_IP, backend_port=settings.BACKEND_PORT)
        resp = requests.put(f"{base}/ISAPI/Event/notification/httpHosts/1", 
                            data=xml.encode("utf-8"), auth=auth, timeout=10)
        if resp.status_code == 200:
            print(f"  ✅ HTTP host configured")
        else:
            print(f"  ⚠️  HTTP host response: {resp.status_code}")
    except Exception as e:
        print(f"  ❌ Failed: {e}"); return

    # Step 2: Enable Smart Detection rules
    for event_type in events:
        if event_type == "AccessControllerEvent":
            print(f"  ℹ️  {event_type}: Push-only (ready via HTTP host)")
            continue

        endpoint = SMART_EVENT_ENDPOINT.get(event_type)
        if not endpoint: continue

        print(f"  → Enabling event: {event_type}")
        try:
            # GET current config -> Patch it -> PUT it back
            get_resp = requests.get(f"{base}{endpoint}", auth=auth, timeout=10)
            if get_resp.status_code == 200:
                patched_xml = _patch_detection_xml(get_resp.text)
                put_resp = requests.put(f"{base}{endpoint}", data=patched_xml.encode("utf-8"), auth=auth, timeout=10)
                if put_resp.status_code == 200:
                    print(f"  ✅ {event_type} enabled (vehicle target)")
        except Exception as e:
            print(f"  ❌ {event_type} failed: {e}")

def main():
    parser = argparse.ArgumentParser(description="Configure Hikvision cameras for Damanat")
    parser.add_argument("--phase", choices=["1", "2", "all"], default="all")
    args = parser.parse_args()
    
    target_phases = {1, 2} if args.phase == "all" else {int(args.phase)}
    configured_count = 0

    if 1 in target_phases:
        for cam_id, events in PHASE1_EVENTS.items():
            cam = settings.CAMERAS.get(cam_id)
            if cam: 
                configure_camera(cam_id, cam, events)
                configured_count += 1

    if 2 in target_phases:
        for cam_id, events in PHASE2_EVENTS.items():
            cam = settings.CAMERAS.get(cam_id)
            if cam: 
                configure_camera(cam_id, cam, events)
                configured_count += 1

    print(f"\n✅ Configuration complete — {configured_count} cameras configured")

if __name__ == "__main__":
    main()
