# app/zone_config.py
"""
Zone name constants and per-camera zone-slot mappings.

Cameras send numeric region IDs (1, 2, 3 ...) or slot labels (zone1, zone2 ...)
that cannot be renamed on the device. ZONE_CONFIG maps those slots to both a
canonical zone name and an optional real-life parking slot label, used by:
  - violation_service  (linedetection / fielddetection)
  - intrusion_service  (fielddetection / regionEntrance)

Each camera freely defines all its zone entries here regardless of event type.
"""

import json
from typing import NamedTuple
from app.config import settings


class ZoneNames:
    """Canonical zone-name constants - single source of truth."""

    class Violation:
        RESTRICTED_VIP  = "restricted-vip"
        NO_PARKING_ZONE = "no-parking-zone"
        EMERGENCY_EXIT  = "emergency-exit"
        LOADING_BAY     = "loading-bay"

    class Intrusion:
        EMERGENCY_EXIT   = "emergency-exit"
        STAFF_ONLY_AREA  = "staff-only-area"
        AFTER_HOURS_ZONE = "after-hours-zone"


class Zone(NamedTuple):
    """One camera zone slot — canonical name + optional physical parking label."""
    name: str
    slot: str | None = None


# ---------------------------------------------------------------------------
# Per-camera zone config  (replaces separate ZONE_MAPPING + ZONE_REAL_SLOT)
#
# Keys   : camera_id  (must match keys in settings.CAMERAS / CAMERA_IP_MAP)
# Values : zone-slot  -> Zone(name=..., slot=...)
#
# Slots arrive as "1"/"2"/... or "zone1"/"zone2"/... - both handled by resolve_zone().
# Both violation_service and intrusion_service read from this same config.
# ---------------------------------------------------------------------------

ZONE_CONFIG: dict[str, dict[str, Zone]] = {

    # ── Violation cameras (GF / B1 / B2 entrances, exits, line-crossing) ─────

    # CAM-01 · GF-ENTRANCE-INTERNAL
    "CAM-01": {
        "zone1": Zone(name=ZoneNames.Intrusion.EMERGENCY_EXIT,   slot="G1"),
        "zone2": Zone(name=ZoneNames.Violation.EMERGENCY_EXIT,   slot="G2"),
        "zone3": Zone(name=ZoneNames.Violation.NO_PARKING_ZONE),
        "zone4": Zone(name=ZoneNames.Violation.LOADING_BAY),
    },
    # CAM-02 · GF-WAITING
    "CAM-02": {
        "zone1": Zone(name=ZoneNames.Violation.RESTRICTED_VIP,   slot="G7"),
        "zone2": Zone(name=ZoneNames.Violation.NO_PARKING_ZONE,  slot="G6"),
        "zone3": Zone(name=ZoneNames.Violation.LOADING_BAY,      slot="G5"),
        "zone4": Zone(name=ZoneNames.Violation.EMERGENCY_EXIT),
    },
    # CAM-03 · B1-ENTRANCE-INTERNAL
    "CAM-03": {
        "zone1": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE, slot="B11"),
        "zone2": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE, slot="B10"),
        "zone3": Zone(name=ZoneNames.Violation.RESTRICTED_VIP),
        "zone4": Zone(name=ZoneNames.Violation.EMERGENCY_EXIT),
    },
    # CAM-04 · B1-PARKING
    "CAM-04": {
        "zone1": Zone(name=ZoneNames.Violation.RESTRICTED_VIP,   slot="B3"),
        "zone2": Zone(name=ZoneNames.Violation.RESTRICTED_VIP,   slot="B2"),
        "zone3": Zone(name=ZoneNames.Violation.EMERGENCY_EXIT,   slot="B1"),
        "zone4": Zone(name=ZoneNames.Violation.LOADING_BAY),
    },

    # ── Intrusion cameras (field detection / after-hours zones) ─────────────

    "CAM-05": {
        "zone1": Zone(name=ZoneNames.Intrusion.EMERGENCY_EXIT),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
    },
    "CAM-06": {
        "zone1": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE, slot="B6"),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
    },
    "CAM-07": {
        "zone1": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE, slot="B9"),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
    },
    "CAM-08": {
        "zone1": Zone(name=ZoneNames.Intrusion.EMERGENCY_EXIT,   slot="B5"),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA,  slot="B13"),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
    },
    "CAM-09": {
        "zone1": Zone(name=ZoneNames.Intrusion.EMERGENCY_EXIT,   slot="B24"),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA,  slot="B25"),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
    },
    "CAM-10": {
        "zone1": Zone(name=ZoneNames.Intrusion.EMERGENCY_EXIT),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
    },
    "CAM-11": {
        "zone1": Zone(name=ZoneNames.Intrusion.EMERGENCY_EXIT,   slot="B18"),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA,  slot="B17"),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE, slot="B20"),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE, slot="B19"),
    },
    "CAM-12": {
        "zone1": Zone(name=ZoneNames.Intrusion.EMERGENCY_EXIT),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA,  slot="B23"),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE, slot="B21"),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
    },
    "CAM-13": {
        "zone1": Zone(name=ZoneNames.Intrusion.EMERGENCY_EXIT,   slot="B22"),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
    },
    "CAM-14": {
        "zone1": Zone(name=ZoneNames.Intrusion.EMERGENCY_EXIT,   slot="B27"),
        "zone2": Zone(name=ZoneNames.Intrusion.STAFF_ONLY_AREA),
        "zone3": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
        "zone4": Zone(name=ZoneNames.Intrusion.AFTER_HOURS_ZONE),
    },
}

