# scripts/test/test_camera_conn.py
"""
Tests connectivity to all cameras by hitting their ISAPI deviceInfo endpoint.
Usage: python scripts/test/test_camera_conn.py
       python scripts/test/test_camera_conn.py --phase 1
       python scripts/test/test_camera_conn.py --phase 2
"""

import sys
import os
import argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import requests
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET
from app.config import settings


def test_camera(cam_id: str, cam: dict) -> dict:
    url = f"http://{cam['ip']}/ISAPI/System/deviceInfo"
    try:
        resp = requests.get(
            url,
            auth=HTTPDigestAuth(cam["user"], cam["password"]),
            timeout=5,
        )
        if resp.status_code == 200:
            # Parse device info from XML
            try:
                root = ET.fromstring(resp.text)
                ns = "http://www.isapi.org/ver20/XMLSchema"
                def get(tag):
                    el = root.find(f"{{{ns}}}{tag}") or root.find(tag)
                    return el.text.strip() if el is not None and el.text else "N/A"
                model = get("model")
                serial = get("serialNumber")
                firmware = get("firmwareVersion")
            except Exception:
                model, serial, firmware = "N/A", "N/A", "N/A"

            return {"status": "✅ online", "model": model, "serial": serial, "firmware": firmware}
        elif resp.status_code == 401:
            return {"status": "❌ auth_failed", "hint": "Wrong username or password"}
        else:
            return {"status": f"❌ http_{resp.status_code}"}

    except requests.exceptions.ConnectTimeout:
        return {"status": "❌ timeout", "hint": "Camera unreachable — check IP and network"}
    except requests.exceptions.ConnectionError:
        return {"status": "❌ connection_refused", "hint": "No device at this IP"}
    except Exception as e:
        return {"status": f"❌ error: {e}"}


def test_webhook_registered(cam_id: str, cam: dict) -> str:
    """Check if backend HTTP host is already configured on the camera."""
    url = f"http://{cam['ip']}/ISAPI/Event/notification/httpHosts"
    try:
        resp = requests.get(
            url,
            auth=HTTPDigestAuth(cam["user"], cam["password"]),
            timeout=5,
        )
        if resp.status_code == 200 and settings.BACKEND_IP in resp.text:
            return f"✅ webhook registered → {settings.BACKEND_IP}:{settings.BACKEND_PORT}"
        elif resp.status_code == 200:
            return "⚠️  webhook NOT configured (run configure_cameras.py)"
        else:
            return f"❓ cannot check ({resp.status_code})"
    except Exception:
        return "❓ cannot check (camera offline)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["1", "2", "all"], default="all")
    args = parser.parse_args()
    target_phases = {1, 2} if args.phase == "all" else {int(args.phase)}

    print("📷 Damanat Camera Connectivity Test")
    print("=" * 55)

    all_ok = True
    for cam_id, cam in settings.CAMERAS.items():
        if cam["phase"] not in target_phases:
            continue

        print(f"\n[{cam_id}] Phase {cam['phase']} — {cam['ip']}")
        result = test_camera(cam_id, cam)

        print(f"  Status   : {result['status']}")
        if result["status"].startswith("✅"):
            print(f"  Model    : {result.get('model', 'N/A')}")
            print(f"  Serial   : {result.get('serial', 'N/A')}")
            print(f"  Firmware : {result.get('firmware', 'N/A')}")
            webhook = test_webhook_registered(cam_id, cam)
            print(f"  Webhook  : {webhook}")
        else:
            all_ok = False
            if "hint" in result:
                print(f"  Hint     : {result['hint']}")

    print("\n" + "=" * 55)
    if all_ok:
        print("✅ All cameras reachable and ready.")
    else:
        print("⚠️  Some cameras have issues. Fix them before starting the backend.")
        sys.exit(1)


if __name__ == "__main__":
    main()
