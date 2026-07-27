# app/models/hik_validation.py
"""
HikCentral evidence behind one gate event.

A side table on purpose: HikCentral validates and recovers plates, but it is
not part of the parking domain, so `parking_sessions` keeps its shape and the
integration can be dropped without a destructive migration.

One row per HikCentral vehicle pass consumed by this system. `guid` is unique
so a single platform record can never justify two sessions.
"""

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)

from app.database import Base


class HikValidation(Base):
    __tablename__ = "hik_validations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(
        Integer, ForeignKey("parking_sessions.id", ondelete="NO ACTION"), index=True
    )
    entry_exit_log_id = Column(
        Integer, ForeignKey("entry_exit_log.id", ondelete="NO ACTION"), index=True
    )
    direction = Column(String(20), nullable=False)  # entry | exit

    # HikCentral's identity for one vehicle pass. Unique — see module docstring.
    guid = Column(String(64), nullable=False, unique=True, index=True)
    # Raw, exactly as HikCentral spells it (digits-first, e.g. "5625JKA").
    plate_license = Column(String(50))
    # normalize_plate() form, matching how this DB stores plates ("JKA-5625").
    canonical_plate = Column(String(50), index=True)
    # What the ANPR camera reported, when it reported anything. NULL on a
    # recovered entry — that is the whole point of the recovery path.
    reported_plate = Column(String(50))
    # edge_anpr | hik_confirmed | hik_corrected | hik_recovered.
    # Audit only — nothing in the pipeline branches on it.
    plate_source = Column(String(30), nullable=False)

    pass_time = Column(DateTime, index=True)  # naive facility-local
    resource_id = Column(String(50))
    resource_name = Column(String(100))

    vehicle_image_path = Column(Text)
    plate_image_path = Column(Text)
    vehicle_type = Column(String(50))
    vehicle_direction_type = Column(String(50))

    matched = Column(Boolean, nullable=False, default=False)
    match_reason = Column(String(50))
    created_at = Column(DateTime, nullable=False)

    def __repr__(self):
        return (
            f"<HikValidation {self.id} {self.direction} "
            f"plate={self.canonical_plate} source={self.plate_source}>"
        )
