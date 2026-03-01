# app/services/event_parser.py
"""
Parses both Phase 1 (XML) and Phase 2 (JSON/ANPR) camera event payloads.
Returns a unified ParsedCameraEvent regardless of source.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import xml.etree.ElementTree as ET
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
# Support both common Hikvision/ISAPI namespaces
NAMESPACES = [
    "http://www.isapi.org/ver20/XMLSchema",
    "http://www.hikvision.com/ver20/XMLSchema"
]


@dataclass
class ParsedCameraEvent:
    camera_id: str
    device_serial: str
    channel_id: int
    event_type: str          # fielddetection | linedetection | regionEntrance | regionExiting | VMD | AccessControllerEvent
    detection_target: Optional[str]  # vehicle | human | others (Phase 1)
    region_id: Optional[str]
    channel_name: Optional[str]
    trigger_time: datetime
    raw_xml: str
    # Phase 2 ANPR fields
    plate_number: Optional[str] = None    # cardNo from AccessControllerEvent
    user_type: Optional[str] = None       # normal | visitor | blacklist
    person_name: Optional[str] = None
    employee_id: Optional[str] = None
    gate: Optional[str] = None            # entry | exit (from camera config)


def parse_camera_event(raw_body: bytes, camera_ip: str, content_type: str = "") -> ParsedCameraEvent:
    """Auto-detect format and parse accordingly."""
    is_json = "json" in content_type.lower() or raw_body.lstrip()[:1] == b"{"
    if is_json:
        return _parse_json_event(raw_body, camera_ip)
    else:
        return _parse_xml_event(raw_body, camera_ip)


def _parse_xml_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 1 XML events (fielddetection, regionEntrance, etc.)"""
    xml_str = raw_body.decode("utf-8", errors="replace")
    root = ET.fromstring(xml_str)

    def find(tag):
        for ns in NAMESPACES:
            el = root.find(f"{{{ns}}}{tag}")
            if el is not None: return el.text.strip() if el.text else None
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text else None

    def find_in(parent, tag):
        for ns in NAMESPACES:
            el = parent.find(f"{{{ns}}}{tag}")
            if el is not None: return el.text.strip() if el.text else None
        el = parent.find(tag)
        return el.text.strip() if el is not None and el.text else None

    trigger_time = datetime.utcnow()
    t = find("triggerTime") or find("dateTime")
    if t:
        try:
            trigger_time = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except Exception:
            pass

    region_id, detection_target = None, None
    region_list = None
    for ns in NAMESPACES:
        region_list = root.find(f"{{{ns}}}DetectionRegionList")
        if region_list is not None: break
    if region_list is None:
        region_list = root.find("DetectionRegionList")

    if region_list is not None:
        entry = None
        for ns in NAMESPACES:
            entry = region_list.find(f"{{{ns}}}DetectionRegionEntry")
            if entry is not None: break
        if entry is None:
            entry = region_list.find("DetectionRegionEntry")

        if entry is not None:
            region_id = find_in(entry, "regionID")
            detection_target = find_in(entry, "detectionTarget")

    return ParsedCameraEvent(
        camera_id=settings.CAMERA_IP_MAP.get(camera_ip, f"UNKNOWN-{camera_ip}"),
        device_serial=find("deviceSerial") or "unknown",
        channel_id=int(find("channelID") or 1),
        event_type=find("eventType") or "unknown",
        detection_target=detection_target,
        region_id=region_id,
        channel_name=find("channelName"),
        trigger_time=trigger_time,
        raw_xml=xml_str,
    )


def _parse_json_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 2 JSON events (AccessControllerEvent from ANPR cameras)."""
    data = json.loads(raw_body.decode("utf-8", errors="replace"))

    trigger_time = datetime.utcnow()
    dt = data.get("dateTime", "")
    if dt:
        try:
            trigger_time = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            pass

    acs = data.get("AccessControllerEvent", {})
    camera_id = settings.CAMERA_IP_MAP.get(camera_ip, f"UNKNOWN-{camera_ip}")

    # Determine gate direction from camera config
    cam_config = settings.CAMERAS.get(camera_id, {})
    gate = cam_config.get("gate")  # "entry" or "exit"

    return ParsedCameraEvent(
        camera_id=camera_id,
        device_serial=data.get("deviceSerial", data.get("deviceID", "unknown")),
        channel_id=data.get("channelID", 1),
        event_type=data.get("eventType", "unknown"),
        detection_target="vehicle",   # ANPR events are always vehicle
        region_id=gate,               # gate = entry | exit
        channel_name=acs.get("deviceName"),
        trigger_time=trigger_time,
        raw_xml=raw_body.decode("utf-8", errors="replace"),
        # ANPR-specific
        plate_number=acs.get("cardNo"),
        user_type=acs.get("userType"),
        person_name=acs.get("name"),
        employee_id=acs.get("employeeNoString"),
        gate=gate,
    )
