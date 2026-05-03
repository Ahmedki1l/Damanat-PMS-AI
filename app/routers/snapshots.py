# app/routers/snapshots.py
"""
Serves saved camera snapshot JPEGs from the local detection_images/ folder.

Replaces the previous StaticFiles mount with a regular GET endpoint so we
can validate filenames and have a normal place to add logging or swap the
storage backend later.
"""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.services.snapshot_service import SNAPSHOT_DIR

router = APIRouter()


@router.get(
    "/pms-ai/snapshots/{filename}",
    summary="Serve a saved camera snapshot JPEG",
    response_class=FileResponse,
)
def get_snapshot(filename: str):
    # Reject anything that isn't a bare filename — blocks ../ traversal,
    # absolute paths, and Windows drive specs.
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        raise HTTPException(status_code=404, detail="Snapshot not found")

    filepath = os.path.join(SNAPSHOT_DIR, safe_name)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Snapshot not found")

    return FileResponse(filepath, media_type="image/jpeg")
