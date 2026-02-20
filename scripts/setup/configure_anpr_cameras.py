# scripts/setup/configure_anpr_cameras.py
"""
🔜 Phase 2: ANPR camera-specific configuration.
Enables LPR (License Plate Recognition) and pre-registers vehicle plates.

Usage:
    python scripts/setup/configure_anpr_cameras.py
"""

import sys
import os
import argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import requests
from requests.auth import HTTPDigestAuth
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

ANPR_CAMERAS = ["CAM-ENTRY", "CAM-EXIT"]


def enable_lpr(cam_id: str, cam: dict):
    """Enable License Plate Recognition on an ANPR camera."""
    ip = cam["ip"]
    auth = HTTPDigestAuth(cam["user"], cam["password"])
    print(f"\n[{cam_id}] Enabling LPR on {ip}...")

    try:
        resp = requests.get(
            f"http://{ip}/ISAPI/Traffic/channels/1/vehicleDetect/capabilities",
            auth=auth,
            timeout=5,
        )
        if resp.status_code == 200:
            print(f"  ✅ LPR capabilities confirmed")
        else:
            print(f"  ⚠️  LPR capabilities check: {resp.status_code}")
    except Exception as e:
        print(f"  ❌ Cannot reach camera: {e}")
        return

    # Enable vehicle detection
    lpr_config = """<?xml version="1.0" encoding="UTF-8"?>
    <VehicleDetect version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
      <enabled>true</enabled>
    </VehicleDetect>"""

    try:
        resp = requests.put(
            f"http://{ip}/ISAPI/Traffic/channels/1/vehicleDetect",
            data=lpr_config.encode("utf-8"),
            auth=auth,
            headers={"Content-Type": "application/xml"},
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"  ✅ Vehicle detection enabled")
        else:
            print(f"  ⚠️  Vehicle detection config: {resp.status_code}")
    except Exception as e:
        print(f"  ❌ Failed to enable: {e}")


def register_plate(cam_id: str, cam: dict, plate: str, name: str = "", employee_id: str = ""):
    """Pre-register a vehicle plate on an ANPR camera."""
    ip = cam["ip"]
    auth = HTTPDigestAuth(cam["user"], cam["password"])

    card_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
    <CardInfo version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
      <cardNo>{plate}</cardNo>
      <cardType>normalCard</cardType>
      <name>{name}</name>
      <employeeNo>{employee_id}</employeeNo>
    </CardInfo>"""

    try:
        resp = requests.post(
            f"http://{ip}/ISAPI/AccessControl/CardInfo/Record?format=xml",
            data=card_xml.encode("utf-8"),
            auth=auth,
            headers={"Content-Type": "application/xml"},
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"  ✅ Plate {plate} registered on {cam_id}")
        else:
            print(f"  ⚠️  Plate {plate}: {resp.status_code}")
    except Exception as e:
        print(f"  ❌ Plate {plate}: {e}")


def main():
    print("🚗 Damanat ANPR Camera Configuration (Phase 2)")
    print("=" * 50)

    for cam_id in ANPR_CAMERAS:
        cam = settings.CAMERAS.get(cam_id)
        if not cam:
            print(f"⚠️  {cam_id} not found in settings")
            continue
        enable_lpr(cam_id, cam)

    print("\n✅ ANPR configuration complete")
    print("\nTo register vehicle plates:")
    print("  Use POST /api/v1/vehicles to register in the backend DB")
    print("  This script can also pre-register plates on cameras if needed")


if __name__ == "__main__":
    main()
