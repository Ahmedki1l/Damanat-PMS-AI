# app/services/event_parser.py
"""
Parses both Phase 1 (XML) and Phase 2 (JSON/ANPR) camera event payloads.
Returns a unified ParsedCameraEvent regardless of source.
Handles multipart/form-data for image extraction.
"""

import json
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Optional, List, Dict
from app.config import settings, facility_now_naive
from app.services.snapshot_service import to_public_snapshot_url
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
    local_snapshot_path: Optional[str] = None  # original local file (preserved for PMS forwarding)
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
            timestamp = facility_now_naive().strftime("%Y%m%d_%H%M%S_%f")
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
        # Rename the placeholder image file to use actual event type and resolved camera_id
        if "multipart" in snapshot_path:
            # We reconstruct the filename because temp_camera_id might have been UNKNOWN if IP was masked
            timestamp = facility_now_naive().strftime("%Y%m%d_%H%M%S_%f")
            correct_filename = os.path.join(SNAPSHOT_DIR, f"snapshot_{event.event_type}_{event.camera_id}_{timestamp}.jpg")
            try:
                os.rename(snapshot_path, correct_filename)
                snapshot_path = correct_filename
            except Exception as e:
                logger.warning(f"Could not rename multipart image from {snapshot_path}: {e}")

        # snapshot_path on the event is the publicly-fetchable URL — that's
        # what gets persisted into entry_exit_log / parking_sessions / alerts.
        # local_snapshot_path keeps the on-disk filename so downstream code
        # (e.g. PMS tracking API forwarding) can read the file directly.
        event.local_snapshot_path = snapshot_path
        event.snapshot_path = to_public_snapshot_url(snapshot_path)

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
    trigger_time = facility_now_naive()
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
    device_serial = find_text("deviceSerial") or "unknown"
    resolved_camera_id = settings.CAMERA_SERIAL_MAP.get(device_serial) \
        or settings.CAMERA_IP_MAP.get(xml_ip) \
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
         
         pass  # plate search continues below in the ANPR extraction block

    event_type = find_text("eventType") or "unknown"
    camera_id = resolved_camera_id



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
                break

        if plate_number:
            plate_number = _normalize_plate(plate_number)
            logger.debug(f"[ANPR] Normalized plate: {plate_number} ({camera_id})")
        else:
            logger.debug(f"[ANPR] No plate found in XML for {event_type} from {camera_id}")

    crossing_direction = find_text("direction") or find_text("crossingDirection")

    # Diagnostic: log parsed linedetection fields so we can verify
    # that region_id and crossing_direction are correctly extracted.
    parsed_event_type = find_text("eventType") or "unknown"
    if parsed_event_type == "linedetection":
        logger.info(
            f"[Parser] {resolved_camera_id} | linedetection | "
            f"region_id={region_id!r} | direction={crossing_direction!r} | "
            f"target={detection_target!r}"
        )

    return ParsedCameraEvent(
        camera_id=resolved_camera_id,
        device_serial=find_text("deviceSerial") or "unknown",
        channel_id=int(find_text("channelID") or 1),
        event_type=parsed_event_type,
        detection_target=detection_target or ("vehicle" if parsed_event_type in ("ANPR", "vehicleMatchResult") else None),
        region_id=region_id,
        channel_name=find_text("channelName"),
        trigger_time=trigger_time,
        raw_xml=xml_str,
        event_state=find_text("eventState"),
        event_description=find_text("eventDescription"),
        plate_number=plate_number,
        crossing_direction=crossing_direction,
    )


