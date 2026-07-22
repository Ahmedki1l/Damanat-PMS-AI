"""Metadata-only contract for Video Analytics entry decisions."""

from datetime import datetime, timedelta
from typing import Literal, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.config import facility_tz


_ENTRY_TIME_TOLERANCE = timedelta(milliseconds=10)
_SQL_SERVER_DATETIME_MIN = datetime(1753, 1, 1) + _ENTRY_TIME_TOLERANCE
_SQL_SERVER_DATETIME_MAX = datetime(9999, 12, 31, 23, 59, 59, 980000)


class EntryConfirmationRequest(BaseModel):
    # Reject undeclared fields so image/base64/path payloads cannot accidentally
    # turn this metadata-only callback into a second evidence transport.
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(min_length=1, max_length=200)
    status: Literal["confirmed", "abstained"]
    canonical_plate: Optional[str] = Field(default=None, max_length=50)
    attempt_id: Optional[str] = Field(default=None, max_length=200)
    crossing_id: Optional[str] = Field(default=None, max_length=200)
    entry_camera_id: str = Field(min_length=1, max_length=50)
    entry_captured_at: AwareDatetime
    reported_plate: Optional[str] = Field(default=None, max_length=50)
    plate_confidence: Optional[int] = Field(default=None, ge=0, le=100)
    corrected: Optional[bool] = None
    reason: Optional[str] = Field(default=None, max_length=500)
    reid_score: Optional[float] = None
    reid_margin: Optional[float] = None
    reid_row_margin: Optional[float] = None
    reid_column_margin: Optional[float] = None
    ocr_source: Optional[str] = Field(default=None, max_length=100)
    ocr_text: Optional[str] = Field(default=None, max_length=100)
    ocr_confidence: Optional[float] = None
    ocr_evidence_ids: list[str] = Field(default_factory=list)
    superseded_plates: list[str] = Field(default_factory=list)

    @field_validator("entry_captured_at")
    @classmethod
    def validate_sql_server_timestamp(cls, value: AwareDatetime) -> AwareDatetime:
        """Keep the local wall-clock value inside DATETIME and ±10 ms queries."""
        try:
            local_value = value.astimezone(facility_tz()).replace(tzinfo=None)
        except (OverflowError, OSError) as exc:
            raise ValueError(
                "entry_captured_at is outside the SQL Server-safe range"
            ) from exc
        if not _SQL_SERVER_DATETIME_MIN <= local_value <= _SQL_SERVER_DATETIME_MAX:
            raise ValueError(
                "entry_captured_at is outside the SQL Server-safe range"
            )
        return value

    @model_validator(mode="after")
    def validate_confirmed_identity(self):
        if self.status == "confirmed" and not self.canonical_plate:
            raise ValueError("canonical_plate is required for a confirmed decision")
        if not self.attempt_id and not self.crossing_id:
            raise ValueError("attempt_id or crossing_id is required")
        return self


class EntryConfirmationResponse(BaseModel):
    decision_id: str
    status: Literal["confirmed", "abstained"]
    result: Literal[
        "created",
        "duplicate",
        "abstained",
        "shadowed",
        "stale_after_exit",
        "superseded_by_newer_entry",
    ]
    plate_number: Optional[str] = None
    entry_log_id: Optional[int] = None
    session_id: Optional[int] = None
