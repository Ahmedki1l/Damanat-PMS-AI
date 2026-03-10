# app/services/event_parser.py
"""
Parses both Phase 1 (XML) and Phase 2 (JSON/ANPR) camera event payloads.
Returns a unified ParsedCameraEvent regardless of source.
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple
import xml.etree.ElementTree as ET
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
NS_ISAPI = "http://www.isapi.org/ver20/XMLSchema"
NS_HIKVISION = "http://www.hikvision.com/ver20/XMLSchema"

SNAPSHOT_DIR = "detection_images"
os.makedirs(SNAPSHOT_DIR, exist_ok=True)


@dataclass
class ParsedCameraEvent:
    camera_id: str
    device_serial: str
    channel_id: int
    event_type: str          # fielddetection | linedetection | regionEntrance | regionExiting | AccessControllerEvent
    detection_target: Optional[str]  # vehicle | human | others (Phase 1)
    region_id: Optional[str]
    channel_name: Optional[str]
    trigger_time: datetime
    raw_xml: str
    # Extra XML fields
    event_state: Optional[str] = None     # active | inactive
    event_description: Optional[str] = None  # human-readable, e.g. "Motion alarm"
    snapshot_path: Optional[str] = None   # path to saved snapshot image
    # Phase 2 ANPR fields
    plate_number: Optional[str] = None    # cardNo from AccessControllerEvent
    user_type: Optional[str] = None       # normal | visitor | blacklist
    person_name: Optional[str] = None
    employee_id: Optional[str] = None
    gate: Optional[str] = None            # entry | exit (from camera config)


def _split_multipart(raw_body: bytes, content_type: str) -> list[dict]:
    """Split multipart body into a list of {headers, body, content_type, filename} dicts."""
    match = re.search(r"boundary=([^\s;]+)", content_type)
    if not match:
        logger.warning("Multipart content-type but no boundary found")
        return []

    boundary = match.group(1).encode()
    raw_parts = raw_body.split(b"--" + boundary)
    result = []

    for part in raw_parts:
        part = part.strip()
        if not part or part == b"--":
            continue

        if b"\r\n\r\n" in part:
            headers_block, body = part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in part:
            headers_block, body = part.split(b"\n\n", 1)
        else:
            continue

        headers_str = headers_block.decode("utf-8", errors="replace").lower()
        # Extract content-type
        ct_match = re.search(r"content-type:\s*([^\r\n;]+)", headers_str)
        part_ct = ct_match.group(1).strip() if ct_match else ""
        # Extract filename
        fn_match = re.search(r'filename="([^"]+)"', headers_str)
        filename = fn_match.group(1) if fn_match else None

        result.append({
            "headers": headers_str,
            "body": body.rstrip(b"\r\n"),
            "content_type": part_ct,
            "filename": filename,
        })

    return result


def _extract_from_multipart(raw_body: bytes, content_type: str) -> bytes:
    """Extract the XML/JSON payload from a multipart body. Image parts are skipped."""
    parts = _split_multipart(raw_body, content_type)

    for p in parts:
        ct = p["content_type"]
        if any(t in ct for t in ("text/xml", "application/xml", "application/json", "text/plain")):
            logger.debug(f"Extracted multipart XML/JSON part ({len(p['body'])} bytes)")
            return p["body"]

    # Fallback: first part's body
    if parts:
        logger.debug(f"Multipart fallback: first part ({len(parts[0]['body'])} bytes)")
        return parts[0]["body"]

    logger.warning("Could not extract any part from multipart body")
    return raw_body


def parse_camera_event(raw_body: bytes, camera_ip: str, content_type: str = "") -> ParsedCameraEvent:
    """Auto-detect format and parse accordingly."""
    # Handle multipart/form-data: extract only the XML/JSON payload
    if "multipart" in content_type.lower():
        logger.debug("Multipart payload detected, extracting content part")
        raw_body = _extract_from_multipart(raw_body, content_type)

    is_json = "json" in content_type.lower() or raw_body.lstrip()[:1] == b"{"
    if is_json:
        return _parse_json_event(raw_body, camera_ip)
    else:
        return _parse_xml_event(raw_body, camera_ip)


def _parse_xml_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 1 XML events (fielddetection, regionEntrance, etc.)"""
    xml_str = raw_body.decode("utf-8", errors="replace")
    root = ET.fromstring(xml_str)

    # Auto-detect namespace from root tag (handles both isapi.org and hikvision.com)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    logger.debug(f"XML namespace: {ns or '(none)'}")

    def find(tag):
        if ns:
            el = root.find(f"{ns}{tag}")
            if el is not None:
                return el.text.strip() if el.text else None
        el = root.find(tag)
        return el.text.strip() if el is not None and el.text else None

    def find_in(parent, tag):
        if ns:
            el = parent.find(f"{ns}{tag}")
            if el is not None:
                return el.text.strip() if el.text else None
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
    region_list = (root.find(f"{ns}DetectionRegionList") if ns else None) or root.find("DetectionRegionList")
    if region_list is not None:
        entry = (region_list.find(f"{ns}DetectionRegionEntry") if ns else None) or region_list.find("DetectionRegionEntry")
        if entry is not None:
            region_id = find_in(entry, "regionID")
            detection_target = find_in(entry, "detectionTarget")

    event_type = find("eventType") or "unknown"
    camera_id = settings.CAMERA_IP_MAP.get(camera_ip, f"UNKNOWN-{camera_ip}")

    # ── ANPR XML debug: dump full XML so we can see the actual structure ──
    if event_type in ("ANPR", "vehicleMatchResult"):
        logger.warning(
            f"[ANPR-DEBUG] camera={camera_id} ip={camera_ip} type={event_type} "
            f"--- RAW XML START ---\n{xml_str}\n--- RAW XML END ---"
        )

    # ── ANPR XML extraction: try common Hikvision plate paths ──
    plate_number = None
    gate = None
    if event_type in ("ANPR", "vehicleMatchResult"):
        detection_target = "vehicle"
        # Determine gate from camera config
        cam_config = settings.CAMERAS.get(camera_id, {})
        gate = cam_config.get("gate")
        region_id = gate  # entry | exit

        # Try all known Hikvision ANPR XML paths for plate number
        plate_paths = [
            # ANPR event: <ANPR><licensePlate>
            f".//{ns}ANPR/{ns}licensePlate" if ns else ".//ANPR/licensePlate",
            # Direct licensePlate
            f".//{ns}licensePlate" if ns else ".//licensePlate",
            # plateNumber (some firmware versions)
            f".//{ns}plateNumber" if ns else ".//plateNumber",
            # VehicleMatchResult paths
            f".//{ns}VehicleInfo/{ns}plateNumber" if ns else ".//VehicleInfo/plateNumber",
            f".//{ns}VehicleInfo/{ns}plate" if ns else ".//VehicleInfo/plate",
            # AccessControllerEvent inside XML
            f".//{ns}AccessControllerEvent/{ns}cardNo" if ns else ".//AccessControllerEvent/cardNo",
            # Generic plate/cardNo anywhere
            f".//{ns}cardNo" if ns else ".//cardNo",
            f".//{ns}plate" if ns else ".//plate",
        ]
        for path in plate_paths:
            el = root.find(path)
            if el is not None and el.text and el.text.strip():
                plate_number = el.text.strip()
                logger.info(f"[ANPR-DEBUG] Found plate '{plate_number}' at path: {path}")
                break

        if not plate_number:
            logger.warning(
                f"[ANPR-DEBUG] No plate found in any known path for {event_type} from {camera_id}"
            )

    return ParsedCameraEvent(
        camera_id=camera_id,
        device_serial=find("deviceSerial") or "unknown",
        channel_id=int(find("channelID") or 1),
        event_type=event_type,
        detection_target=detection_target,
        region_id=region_id,
        channel_name=find("channelName"),
        trigger_time=trigger_time,
        raw_xml=xml_str,
        event_state=find("eventState"),
        event_description=find("eventDescription"),
        plate_number=plate_number,
        gate=gate,
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