def _normalize_plate(raw: str) -> Optional[str]:
    """
    Canonicalize an ANPR plate read so the SAME physical plate always yields the
    SAME string. Entry/exit dedup, open-session matching, and vehicle lookup all
    compare ``plate_number`` by exact string equality, so any drift in
    separators, spacing, or case silently creates duplicate sessions / vehicle
    rows (the "registered twice" bug).

    Canonical form: upper-cased LETTERS-DIGITS with a SINGLE ``-`` between the
    letter-run and the digit-run, regardless of the order the camera reported.
    The DB convention is letters-first ("HUD-9444"); the frontend flips it to
    digits-first ("9444-HUD") for display. Storing one consistent order is what
    fixes the "registered twice" duplicates, so "9444HUD", "9444 HUD",
    "9444-HUD", "HUD9444", and "HUD-9444" ALL canonicalize to "HUD-9444".

    Returns None for reads that are not plausibly a plate:
      - explicit failure tokens (N/A / NONE / NULL / UNKNOWN / empty)
      - reads missing EITHER a letter or a digit — a real plate always has both,
        so OCR garbage like "6466466" or "1211" is rejected rather than stored
        (storing it is what produced the "plate mismatch" records on the
        dashboard, where the saved number didn't match the snapshot).
      - reads with an implausible length, or interleaved layouts that aren't a
        single digit-run + letter-run — also OCR failures.
    """
    if not raw:
        return None
    raw = raw.strip().upper()

    # Camera explicitly couldn't read the plate (and a bare "UNKNOWN" sentinel).
    if raw in ("N/A", "NONE", "NULL", "", "UNKNOWN"):
        return None

    # Fold non-ASCII Unicode digits (e.g. Arabic-Indic ٠-٩, U+0660–0669) to ASCII
    # BEFORE the [^A-Z0-9] cleanup — otherwise a plate whose digits the camera
    # reported in Arabic-Indic numerals would be stripped to letters-only and
    # wrongly rejected. (Python's str.isdigit() already treats them as digits.)
    raw = "".join(
        str(unicodedata.digit(c)) if (not c.isascii() and c.isdigit()) else c
        for c in raw
    )

    # Plate "core" = letters and digits only (drop dashes, spaces, dots, …).
    core = re.sub(r"[^A-Z0-9]", "", raw)
    has_alpha = any(c.isalpha() for c in core)
    has_digit = any(c.isdigit() for c in core)

    # Reject OCR misreads: a real plate has BOTH letters and digits and a sane
    # length (Saudi plates are ~5–7 alphanumerics; cap at 8 for slack). This
    # drops all-digit garbage ("6466466", "1211") and concatenated misreads
    # ("99999999HUD") rather than storing them as fake plates.
    if not (3 <= len(core) <= 8) or not (has_alpha and has_digit):
        logger.debug(f"[ANPR] Rejected implausible plate: {raw!r}")
        return None

    # Canonical LETTERS-DIGITS, regardless of the order the camera reported.
    m = re.match(r"^(\d+)([A-Z]+)$", core)   # digits then letters — e.g. "9444HUD"
    if m:
        return f"{m.group(2)}-{m.group(1)}"   # -> "HUD-9444"
    m = re.match(r"^([A-Z]+)(\d+)$", core)   # letters then digits — e.g. "HUD9444"
    if m:
        return f"{m.group(1)}-{m.group(2)}"   # -> "HUD-9444"

    # Interleaved / multi-segment (e.g. "9H4U4D") isn't a real plate layout —
    # treat it as an OCR failure rather than storing a dash-less oddball.
    logger.debug(f"[ANPR] Rejected non-plate layout: {raw!r}")
    return None


def _parse_json_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 2 JSON events (AccessControllerEvent / vehicleMatchResult from ANPR cameras)."""
    try:
        data = json.loads(raw_body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        raise

    # Resolve Trigger Time
    trigger_time = facility_now_naive()
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
    device_serial = data.get("deviceSerial", data.get("deviceID", "unknown"))
    camera_id = (
        settings.CAMERA_SERIAL_MAP.get(device_serial)
        or settings.CAMERA_IP_MAP.get(json_ip)
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
        acs.get("plateNumber")
        or acs.get("licensePlateNumber")
        or acs.get("cardNo")
        or vmr.get("PlateInfo", {}).get("plate")
        or vmr.get("PlateInfo", {}).get("plateNumber")
    )
    plate_number = _normalize_plate(raw_plate) if raw_plate else None

    if plate_number:
        logger.debug(f"[ANPR] Extracted plate: {raw_plate!r} -> {plate_number!r} ({camera_id})")
    else:
        logger.debug(f"[ANPR] No plate found in JSON event from {camera_id}")

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
