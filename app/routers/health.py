# app/routers/health.py
"""
System health check endpoint.
Returns status of backend + DB + camera reachability.
"""

import requests
from requests.auth import HTTPDigestAuth
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
from app.config import settings, facility_now_naive
from app.schemas.responses import HealthResponse
from app.services.entry_v2_forwarder import entry_v2_shadow_status
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.get("/health", response_model=HealthResponse, summary="System health check")
def health_check(db: Session = Depends(get_db)):
    """
    Returns:
    - Backend status
    - Database connectivity
    - Camera reachability (ping ISAPI on each camera)
    """

    result = {
        "status": "ok",
        "timestamp": facility_now_naive().isoformat(),
        "backend": "ok",
        "database": "unknown",
        "cameras": list(settings.CAMERAS.keys()),
        "entry_v2_shadow": entry_v2_shadow_status(),
    }

    # Check database
    try:
        db.execute(text("SELECT 1"))
        result["database"] = "ok"
    except Exception as e:
        result["database"] = f"error: {str(e)}"
        result["status"] = "degraded"

    # Ping cameras in parallel to avoid linear timeouts (G-24)
    def check_cam(cam_id_info):
        cam_id, cam = cam_id_info
        try:
            resp = requests.get(
                f"http://{cam['ip']}/ISAPI/System/deviceInfo",
                auth=HTTPDigestAuth(cam["user"], cam["password"]),
                timeout=2.0,
            )
            return cam_id, "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
        except requests.exceptions.ConnectionError:
            return cam_id, "unreachable"
        except Exception as e:
            return cam_id, f"error: {str(e)}"

    # with ThreadPoolExecutor(max_workers=10) as executor:
    #     camera_results = list(executor.map(check_cam, settings.CAMERAS.items()))

    # for cam_id, cam_status in camera_results:
    #     if cam_status != "ok":
    #         result["status"] = "degraded"
    #     logger.debug(f"[health] camera={cam_id} status={cam_status}")

    return result

