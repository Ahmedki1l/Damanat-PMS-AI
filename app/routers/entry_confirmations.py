"""Authenticated, metadata-only Video Analytics entry confirmations."""

import hmac
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.schemas.entry_confirmation import (
    EntryConfirmationRequest,
    EntryConfirmationResponse,
)
from app.services.entry_confirmation_service import (
    InvalidEntryConfirmation,
    StaleAfterExit,
    SupersededByNewerEntry,
    apply_confirmed_entry,
    confirmation_transaction_guard,
)
from app.services.entry_state_lock import EntryStateLockUnavailable
from app.utils.logger import get_logger

logger = get_logger(__name__)
router = APIRouter(prefix="/internal/entry-confirmations")


def require_entry_v2_service_key(
    x_service_key: Annotated[Optional[str], Header()] = None,
) -> None:
    expected = settings.ENTRY_V2_SERVICE_KEY
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Entry V2 service authentication is not configured",
        )
    if not x_service_key or not hmac.compare_digest(x_service_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing service key",
        )


@router.post("", response_model=EntryConfirmationResponse)
def confirm_entry(
    body: EntryConfirmationRequest,
    _: None = Depends(require_entry_v2_service_key),
    db: Session = Depends(get_db),
) -> EntryConfirmationResponse:
    if settings.ENTRY_V2_MODE == "off":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Entry V2 is disabled",
        )
    if settings.ENTRY_V2_MODE == "shadow":
        logger.info(
            "[EntryV2][shadow] Decision observed without mutation: %s status=%s",
            body.decision_id,
            body.status,
        )
        return EntryConfirmationResponse(
            decision_id=body.decision_id,
            status=body.status,
            result="shadowed",
            plate_number=body.canonical_plate,
        )
    if body.status == "abstained":
        logger.info(
            "[EntryV2] Abstained decision=%s reason=%s reid=%s row_margin=%s "
            "column_margin=%s ocr_source=%s ocr_confidence=%s",
            body.decision_id,
            body.reason,
            body.reid_score,
            body.reid_row_margin,
            body.reid_column_margin,
            body.ocr_source,
            body.ocr_confidence,
        )
        return EntryConfirmationResponse(
            decision_id=body.decision_id,
            status=body.status,
            result="abstained",
            plate_number=body.canonical_plate,
        )

    try:
        with confirmation_transaction_guard(db, body):
            result = apply_confirmed_entry(db, body)
            db.commit()
    except StaleAfterExit as exc:
        db.rollback()
        logger.warning(
            "[EntryV2] Terminal stale confirmation decision=%s: %s",
            body.decision_id,
            exc,
        )
        return EntryConfirmationResponse(
            decision_id=body.decision_id,
            status=body.status,
            result="stale_after_exit",
            plate_number=exc.plate_number,
        )
    except SupersededByNewerEntry as exc:
        db.rollback()
        logger.warning(
            "[EntryV2] Terminal superseded confirmation decision=%s: %s",
            body.decision_id,
            exc,
        )
        return EntryConfirmationResponse(
            decision_id=body.decision_id,
            status=body.status,
            result="superseded_by_newer_entry",
            plate_number=exc.plate_number,
        )
    except EntryStateLockUnavailable as exc:
        db.rollback()
        logger.warning(
            "[EntryV2] Confirmation state lock unavailable decision=%s: %s",
            body.decision_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="entry state is busy",
            headers={"Retry-After": "1"},
        ) from exc
    except InvalidEntryConfirmation as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        logger.error(
            "[EntryV2] Confirmation transaction failed decision=%s",
            body.decision_id,
            exc_info=True,
        )
        raise

    logger.info(
        "[EntryV2] Confirmation applied decision=%s result=%s plate=%s "
        "reid=%s row_margin=%s column_margin=%s ocr_source=%s "
        "ocr_confidence=%s corrected=%s",
        body.decision_id,
        result.result,
        result.plate_number,
        body.reid_score,
        body.reid_row_margin,
        body.reid_column_margin,
        body.ocr_source,
        body.ocr_confidence,
        body.corrected,
    )
    return EntryConfirmationResponse(
        decision_id=body.decision_id,
        status=body.status,
        result=result.result,
        plate_number=result.plate_number,
        entry_log_id=result.entry_log_id,
        session_id=result.session_id,
    )
