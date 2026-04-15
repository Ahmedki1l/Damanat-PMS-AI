from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean

from app.database import Base


class CameraFeed(Base):
    __tablename__ = "camera_feeds"

    id = Column(Integer, primary_key=True, autoincrement=True)
    camera_id = Column(String(50), nullable=False, index=True) # e.g. CAM-08
    location_label = Column(String(100), nullable=False, index=True) # e.g. (B1-EXIT)
    event_description = Column(String(255), default="Vehicle detected") # e.g. Vehicle detected
    detection_source = Column(String(50), nullable=False, index=True) # e.g. ANPR, Line Detection, Vehicle Match
    plate_number = Column(String(50), nullable=True, index=True)
    snapshot_path = Column(Text)
    timestamp = Column(DateTime, nullable=False, index=True)

    def __repr__(self):
        return f"<CameraFeed {self.id} camera_id={self.camera_id} source={self.detection_source}>"