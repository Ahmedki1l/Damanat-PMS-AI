import sys
import os
from datetime import datetime, UTC
from sqlalchemy.orm import Session

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.database import SessionLocal, engine
from app.services.event_parser import ParsedCameraEvent
from app.services.camera_feed_service import add_event_to_feed
from app.models.camera_feed import CameraFeed

def test_camera_feed():
    # Create the table if it doesn't exist
    CameraFeed.__table__.create(engine, checkfirst=True)
    db: Session = SessionLocal()
    try:
        print("Creating fake camera events...")
        
        # Test Event 1: Line Detection on CAM-04 (Not a gate, but should PASS now)
        event1 = ParsedCameraEvent(
            camera_id="CAM-04",
            device_serial="DS-SERIAL-04",
            channel_id=1,
            event_type="linedetection",
            detection_target="vehicle",
            region_id="1",
            channel_name="B1 Parking",
            trigger_time=datetime.now(UTC),
            raw_xml="<fake>xml</fake>",
            event_description="Vehicle detected",
            snapshot_path="detection_images/test_cam04.jpg"
        )
        
        # Test Event 2: ANPR on CAM-ENTRY (Gate, should PASS)
        event2 = ParsedCameraEvent(
            camera_id="CAM-ENTRY",
            device_serial="DS-SERIAL-ENTRY",
            channel_id=1,
            event_type="ANPR",
            detection_target="vehicle",
            region_id="entry",
            channel_name="Main Entry",
            trigger_time=datetime.now(UTC),
            raw_xml="<fake>xml</fake>",
            plate_number="HUD-9444",
            gate="entry",
            event_description="Vehicle detected",
            snapshot_path="detection_images/test_entry.jpg"
        )

        # Test Event 3: ANPR on CAM-04 (Not a gate, should be BLOCKED)
        event3 = ParsedCameraEvent(
            camera_id="CAM-04",
            device_serial="DS-SERIAL-04",
            channel_id=1,
            event_type="ANPR",
            detection_target="vehicle",
            region_id="1",
            channel_name="B1 Parking",
            trigger_time=datetime.now(UTC),
            raw_xml="<fake>xml</fake>",
            plate_number="ABC-123",
            event_description="Vehicle detected",
        )

        add_event_to_feed(db, event1)
        add_event_to_feed(db, event2)
        add_event_to_feed(db, event3)
        
        db.commit()
        print("Events processed.")
        
        # Query results
        results = db.query(CameraFeed).order_by(CameraFeed.id.desc()).limit(5).all()
        print("\nEntries in CameraFeed table (last 5):")
        for r in results:
            print(f"ID: {r.id} | Cam: {r.camera_id} | Label: {r.location_label} | Source: {r.detection_source}")

    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    test_camera_feed()
