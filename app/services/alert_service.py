# app/services/alert_service.py
"""
Shared alert creation service.
Used by occupancy_service, violation_service, intrusion_service, and entry_exit_service.
"""

from datetime import datetime, UTC
from sqlalchemy.orm import Session
from app.config import settings, facility_now_naive
from app.models.alert import Alert
from app.models.vehicle import Vehicle
from app.utils.logger import get_logger
from app.utils.event_bus import event_bus

logger = get_logger(__name__)


ALERT_SEVERITY_MAP = {
    "unknown_vehicle": "critical",
    "violence": "critical",
    "intrusion": "critical",
    "vehicle_intrusion": "critical",
    "named_slot_violation": "critical",
    "vehicle_violation": "critical",
    "capacity_exceeded": "warning",
}


def _resolve_severity(alert_type: str) -> str:
    return ALERT_SEVERITY_MAP.get(alert_type, "warning")


async def broadcast_event(
    is_alert: bool,
    severity: str,
    event_type: str,
    description: str,
    camera_id: str,
    alert_type: str | None = None,
    alert_id: int | None = None,
    plate_number: str | None = None,
    snapshot_path: str | None = None,
    zone_id: str | None = None,
    zone_name: str | None = None,
    slot_number: int | None = None,
    triggered_at: datetime | None = None,
):
    """
    Standardized broadcast function for all real-time events.
    Ensures consistent JSON schema for api_gateway.
    """
    import json
    
    # Resolve location display name
    location_display = _resolve_location_display(
        camera_id=camera_id,
        zone_id=zone_id,
        zone_name=zone_name,
        slot_number=slot_number,
    )

    payload = {
        "is_alert": is_alert,
        "alert_id": alert_id,
        "severity": severity,
        "event_type": event_type,
        "alert_type": alert_type or event_type,
        "camera_id": camera_id,
        "location_display": location_display,
        "description": description,
        "plate_number": plate_number,
        "snapshot_path": snapshot_path,
        "triggered_at": (triggered_at or facility_now_naive()).isoformat(),
        # Breadcrumbs for internal debugging
        "internal_meta": {
            "zone_id": zone_id,
            "region_id": slot_number,
        }
    }

    # Notification suppression. The caller has already persisted and logged the
    # alert by the time it reaches here, so dropping the publish silences the
    # dashboard pop-up WITHOUT losing the record — the row is still returned by
    # GET /alerts. Keyed on the resolved alert_type so a suppressed type stays
    # suppressed no matter which service published it.
    if payload["alert_type"] in settings.suppressed_alert_notification_types():
        logger.debug(
            f"[Broadcast] SUPPRESSED {payload['alert_type']} | {description} "
            "(alert still recorded; see SUPPRESSED_ALERT_NOTIFICATION_TYPES)"
        )
        return

    event_bus.publish(json.dumps(payload))
    logger.debug(f"[Broadcast] {event_type} | {'ALERT' if is_alert else 'INFO'} | {description}")



def _resolve_location_display(
    camera_id: str,
    zone_id: str | None,
    zone_name: str | None,
    slot_number: int | str | None,
) -> str:
    if slot_number:
        return f"Slot {slot_number}"
    if zone_name:
        return zone_name
    if zone_id:
        zone_meta = settings.get_zone_metadata(zone_id)
        if zone_meta.get("zone_name"):
            return str(zone_meta["zone_name"])
        return zone_id

    camera = settings.CAMERAS.get(camera_id, {})
    if camera.get("name"):
        return str(camera["name"])

    gate = camera.get("gate")
    if gate == "entry":
        return "Entry Gate"
    if gate == "exit":
        return "Exit Gate"

    return camera_id


