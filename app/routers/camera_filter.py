from fastapi import APIRouter, FastAPI
from pydantic_settings import BaseSettings
from app.services.camera_filter_service import set_camera_filter, exclude_camera_filter, get_incl_camera_filter, get_exclude_camera_filter
from app.database import get_db
from app.utils.logger import get_logger

logger = get_logger(__name__)



router = APIRouter()


@router.get("/camera-filter")
def get_camera_filter():
    logger.info("Getting camera filter")
    camera_filter = get_incl_camera_filter()
    exclude_camera_filter = get_exclude_camera_filter()
    
    return {"camera_filter": camera_filter, "exclude_camera_filter": exclude_camera_filter}




@router.put("/set-camera-filter")
def set_camera_filter(camera_id: str):
    db = get_db()
    set_camera_filter(camera_id, db)
    exclude_camera_filter(camera_id, db)

    camera_filter = get_incl_camera_filter(db)
    exclude_camera_filter = get_exclude_camera_filter(db)

    return {"camera_filter": camera_filter, "exclude_camera_filter": exclude_camera_filter}



@router.delete("/camera-filter/{camera_id}")
def delete_camera_filter(camera_id: str):
    db = get_db()
    set_camera_filter(camera_id=camera_id, db=db)
    exclude_camera_filter(camera_id=camera_id, db=db)

    camera_filter = get_incl_camera_filter(db)
    exclude_camera_filter = get_exclude_camera_filter(db)

    return {"camera_filter": camera_filter, "exclude_camera_filter": exclude_camera_filter}



