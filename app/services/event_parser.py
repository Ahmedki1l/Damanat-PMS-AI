# app/services/event_parser.py
"""
Parses both Phase 1 (XML) and Phase 2 (JSON/ANPR) camera event payloads.
Returns a unified ParsedCameraEvent regardless of source.
Handles multipart/form-data for image extraction.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Namespaces commonly used by Hikvision ISAPI
NS_ISAPI = "http://www.isapi.org/ver20/XMLSchema"
NS_HIKVISION = "http://www.hikvision.com/ver20/XMLSchema"

SNAPSHOT_DIR = "detection_images"
os.makedirs(SNAPSHOT_DIR, exist_ok=True)


@dataclass
class ParsedCameraEvent:
    camera_id: str
    device_serial: str
    channel_id: int
    event_type: str            # fielddetection | linedetection | regionEntrance | regionExiting | AccessControllerEvent
    detection_target: Optional[str]  # vehicle | human | others (Phase 1)
    region_id: Optional[str]
    channel_name: Optional[str]
    trigger_time: datetime
    raw_xml: str
    # Extra XML fields
    event_state: Optional[str] = None      # active | inactive
    event_description: Optional[str] = None  # human-readable description
    snapshot_path: Optional[str] = None    # path to saved snapshot image
    # Phase 2 ANPR fields
    plate_number: Optional[str] = None     # cardNo from AccessControllerEvent
    user_type: Optional[str] = None        # normal | visitor | blacklist
    person_name: Optional[str] = None
    employee_id: Optional[str] = None
    gate: Optional[str] = None             # entry | exit (from settings mapping)
    crossing_direction: Optional[str] = None # A-to-B | B-to-A



def _split_multipart(raw_body: bytes, content_type: str) -> List[Dict]:
    """Split multipart body into a list of {headers, body, content_type, filename} dicts."""
    match = re.search(r"boundary=([^\s;]+)", content_type)
    if not match:
        logger.warning("Multipart content-type but no boundary found")
        return []

    boundary = match.group(1).encode()
    # Boundaries in body start with --
    raw_parts = raw_body.split(b"--" + boundary)
    result = []

    for part in raw_parts:
        part = part.strip()
        if not part or part == b"--":
            continue

        # Split headers from body
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


def _extract_from_multipart(raw_body: bytes, content_type: str, camera_id: str, event_type: str) -> tuple[bytes, Optional[str]]:
    """
    Extract the XML/JSON payload from a multipart body.
    Also saves any included JPEGs to the snapshot directory.
    Returns (metadata_body, saved_image_path).
    """
    parts = _split_multipart(raw_body, content_type)
    content_body = raw_body
    image_path = None

    # Priority 1: Save images first
    for p in parts:
        ct = p["content_type"]
        if "image/" in ct.lower():
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"part_{event_type}_{camera_id}_{timestamp}.jpg"
            filepath = os.path.join(SNAPSHOT_DIR, filename)
            try:
                with open(filepath, "wb") as f:
                    f.write(p["body"])
                image_path = filepath
                logger.debug(f"Saved multipart image: {filename} ({len(p['body'])} bytes)")
                # We stop at the first image for now
                break
            except Exception as e:
                logger.error(f"Failed to save multipart image: {e}")

    # Priority 2: Return XML or JSON parts
    for p in parts:
        ct = p["content_type"]
        if any(t in ct for t in ("text/xml", "application/xml", "application/json", "text/plain")):
            logger.debug(f"Extracted multipart metadata part ({len(p['body'])} bytes)")
            return p["body"], image_path

    # Fallback: return first part's body as content
    if parts:
        return parts[0]["body"], image_path

    return content_body, image_path


def parse_camera_event(raw_body: bytes, camera_ip: str, content_type: str = "") -> ParsedCameraEvent:
    """Auto-detect format (Multipart, JSON, or XML) and parse accordingly."""
    
    # Handle multipart/form-data (common in Hikvision events with snapshots)
    snapshot_path = None
    if "multipart" in content_type.lower():
        # First pass identification: try to get camera_id from IP to name the file properly
        temp_camera_id = settings.CAMERA_IP_MAP.get(camera_ip, f"UNKNOWN-{camera_ip}")
        raw_body, snapshot_path = _extract_from_multipart(raw_body, content_type, temp_camera_id, "multipart")

    # Detect if body is JSON or XML
    is_json = "json" in content_type.lower() or raw_body.lstrip()[:1] == b"{"
    
    if is_json:
        event = _parse_json_event(raw_body, camera_ip)
    else:
        event = _parse_xml_event(raw_body, camera_ip)

    if snapshot_path:
        # Rename "multipart" placeholder name to use actual event type for better file organization
        if "multipart" in snapshot_path:
            correct_filename = snapshot_path.replace("multipart", event.event_type)
            try:
                os.rename(snapshot_path, correct_filename)
                snapshot_path = correct_filename
            except Exception as e:
                logger.warning(f"Could not rename multipart image from {snapshot_path}: {e}")
        
        event.snapshot_path = snapshot_path
    
    return event


def _parse_xml_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 1 XML events using ElementTree with full Namespace support."""
    xml_str = raw_body.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        logger.error(f"Failed to parse XML: {e}")
        raise

    # Auto-detect namespace from root tag
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    logger.debug(f"XML namespace detected: {ns or '(none)'}")

    def find_text(tag, parent=root):
        """Helper to find text in a tag considering namespaces."""
        if ns:
            el = parent.find(f"{ns}{tag}")
            if el is not None:
                return el.text.strip() if el.text else None
        el = parent.find(tag)
        return el.text.strip() if el is not None and el.text else None

    # Resolve Trigger Time
    trigger_time = datetime.utcnow()
    t_str = find_text("triggerTime") or find_text("dateTime")
    if t_str:
        try:
            # Handle ISO format and Z suffix
            trigger_time = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
        except Exception:
            logger.warning(f"Could not parse timestamp: {t_str}")

    # Resolve Regions and Targets (UC3, UC6)
    region_id, detection_target = None, None
    reg_list = root.find(f"{ns}DetectionRegionList") if ns else root.find("DetectionRegionList")
    if reg_list is not None:
        entry = reg_list.find(f"{ns}DetectionRegionEntry") if ns else reg_list.find("DetectionRegionEntry")
        if entry is not None:
            region_id = find_text("regionID", entry)
            detection_target = find_text("detectionTarget", entry)

    # Fallback: real cameras nest regionID inside DetectionRegionList, but simple test
    # XMLs (and some older firmware) put it at the top level — support both.
    if region_id is None:
        region_id = find_text("regionID")
    if detection_target is None:
        detection_target = find_text("detectionTarget")

    # Prefer the <ipAddress> from XML body over the HTTP request's client IP.
    # This allows manual testing from localhost while still correctly identifying the camera.
    xml_ip = find_text("ipAddress") or camera_ip
    resolved_camera_id = settings.CAMERA_IP_MAP.get(xml_ip) \
        or settings.CAMERA_IP_MAP.get(camera_ip) \
        or f"UNKNOWN-{camera_ip}"

    # Resolve ANPR Plate (UC1, UC2, UC4)
    plate_number = find_text("licensePlateNumber") or find_text("plateNumber")
    
    # If it's an ANPR event, we should try to extract more info
    if (find_text("eventType") in ("ANPR", "vehicleMatchResult", "AccessControllerEvent")):
         # Robust plate search: check multiple common Hikvision tags
         if not plate_number:
            plate_number = find_text("licensePlateNumber") or find_text("plateNumber") or find_text("cardNo")
         
         # Try looking inside nested nodes like <ANPR> or <AccessControllerEvent>
         for node_name in ("ANPR", "AccessControllerEvent"):
             node = root.find(f"{ns}{node_name}") if ns else root.find(node_name)
             if node is not None and not plate_number:
                 plate_number = find_text("licensePlateNumber", node) or find_text("plateNumber", node) or find_text("cardNo", node)
         
         if not plate_number:
             logger.debug(f"[DEBUG-ANPR] Plate not found in XML. Payload head (500 chars): {xml_str[:500]}")
         else:
             logger.info(f"[DEBUG-ANPR] Extracted plate: {plate_number}")

    event_type = find_text("eventType") or "unknown"
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

        if plate_number:
            plate_number = _normalize_plate(plate_number)
            logger.info(f"[ANPR] Normalized plate: {plate_number} ({camera_id})")
        else:
            logger.warning(
                f"[ANPR-DEBUG] No plate found in any known path for {event_type} from {camera_id}"
            )

    return ParsedCameraEvent(
        camera_id=resolved_camera_id,
        device_serial=find_text("deviceSerial") or "unknown",
        channel_id=int(find_text("channelID") or 1),
        event_type=find_text("eventType") or "unknown",
        detection_target=detection_target or ("vehicle" if find_text("eventType") in ("ANPR", "vehicleMatchResult") else None),
        region_id=region_id,
        channel_name=find_text("channelName"),
        trigger_time=trigger_time,
        raw_xml=xml_str,
        event_state=find_text("eventState"),
        event_description=find_text("eventDescription"),
        plate_number=plate_number,
        crossing_direction=find_text("direction") or find_text("crossingDirection"),
    )


