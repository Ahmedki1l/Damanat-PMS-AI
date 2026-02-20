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
from app.config import settings
from datetime import datetime

router = APIRouter()


@router.get("/health", summary="System health check")
def health_check(db: Session = Depends(get_db)):
    """
    Returns:
    - Backend status
    - Database connectivity
    - Camera reachability (ping ISAPI on each camera)
    """
    result = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "backend": "ok",
        "database": "unknown",
        "cameras": {},
    }

    # Check database
    try:
        db.execute(text("SELECT 1"))
        result["database"] = "ok"
    except Exception as e:
        result["database"] = f"error: {str(e)}"
        result["status"] = "degraded"

    # Ping each camera
    for cam_id, cam in settings.CAMERAS.items():
        try:
            resp = requests.get(
                f"http://{cam['ip']}/ISAPI/System/deviceInfo",
                auth=HTTPDigestAuth(cam["user"], cam["password"]),
                timeout=3,
            )
            result["cameras"][cam_id] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
        except requests.exceptions.ConnectionError:
            result["cameras"][cam_id] = "unreachable"
            result["status"] = "degraded"
        except Exception as e:
            result["cameras"][cam_id] = f"error: {str(e)}"

    return result
