"""Video Analytics slot-recovery evidence and verdict.

A recovery asserts something unusual: that a car is parked inside which this
system has no record of admitting. The payload therefore carries the EVIDENCE,
not just the answer, so the decision is auditable after the fact and so PMS-AI
can apply its own bar rather than trusting VA's.
"""

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class SlotRecoveryRequest(BaseModel):
    """Two independent witnesses that a known car is parked in a known slot."""

    model_config = ConfigDict(str_strip_whitespace=True)

    plate_number: str = Field(min_length=2, max_length=50)
    slot_id: str = Field(min_length=1, max_length=50)
    camera_id: str = Field(min_length=1, max_length=50)

    # Witness 1 — appearance against the car's persisted gallery.
    reid_score: float = Field(ge=0.0, le=1.0)
    reid_margin: float = Field(ge=0.0, le=1.0)
    # True when the winning reference was taught by THIS camera on a previous
    # visit (a parked pose: rank-1 0.976) rather than at the gate (cross-view
    # 0.736, and per-car inverted). The two are not the same quality of evidence.
    reid_same_view: bool = False

    # Witness 2 — characters read off the plate on the car, as OCR returned them.
    # Kept raw, unnormalised: it is evidence, and `read_matches_plate` already
    # tolerates the letter mush. Storing a cleaned-up version would hide what was
    # actually seen.
    ocr_text: str = Field(min_length=1, max_length=64)

    observed_at: Optional[str] = None


class SlotRecoveryResponse(BaseModel):
    plate_number: str
    slot_id: str
    # created | already_open | rejected
    result: str
    session_id: Optional[int] = None
    reason: Optional[str] = None
