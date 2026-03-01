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


def _extract_from_multipart(raw_body: bytes, content_type: str, camera_id: str = "") -> Tuple[bytes, Optional[str]]:
    """
    Extract the XML/JSON payload and save any image attachment.
    Returns (xml_or_json_bytes, snapshot_path_or_none).
    """
    parts = _split_multipart(raw_body, content_type)
    xml_body = None
    snapshot_path = None

    for p in parts:
        ct = p["content_type"]
        # Text/XML/JSON part → the event payload
        if any(t in ct for t in ("text/xml", "application/xml", "application/json", "text/plain")):
            xml_body = p["body"]
            logger.debug(f"Extracted multipart XML/JSON part ({len(xml_body)} bytes)")

        # Image part → save to disk
        elif any(t in ct for t in ("image/jpeg", "image/png", "image/")):
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
            ext = "jpg" if "jpeg" in ct else "png" if "png" in ct else "jpg"
            filename = p.get("filename") or f"snap_{camera_id}_{timestamp}.{ext}"
            filepath = os.path.join(SNAPSHOT_DIR, filename)
            try:
                with open(filepath, "wb") as f:
                    f.write(p["body"])
                snapshot_path = filepath
                logger.info(f"[SNAPSHOT] Saved multipart image: {filename} ({len(p['body'])} bytes)")
            except Exception as e:
                logger.error(f"[SNAPSHOT] Failed to save {filename}: {e}")

    if xml_body is None:
        # Fallback: use the first part's body
        if parts:
            xml_body = parts[0]["body"]
            logger.debug(f"Multipart fallback: first part ({len(xml_body)} bytes)")
        else:
            logger.warning("Could not extract any part from multipart body")
            xml_body = raw_body

    return xml_body, snapshot_path


def parse_camera_event(raw_body: bytes, camera_ip: str, content_type: str = "") -> ParsedCameraEvent:
    """Auto-detect format and parse accordingly."""
    snapshot_path = None

    # Handle multipart/form-data: extract the XML/JSON payload + save image
    if "multipart" in content_type.lower():
        camera_id = settings.CAMERA_IP_MAP.get(camera_ip, f"UNKNOWN-{camera_ip}")
        logger.debug("Multipart payload detected, extracting content part")
        raw_body, snapshot_path = _extract_from_multipart(raw_body, content_type, camera_id)

    is_json = "json" in content_type.lower() or raw_body.lstrip()[:1] == b"{"
    if is_json:
        event = _parse_json_event(raw_body, camera_ip)
    else:
        event = _parse_xml_event(raw_body, camera_ip)

    # Attach snapshot from multipart (if any)
    if snapshot_path:
        event.snapshot_path = snapshot_path

    return event


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
        event_state=find("eventState"),
        event_description=find("eventDescription"),
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