CAMERA_REGION_ZONE_MAP: dict[tuple[str, int], str] = {
    # Example:
    # ("CAM-04", 0): "emergency-exit",
}


def _normalize_region_id(region_id: str | int | None) -> int | None:
    if region_id is None:
        return None
    if isinstance(region_id, int):
        return region_id
    if isinstance(region_id, str) and region_id.strip().isdigit():
        return int(region_id.strip())
    return None


def _parse_camera_region_zone_map(raw: str) -> dict[tuple[str, int], str]:
    """
    Parse env mapping into {(camera_id, region_id): zone_name}.
    Supported formats:
      1) JSON:
         {"CAM-04": {"0": "emergency-exit", "1": "restricted-vip"}}
         {"CAM-04:0": "emergency-exit", "CAM-04:1": "restricted-vip"}
      2) Delimited:
         CAM-04:0=emergency-exit;CAM-04:1=restricted-vip
    """
    mapping: dict[tuple[str, int], str] = {}
    if not raw:
        return mapping

    raw = raw.strip()
    if not raw:
        return mapping

    def add_entry(camera_id: str, region_id: str | int, zone_name: str):
        cam = camera_id.strip()
        zone = zone_name.strip()
        rid = _normalize_region_id(region_id)
        if cam and zone and rid is not None:
            mapping[(cam, rid)] = zone

    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:
            data = None

        if isinstance(data, dict):
            for cam_key, value in data.items():
                if isinstance(value, dict):
                    for rid_key, zone in value.items():
                        if isinstance(zone, str):
                            add_entry(cam_key, rid_key, zone)
                elif isinstance(value, str):
                    if ":" in cam_key:
                        cam, rid = cam_key.split(":", 1)
                        add_entry(cam, rid, value)
        return mapping

    parts = [p for p in raw.replace(",", ";").split(";") if p.strip()]
    for part in parts:
        if "=" not in part or ":" not in part:
            continue
        left, zone = part.split("=", 1)
        cam, rid = left.split(":", 1)
        add_entry(cam, rid, zone)

    return mapping


def resolve_region_zone(camera_id: str, region_id: str | int | None) -> str | None:
    """Resolve (camera_id, region_id) -> canonical zone name."""
    rid = _normalize_region_id(region_id)
    if rid is None:
        return None

    mapping = dict(CAMERA_REGION_ZONE_MAP)
    mapping.update(_parse_camera_region_zone_map(settings.CAMERA_REGION_ZONE_MAP))
    return mapping.get((camera_id, rid))



def resolve_zone(camera_id: str, raw_zone_id: str | None) -> str | None:
    """
    Translate a camera-reported zone slot to its canonical zone name.
    Works for both violation and intrusion events.

    Args:
        camera_id:   e.g. "CAM-04"
        raw_zone_id: slot sent by camera - "zone1", "1", etc., or None

    Returns:
        Canonical zone name string, or None if not mapped.
    """
    if raw_zone_id is None:
        return None

    # First, try numeric region mapping (CAMERA_REGION_ZONE_MAP)
    region_zone = resolve_region_zone(camera_id, raw_zone_id)
    if region_zone:
        return region_zone

    cam_zones = ZONE_CONFIG.get(camera_id, {})

    # Direct lookup ("zone1" style)
    if raw_zone_id in cam_zones:
        return cam_zones[raw_zone_id].name

    # Numeric fallback: "1" -> "zone1"
    numeric_key = f"zone{raw_zone_id}"
    if numeric_key in cam_zones:
        return cam_zones[numeric_key].name

    return None


def get_real_slot_number(camera_id: str, raw_zone_id: str | None) -> str | None:
    """
    Return the real-life parking bay label for a given camera zone slot.

    Looks up ZONE_CONFIG using the same slot-normalisation logic as
    resolve_zone() — accepts both "zone1" and "1" style inputs.

    Returns:
        str label (e.g. "B3", "G7") if configured, None otherwise.
    """
    if raw_zone_id is None:
        return None

    cam_zones = ZONE_CONFIG.get(camera_id, {})

    # Direct lookup ("zone1" style)
    if raw_zone_id in cam_zones:
        return cam_zones[raw_zone_id].slot

    # Numeric fallback: "1" → "zone1"
    numeric_key = f"zone{raw_zone_id}"
    if numeric_key in cam_zones:
        return cam_zones[numeric_key].slot

    return None