def _normalize_plate(raw: str) -> str:
    """
    Normalize Saudi plate format from camera output to display format.
    Examples: "9444HUD" → "HUD-9444",  "7800ERD" → "ERD-7800"
    Returns None for unknown/partial/invalid plates.
    """
    if not raw:
        return None
    raw = raw.strip().upper()

    # Camera explicitly couldn't read the plate
    if raw in ("UNKNOWN", "N/A", "NONE", "NULL", ""):
        return None

    # Already normalised (e.g. "HUD-9444") — validate full format 3 letters + 4 digits
    if "-" in raw:
        m = re.match(r"^([A-Z]{2,3})-(\d{3,4})$", raw)
        return raw if m else None

    # Saudi format: 1-4 digits then 2-3 letters  e.g. "9444HUD", "7HDU"
    m = re.match(r"^(\d{1,4})([A-Z]{2,3})$", raw)
    if m:
        return f"{m.group(2)}-{m.group(1)}"

    # Letters then digits  e.g. "HUD9444", "HDU7"
    m = re.match(r"^([A-Z]{2,3})(\d{1,4})$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # Partial or unrecognised — reject
    logger.debug(f"[ANPR] Rejected partial/invalid plate: {raw!r}")
    return None


def _parse_json_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 2 JSON events (AccessControllerEvent / vehicleMatchResult from ANPR cameras)."""
    try:
        data = json.loads(raw_body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        raise

    # Resolve Trigger Time
    trigger_time = datetime.utcnow()
    dt_str = data.get("dateTime", "")
    if dt_str:
        try:
            trigger_time = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except Exception:
            pass

    acs = data.get("AccessControllerEvent", {})
    vmr = data.get("VehicleMatchResult", {})

    # Prefer the ipAddress declared inside the JSON body over the HTTP source IP.
    # This handles NAT/proxy scenarios where the HTTP client IP differs from the camera IP.
    json_ip = data.get("ipAddress") or camera_ip
    camera_id = (
        settings.CAMERA_IP_MAP.get(json_ip)
        or settings.CAMERA_IP_MAP.get(camera_ip)
        or f"UNKNOWN-{camera_ip}"
    )

    # Determine gate direction from camera config in settings
    cam_config = settings.CAMERAS.get(camera_id, {})
    gate = cam_config.get("gate")  # "entry" or "exit"

    # Extract plate — check both event structures:
    #   AccessControllerEvent → cardNo
    #   VehicleMatchResult    → PlateInfo.plate
    raw_plate = (
        acs.get("cardNo")
        or vmr.get("PlateInfo", {}).get("plate")
    )
    plate_number = _normalize_plate(raw_plate) if raw_plate else None

    if plate_number:
        logger.info(f"[ANPR] Extracted plate: {raw_plate!r} → {plate_number!r} ({camera_id})")
    else:
        logger.warning(f"[ANPR] No plate found in JSON event from {camera_id}")

    return ParsedCameraEvent(
        camera_id=camera_id,
        device_serial=data.get("deviceSerial", data.get("deviceID", "unknown")),
        channel_id=data.get("channelID", 1),
        event_type=data.get("eventType", "unknown"),
        detection_target="vehicle",
        region_id=gate,
        channel_name=acs.get("deviceName") or data.get("channelName"),
        trigger_time=trigger_time,
        raw_xml=raw_body.decode("utf-8", errors="replace"),
        plate_number=plate_number,
        user_type=acs.get("userType") or vmr.get("listType"),
        person_name=acs.get("name"),
        employee_id=acs.get("employeeNoString"),
        gate=gate,
    )