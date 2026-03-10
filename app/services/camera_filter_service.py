from datetime import datetime
from sqlalchemy.orm import Session
from app.models.system_config import SystemConfig
from app.utils.logger import get_logger
from app.database import SessionLocal

logger = get_logger(__name__)

def set_camera_filter(camera_id: str, db: Session):
    """Set camera filter."""
    try:
        system_config = SystemConfig(
            key="LOG_CAMERA_FILTER",
            value=camera_id,
            created_at=datetime.utcnow(),
        )
        db.add(system_config)
        db.commit()
        logger.info(f"Camera filter set to {camera_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to set camera filter: {e}")
        raise

def exclude_camera_filter(camera_id: str, db: Session):
    """Exclude camera filter."""
    try:
        system_config = SystemConfig(
            key="EXCLUDE_CAMERA_FILTER",
            value=camera_id,
            created_at=datetime.utcnow(),
        )
        db.add(system_config)
        db.commit()
        logger.info(f"Camera filter set to {camera_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to set camera filter: {e}")
        raise

def get_incl_camera_filter():
    """Get camera filter."""
    try:
        db = SessionLocal()
        system_config = db.query(SystemConfig).filter(SystemConfig.key == "LOG_CAMERA_FILTER").first()
        return system_config.value if system_config else None
    except Exception as e:
        logger.error(f"Failed to get camera filter: {e}")
        raise

def get_exclude_camera_filter():
    """Get exclude camera filter."""
    try:
        db = SessionLocal()
        system_config = db.query(SystemConfig).filter(SystemConfig.key == "EXCLUDE_CAMERA_FILTER").first()
        return system_config.value if system_config else None
    except Exception as e:
        logger.error(f"Failed to get exclude camera filter: {e}")
        raise
