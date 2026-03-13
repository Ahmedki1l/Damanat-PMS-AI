# app/zone_config.py
"""
Zone name constants and per-camera zone-slot mappings.

Cameras send numeric region IDs (1, 2, 3 ...) or slot labels (zone1, zone2 ...)
that cannot be renamed on the device. ZONE_MAPPING translates those slots ->
human-readable names used by both:
  - violation_service  (linedetection / fielddetection)
  - intrusion_service  (fielddetection / regionEntrance)

Each camera freely defines all its zone names here regardless of event type.
"""

import json
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

    class ParkingArea:
        """Named parking spots — use slot(n) to reference a specific bay number."""

        @staticmethod
        def slot(number: int) -> str:
            """Return the canonical name for parking spot *number*, e.g. slot(3) → 'parking-area-3'."""
            return f"parking-area-{number}"


# ---------------------------------------------------------------------------
# Per-camera zone mapping
#
# Keys   : camera_id  (must match keys in settings.CAMERAS / CAMERA_IP_MAP)
# Values : zone-slot  -> canonical zone name
#
# Slots arrive as "1"/"2"/... or "zone1"/"zone2"/... - both handled by resolve_zone().
# Both violation_service and intrusion_service read from this same mapping.
# ---------------------------------------------------------------------------

ZONE_MAPPING: dict[str, dict[str, str]] = {

    # ── Violation cameras (GF / B1 / B2 entrances, exits, line-crossing) ─────

    # CAM-01 · GF-ENTRANCE-INTERNAL
    "CAM-01": {
        "zone1": ZoneNames.Intrusion.EMERGENCY_EXIT,
        "zone2": ZoneNames.Violation.EMERGENCY_EXIT,
        "zone3": ZoneNames.Violation.NO_PARKING_ZONE,
        "zone4": ZoneNames.Violation.LOADING_BAY,
    },
    # CAM-02 · GF-WAITING
    "CAM-02": {
        "zone1": ZoneNames.Violation.RESTRICTED_VIP,
        "zone2": ZoneNames.Violation.NO_PARKING_ZONE,
        "zone3": ZoneNames.Violation.LOADING_BAY,
        "zone4": ZoneNames.Violation.EMERGENCY_EXIT,
    },
    # CAM-03 · B1-ENTRANCE-INTERNAL
    "CAM-03": {
        "zone1": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone2": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone3": ZoneNames.Violation.RESTRICTED_VIP,
        "zone4": ZoneNames.Violation.EMERGENCY_EXIT,
    },
    # CAM-04 · B1-PARKING (primary violation cam)
    # zone1 → parking spot #3, zone2 → parking spot #7, etc.
    "CAM-04": {
        "zone1": ZoneNames.Violation.RESTRICTED_VIP,   # real parking bay number 3
        "zone2": ZoneNames.Violation.RESTRICTED_VIP,
        "zone3": ZoneNames.Violation.EMERGENCY_EXIT,
        "zone4": ZoneNames.Violation.LOADING_BAY,
    },
    # -- Intrusion cameras (field detection / after-hours zones) ------------
    "CAM-14": {
        "zone1": ZoneNames.Intrusion.EMERGENCY_EXIT,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
    "CAM-13": {
        "zone1": ZoneNames.Intrusion.EMERGENCY_EXIT,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
    "CAM-12": {
        "zone1": ZoneNames.Intrusion.EMERGENCY_EXIT,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
    "CAM-11": {
        "zone1": ZoneNames.Intrusion.EMERGENCY_EXIT,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
    "CAM-10": {
        "zone1": ZoneNames.Intrusion.EMERGENCY_EXIT,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
    "CAM-09": {
        "zone1": ZoneNames.Intrusion.EMERGENCY_EXIT,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
    "CAM-08": {
        "zone1": ZoneNames.Intrusion.EMERGENCY_EXIT,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
    "CAM-07": {
        "zone1": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
    "CAM-06": {
        "zone1": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
    "CAM-05": {
        "zone1": ZoneNames.Intrusion.EMERGENCY_EXIT,
        "zone2": ZoneNames.Intrusion.STAFF_ONLY_AREA,
        "zone3": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
        "zone4": ZoneNames.Intrusion.AFTER_HOURS_ZONE,
    },
}

# ---------------------------------------------------------------------------
# Real-life parking slot numbers per camera zone
#
# Keys   : camera_id → zone slot ("zone1".."zone4")
# Values : real-life parking bay number (int), or None = not a parking spot
#
# Fill in the numbers next to each zone.  Leave None for zones that are
# intrusion / violation areas and don't correspond to a physical parking bay.
# ---------------------------------------------------------------------------

ZONE_REAL_SLOT: dict[str, dict[str, int | None]] = {

    # ── CAM-01 · GF-ENTRANCE-INTERNAL ──────────────────────────────────────
    "CAM-01": {
        "zone1": None,  # emergency-exit      → fill real slot number if applicable
        "zone2": None,  # emergency-exit (vio) → fill real slot number if applicable
        "zone3": None,  # no-parking-zone      → fill real slot number if applicable
        "zone4": None,  # loading-bay          → fill real slot number if applicable
    },

    # ── CAM-02 · GF-WAITING ────────────────────────────────────────────────
    "CAM-02": {
        "zone1": "B12",  # restricted-vip   → fill real slot number if applicable
        "zone2": None,  # no-parking-zone  → fill real slot number if applicable
        "zone3": None,  # loading-bay      → fill real slot number if applicable
        "zone4": None,  # emergency-exit   → fill real slot number if applicable
    },

    # ── CAM-03 · B1-ENTRANCE-INTERNAL ──────────────────────────────────────
    "CAM-03": {
        "zone1": None,  # parking-area-11  → e.g. 11
        "zone2": None,  # parking-area-10  → e.g. 10
        "zone3": None,  # restricted-vip   → fill real slot number if applicable
        "zone4": None,  # emergency-exit   → fill real slot number if applicable
    },

    # ── CAM-04 · B1-PARKING ────────────────────────────────────────────────
    "CAM-04": {
        "zone1": None,  # parking-area-3   → e.g. 3
        "zone2": None,  # parking-area-2   → e.g. 2
        "zone3": None,  # emergency-exit   → fill real slot number if applicable
        "zone4": None,  # loading-bay      → fill real slot number if applicable
    },

    # ── CAM-05 · B1-PARKING ────────────────────────────────────────────────
    "CAM-05": {
        "zone1": None,  # emergency-exit      → fill real slot number if applicable
        "zone2": None,  # staff-only-area     → fill real slot number if applicable
        "zone3": None,  # after-hours-zone    → fill real slot number if applicable
        "zone4": None,  # after-hours-zone    → fill real slot number if applicable
    },

    # ── CAM-06 · B1-PARKING ────────────────────────────────────────────────
    "CAM-06": {
        "zone1": None,  # parking-area-6   → e.g. 6
        "zone2": None,  # staff-only-area  → fill real slot number if applicable
        "zone3": None,  # after-hours-zone → fill real slot number if applicable
        "zone4": None,  # after-hours-zone → fill real slot number if applicable
    },

    # ── CAM-07 · B1-PARKING ────────────────────────────────────────────────
    "CAM-07": {
        "zone1": None,  # parking-area-9   → e.g. 9
        "zone2": None,  # staff-only-area  → fill real slot number if applicable
        "zone3": None,  # after-hours-zone → fill real slot number if applicable
        "zone4": None,  # after-hours-zone → fill real slot number if applicable
    },

    # ── CAM-08 · B1-EXIT-INTERNAL ──────────────────────────────────────────
    "CAM-08": {
        "zone1": None,  # emergency-exit   → fill real slot number if applicable
        "zone2": None,  # staff-only-area  → fill real slot number if applicable
        "zone3": None,  # after-hours-zone → fill real slot number if applicable
        "zone4": None,  # after-hours-zone → fill real slot number if applicable
    },

    # ── CAM-09 · B2-PARKING ────────────────────────────────────────────────
    "CAM-09": {
        "zone1": None,  # emergency-exit   → fill real slot number if applicable
        "zone2": None,  # staff-only-area  → fill real slot number if applicable
        "zone3": None,  # after-hours-zone → fill real slot number if applicable
        "zone4": None,  # after-hours-zone → fill real slot number if applicable
    },

    # ── CAM-10 · B2-PARKING ────────────────────────────────────────────────
    "CAM-10": {
        "zone1": None,  # emergency-exit   → fill real slot number if applicable
        "zone2": None,  # staff-only-area  → fill real slot number if applicable
        "zone3": None,  # after-hours-zone → fill real slot number if applicable
        "zone4": None,  # after-hours-zone → fill real slot number if applicable
    },

    # ── CAM-11 · B2-PARKING ────────────────────────────────────────────────
    "CAM-11": {
        "zone1": None,  # emergency-exit   → fill real slot number if applicable
        "zone2": None,  # staff-only-area  → fill real slot number if applicable
        "zone3": None,  # after-hours-zone → fill real slot number if applicable
        "zone4": None,  # after-hours-zone → fill real slot number if applicable
    },

    # ── CAM-12 · B2-PARKING ────────────────────────────────────────────────
    "CAM-12": {
        "zone1": None,  # emergency-exit   → fill real slot number if applicable
        "zone2": None,  # staff-only-area  → fill real slot number if applicable
        "zone3": None,  # after-hours-zone → fill real slot number if applicable
        "zone4": None,  # after-hours-zone → fill real slot number if applicable
    },

    # ── CAM-13 · B2-PARKING ────────────────────────────────────────────────
    "CAM-13": {
        "zone1": None,  # emergency-exit   → fill real slot number if applicable
        "zone2": None,  # staff-only-area  → fill real slot number if applicable
        "zone3": None,  # after-hours-zone → fill real slot number if applicable
        "zone4": None,  # after-hours-zone → fill real slot number if applicable
    },

    # ── CAM-14 · B2-PARKING ────────────────────────────────────────────────
    "CAM-14": {
        "zone1": None,  # emergency-exit   → fill real slot number if applicable
        "zone2": None,  # staff-only-area  → fill real slot number if applicable
        "zone3": None,  # after-hours-zone → fill real slot number if applicable
        "zone4": None,  # after-hours-zone → fill real slot number if applicable
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

    cam_zones = ZONE_MAPPING.get(camera_id, {})

    # Direct lookup ("zone1" style)
    if raw_zone_id in cam_zones:
        return cam_zones[raw_zone_id]

    # Numeric fallback: "1" -> "zone1"
    numeric_key = f"zone{raw_zone_id}"
    if numeric_key in cam_zones:
        return cam_zones[numeric_key]

    return None


def get_real_slot_number(camera_id: str, raw_zone_id: str | None) -> int | None:
    """
    Return the real-life parking bay number for a given camera zone slot.

    Looks up ZONE_REAL_SLOT using the same slot-normalisation logic as
    resolve_zone() — accepts both "zone1" and "1" style inputs.

    Returns:
        int if a slot number has been configured, None otherwise.
    """
    if raw_zone_id is None:
        return None

    cam_slots = ZONE_REAL_SLOT.get(camera_id, {})

    # Direct lookup ("zone1" style)
    if raw_zone_id in cam_slots:
        return cam_slots[raw_zone_id]

    # Numeric fallback: "1" → "zone1"
    numeric_key = f"zone{raw_zone_id}"
    if numeric_key in cam_slots:
        return cam_slots[numeric_key]

    return None