"""Value objects for the HikCentral validation layer.

Pure data — no HTTP, no ORM, no DB session. Everything that crosses the package
boundary is one of these frozen dataclasses, so HikCentral's wire format never
leaks into the entry pipeline.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from app.config import facility_tz
from app.services.event_parser import normalize_plate

# `plate_source` values recorded on hik_validations for audit only. Nothing in
# the pipeline branches on these — callers always receive one canonical plate.
PLATE_SOURCE_EDGE_ANPR = "edge_anpr"
PLATE_SOURCE_HIK_CONFIRMED = "hik_confirmed"
PLATE_SOURCE_HIK_CORRECTED = "hik_corrected"
PLATE_SOURCE_HIK_RECOVERED = "hik_recovered"

# HikCentral spells its JSON in PascalCase. Kept as a table rather than inline
# string literals so the live-probe script and the parser can never drift.
_FIELD_GUID = "GUID"
_FIELD_PASS_TIME = "PassTime"
_FIELD_PLATE = "PlateLicense"
_FIELD_VEHICLE_IMAGE = "VehicleImageUrl"
_FIELD_PLATE_IMAGE = "PlateImageUrl"
_FIELD_RESOURCE_ID = "ResourceID"
_FIELD_RESOURCE_NAME = "ResourceName"
_FIELD_DIRECTION = "VehicleDirectionType"
_FIELD_VEHICLE_TYPE = "VehicleType"


def _text(raw: dict, key: str) -> Optional[str]:
    """Return a trimmed string field, or None when absent/blank."""
    value = raw.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def to_facility_naive(moment: datetime) -> datetime:
    """Convert a HikCentral timestamp to the DB's naive facility-local form.

    HikCentral reports tz-aware times (`...+03:00`); every writer in this
    codebase stores naive facility-local wall-clock. Mixing the two silently
    shifts entries by the UTC offset, so this conversion is mandatory at the
    boundary. Naive input is assumed to already be facility-local.
    """
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(facility_tz()).replace(tzinfo=None)


def from_facility_naive(moment: datetime) -> datetime:
    """Attach the facility timezone to a naive DB/pipeline timestamp.

    The inverse of to_facility_naive(). HikCentral is always queried with an
    explicit offset so the window can never be read in the wrong timezone.
    """
    if moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=facility_tz())


@dataclass(frozen=True)
class VehicleLogRecord:
    """One HikCentral vehicle pass."""

    guid: str
    # tz-aware, exactly as HikCentral reported it. Convert with
    # to_facility_naive() before it touches the DB.
    pass_time: datetime
    plate_license: str
    # normalize_plate(plate_license): HikCentral spells plates digits-first
    # ("5625JKA") while this DB stores letters-first ("JKA-5625").
    canonical_plate: Optional[str]
    vehicle_image_url: Optional[str] = None
    plate_image_url: Optional[str] = None
    resource_id: Optional[str] = None
    resource_name: Optional[str] = None
    vehicle_direction_type: Optional[str] = None
    vehicle_type: Optional[str] = None

    @classmethod
    def from_payload(cls, raw: Any) -> Optional["VehicleLogRecord"]:
        """Build a record from one raw HikCentral entry, or None if unusable.

        A record without a GUID or a parsable PassTime cannot be matched or
        de-duplicated, so it is dropped rather than half-trusted.
        """
        if not isinstance(raw, dict):
            return None

        guid = _text(raw, _FIELD_GUID)
        raw_pass_time = _text(raw, _FIELD_PASS_TIME)
        if not guid or not raw_pass_time:
            return None
        try:
            pass_time = datetime.fromisoformat(raw_pass_time)
        except ValueError:
            return None

        plate_license = _text(raw, _FIELD_PLATE) or ""
        return cls(
            guid=guid,
            pass_time=pass_time,
            plate_license=plate_license,
            canonical_plate=normalize_plate(plate_license),
            vehicle_image_url=_text(raw, _FIELD_VEHICLE_IMAGE),
            plate_image_url=_text(raw, _FIELD_PLATE_IMAGE),
            resource_id=_text(raw, _FIELD_RESOURCE_ID),
            resource_name=_text(raw, _FIELD_RESOURCE_NAME),
            vehicle_direction_type=_text(raw, _FIELD_DIRECTION),
            vehicle_type=_text(raw, _FIELD_VEHICLE_TYPE),
        )


@dataclass(frozen=True)
class HikImages:
    """Locally persisted HikCentral imagery, as public snapshot URLs."""

    # Public /snapshots URLs — what goes into the DB.
    vehicle_image_path: Optional[str] = None
    plate_image_path: Optional[str] = None
    # On-disk paths, kept for local processing (e.g. forwarding to VA).
    vehicle_local_path: Optional[str] = None
    plate_local_path: Optional[str] = None

    def __bool__(self) -> bool:
        return bool(self.vehicle_image_path or self.plate_image_path)


@dataclass(frozen=True)
class HikOutcome:
    """The single answer the entry pipeline consumes.

    `plate` is the plate the caller must use — already reconciled against the
    configured mode. Callers are not expected to inspect anything else; the
    remaining fields exist so the decision can be persisted and audited.
    """

    plate: Optional[str]
    plate_source: str
    matched: bool
    reason: str
    record: Optional[VehicleLogRecord] = None
    reported_plate: Optional[str] = None
    # How many HikCentral records fell inside the lookup window. Recovery
    # requires exactly one; this is kept so an ambiguous window is diagnosable.
    candidates_considered: int = 0

    @property
    def guid(self) -> Optional[str]:
        return self.record.guid if self.record else None

    @property
    def pass_time(self) -> Optional[datetime]:
        return self.record.pass_time if self.record else None

    @property
    def pass_time_local(self) -> Optional[datetime]:
        """PassTime as the naive facility-local value every writer stores."""
        return to_facility_naive(self.record.pass_time) if self.record else None

    @property
    def vehicle_image_url(self) -> Optional[str]:
        return self.record.vehicle_image_url if self.record else None

    @property
    def plate_image_url(self) -> Optional[str]:
        return self.record.plate_image_url if self.record else None

    @property
    def has_evidence(self) -> bool:
        """True when there is a HikCentral record worth persisting."""
        return self.record is not None
