"""Process liveness and dependency diagnostics."""

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import facility_now_naive, settings
from app.database import get_db
from app.schemas.responses import HealthResponse
from app.services.entry_v2_forwarder import entry_v2_shadow_status

router = APIRouter()


def _camera_ingest_spool_status() -> dict:
    """Keep a spool failure visible without hiding the other diagnostics."""
    try:
        from app.services.camera_ingest_spool import spool_stats

        return spool_stats()
    except Exception as exc:  # noqa: BLE001
        return {"enabled": None, "error": str(exc)}


@router.get("/health", response_model=HealthResponse, summary="Process liveness")
async def health_check():
    """Report whether this API can serve requests, without dependency I/O."""
    return {
        "status": "ok",
        "timestamp": facility_now_naive().isoformat(),
        "backend": "ok",
        "database": "not_checked",
        "cameras": list(settings.CAMERAS.keys()),
        "entry_v2_shadow": entry_v2_shadow_status(),
        "camera_ingest_spool": {"status": "not_checked"},
    }


@router.get("/health/diagnostics", response_model=HealthResponse, summary="Dependency diagnostics")
def health_diagnostics(db: Session = Depends(get_db)):
    """Inspect DB and spool state outside the liveness probe."""
    result = {
        "status": "ok",
        "timestamp": facility_now_naive().isoformat(),
        "backend": "ok",
        "database": "unknown",
        "cameras": list(settings.CAMERAS.keys()),
        "entry_v2_shadow": entry_v2_shadow_status(),
        "camera_ingest_spool": _camera_ingest_spool_status(),
    }
    try:
        db.execute(text("SELECT 1"))
        result["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        result["database"] = f"error: {exc}"
        result["status"] = "degraded"
    return result
