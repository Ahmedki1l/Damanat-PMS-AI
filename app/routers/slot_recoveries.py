"""Authenticated Video Analytics slot-recovery endpoint.

Deliberately separate from `/internal/entry-confirmations`, which is gated on
`ENTRY_V2_MODE` and describes a car arriving at the gate. This describes the
opposite situation — a car already parked that no gate event ever accounted for —
and must work whether or not Entry V2 is rolled out.

Every write goes through `slot_recovery_service`, which re-checks the live slot
state before committing. See its module docstring for why that guard exists.
"""

import hmac
from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.schemas.slot_recovery import SlotRecoveryRequest, SlotRecoveryResponse
from app.services.slot_recovery_service import (
    RecoveryRejected,
    recover_slot_session,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/internal/slot-recoveries")


def require_service_key(
    x_service_key: Annotated[Optional[str], Header()] = None,
) -> None:
    """Same service-key boundary Entry V2 uses — this endpoint mutates sessions."""
    expected = settings.ENTRY_V2_SERVICE_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service authentication is not configured",
        )
    if not x_service_key or not hmac.compare_digest(x_service_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service key",
        )


@router.post("", response_model=SlotRecoveryResponse)
def recover_slot(
    body: SlotRecoveryRequest,
    _: None = Depends(require_service_key),
    db: Session = Depends(get_db),
) -> SlotRecoveryResponse:
    if not settings.SLOT_RECOVERY_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Slot recovery is disabled",
        )

    observed = None
    if body.observed_at:
        try:
            observed = datetime.fromisoformat(body.observed_at)
            if observed.tzinfo is not None:
                observed = observed.astimezone().replace(tzinfo=None)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="observed_at must be ISO-8601",
            )

    # PMS-AI applies its own bar rather than trusting VA's. The two services can be
    # deployed and tuned independently, and this is the side that owns sessions.
    if body.reid_score < settings.SLOT_RECOVERY_MIN_REID_SCORE:
        return SlotRecoveryResponse(
            plate_number=body.plate_number, slot_id=body.slot_id, result="rejected",
            reason=(f"reid_score {body.reid_score:.3f} below "
                    f"{settings.SLOT_RECOVERY_MIN_REID_SCORE:.2f}"),
        )
    if body.reid_margin < settings.SLOT_RECOVERY_MIN_REID_MARGIN:
        return SlotRecoveryResponse(
            plate_number=body.plate_number, slot_id=body.slot_id, result="rejected",
            reason=(f"reid_margin {body.reid_margin:.3f} below "
                    f"{settings.SLOT_RECOVERY_MIN_REID_MARGIN:.2f}"),
        )

    try:
        session, created = recover_slot_session(
            db,
            plate_number=body.plate_number,
            slot_id=body.slot_id,
            camera_id=body.camera_id,
            reid_score=body.reid_score,
            reid_margin=body.reid_margin,
            reid_same_view=body.reid_same_view,
            ocr_text=body.ocr_text,
            observed_at=observed,
        )
    except RecoveryRejected as exc:
        # A rejection is the guard working, not a failure. 200 with a reason so VA
        # records the outcome instead of retrying a claim that is now provably stale.
        db.rollback()
        logger.info(
            "[recovery] REFUSED %s for slot=%s: %s",
            body.plate_number, body.slot_id, exc.reason,
        )
        return SlotRecoveryResponse(
            plate_number=body.plate_number, slot_id=body.slot_id,
            result="rejected", reason=exc.reason,
        )

    db.commit()
    return SlotRecoveryResponse(
        plate_number=body.plate_number,
        slot_id=body.slot_id,
        result="created" if created else "already_open",
        session_id=session.id,
    )