async def create_alert(
    db: Session,
    alert_type: str,
    camera_id: str,
    zone_id: str,
    event_type: str,
    description: str,
    snapshot_path: str | None = None,
    region_id: int | None = None,
    slot_id: str | None = None,
    slot_number: int | None = None,
    zone_name: str | None = None,
    plate_number: str | None = None,
):
    """
    Create an alert record in the database.

    Parameters:
        zone_id:   canonical zone name used for cooldown/resolution logic (e.g. "restricted-vip")
        zone_name: human-readable stored name; defaults to zone_id if not provided
        region_id: raw integer region ID sent by the camera
        slot_number: real-life parking bay number (from ZONE_REAL_SLOT)
        snapshot_path: CDN URL or local file path for the evidence image.

    Note: The caller (event_dispatcher) is responsible for committing the transaction.
    Uses a nested savepoint for the DB insert so that a constraint failure on the
    alert table does NOT roll back the parent transaction (entry_exit_log, parking_session).
    """
    # Fully-disabled alert types are dropped before anything is written: no DB
    # row, no log, no stream. Distinct from SUPPRESSED_ALERT_NOTIFICATION_TYPES,
    # which silences only the live notification but still records the row.
    if alert_type in settings.disabled_alert_types():
        logger.debug(
            "[ALERT] %s is disabled (DISABLED_ALERT_TYPES) — not recorded", alert_type
        )
        return None

    try:
        severity = _resolve_severity(alert_type)
        zone_meta = settings.get_zone_metadata(zone_id)
        resolved_zone_name = zone_name if zone_name is not None else zone_meta.get("zone_name") or zone_id

        # Defensive slot_id fallback: when the calling site didn't pass slot_id
        # but did pass a plate, look up vehicles.current_slot_id (kept fresh by
        # parking_session_service.py:141 on every bind, cleared at :178-179 on
        # unbind). This ensures alerts about a parked vehicle carry the slot
        # they're parked in even when the call site (e.g. unknown_vehicle alert
        # at the gate) doesn't know the slot directly.
        if slot_id is None and plate_number:
            try:
                vehicle = db.query(Vehicle).filter(Vehicle.plate_number == plate_number).first()
                if vehicle and vehicle.current_slot_id:
                    slot_id = vehicle.current_slot_id
            except Exception as lookup_err:
                logger.debug(f"[ALERT] current_slot_id fallback lookup failed: {lookup_err}")

        location_display = _resolve_location_display(
            camera_id=camera_id,
            zone_id=zone_id,
            zone_name=resolved_zone_name,
            slot_number=slot_number,
        )
        new_alert = Alert(
            alert_type=alert_type,
            camera_id=camera_id,
            zone_id=zone_id,
            zone_name=resolved_zone_name,
            slot_id=slot_id,
            region_id=region_id,
            slot_number=slot_number,
            event_type=event_type,
            description=description,
            snapshot_path=snapshot_path,
            plate_number=plate_number,
            severity=severity,
            location_display=location_display,
            is_resolved=False,
            triggered_at=facility_now_naive(),
        )

        logger.warning(
            f"[ALERT][{alert_type.upper()}] Cam: {camera_id} | Zone: {zone_name or zone_id} "
            f"| Region: {region_id} | Slot: {slot_number} | {description}"
        )

        # Use a nested savepoint so that a DB-level failure (e.g. stale FK constraint)
        # only rolls back the alert INSERT — not the parent transaction that holds
        # the entry_exit_log and parking_session inserts.
        try:
            with db.begin_nested():
                db.add(new_alert)
                db.flush()
            
        except Exception as db_err:
            logger.error(
                f"[ALERT] DB insert failed for {alert_type} (plate={plate_number}): {db_err}. "
                "Alert was NOT saved but the event transaction will continue."
            )
            # Fall through to publish the SSE event even without DB persistence.
            new_alert = None

        import json
        alert_id = new_alert.id if new_alert else None
        
        await broadcast_event(
            is_alert=True,
            severity=severity,
            event_type=event_type,
            alert_type=alert_type,
            alert_id=alert_id,
            description=description,
            camera_id=camera_id,
            zone_id=zone_id,
            zone_name=resolved_zone_name,
            slot_number=slot_number,
            plate_number=plate_number,
            snapshot_path=snapshot_path,
            triggered_at=new_alert.triggered_at if new_alert else facility_now_naive(),
        )

    except Exception as e:
        logger.error(f"Failed to create alert: {e}", exc_info=True)
        # We don't raise here to prevent the main event processing from failing
        # just because the alert logging had an issue.
