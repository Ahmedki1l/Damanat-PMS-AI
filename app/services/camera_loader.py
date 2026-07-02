# app/services/camera_loader.py
"""
Load the camera inventory from the gateway-owned `cameras` DB table at startup
and populate the in-memory dicts the rest of the app reads
(`settings.CAMERAS`, `settings.CAMERA_IP_MAP`).

Why: cameras (including new ones, e.g. CAM-15..CAM-23) are managed in the shared
MSSQL `damanat_pms` DB, not in `.env`. The `.env` `CAM_XX_*` vars and the
`@model_validator` in config.py still build a baseline at import time — that
baseline is now used only as a **per-field fallback** when the DB is
unreachable, a value is missing, or a password can't be decrypted.

Design:
  - The DB `camera_id` values (`Cam_03`, `ANPR-Entry`, ...) are normalized to the
    canonical ids the codebase keys on (`CAM-03`, `CAM-ENTRY`, ...).
  - Passwords are Fernet-decrypted with CAMERAS_ENCRYPTION_KEY; on failure the
    `.env` password for that camera is used instead.
  - Gate (entry/exit) and phase are derived in code — the `cameras` table has no
    such columns. Gate rules come from config.apply_gate_rules (single source).
  - The whole load is best-effort: any exception leaves the env-built dicts
    intact so the service still boots with the last-known-good inventory.
"""

from app.config import settings, apply_gate_rules
from app.database import SessionLocal
from app.repositories.camera_repository import fetch_enabled_camera_rows
from app.utils.crypto import make_decrypt
from app.utils.logger import get_logger

logger = get_logger(__name__)

# DB ids that don't follow the simple `Cam_NN` -> `CAM-NN` pattern.
_ID_ALIASES = {
    "ANPR-ENTRY": "CAM-ENTRY",
    "ANPR-EXIT": "CAM-EXIT",
}

# Canonical ids that are Phase-2 ANPR gate cameras (everything else is Phase 1).
_PHASE2_IDS = {"CAM-ENTRY", "CAM-EXIT"}


def normalize_camera_id(raw: str) -> str:
    """Map a DB `camera_id` to the canonical id used throughout the codebase.

    `Cam_03` -> `CAM-03`, `ANPR-Entry` -> `CAM-ENTRY`, `CAM-15` -> `CAM-15`.
    """
    canonical = (raw or "").strip().upper().replace("_", "-")
    return _ID_ALIASES.get(canonical, canonical)


def load_cameras_from_db() -> None:
    """Replace settings.CAMERAS / CAMERA_IP_MAP with the DB inventory.

    Best-effort: on any failure the existing (env-built) dicts are left
    untouched. CAMERA_SERIAL_MAP is left as-is (no serial column in the DB).
    """
    key_set = bool(settings.CAMERAS_ENCRYPTION_KEY)
    try:
        decrypt = make_decrypt(settings.CAMERAS_ENCRYPTION_KEY)
    except Exception as e:
        logger.warning(
            f"[CameraLoader] Invalid CAMERAS_ENCRYPTION_KEY ({e}); "
            "keeping .env-built inventory."
        )
        return

    db = SessionLocal()
    try:
        rows = fetch_enabled_camera_rows(db)
    except Exception as e:
        logger.warning(
            f"[CameraLoader] Could not load cameras from DB ({e}); "
            "keeping .env-built inventory."
        )
        return
    finally:
        db.close()

    if not rows:
        logger.warning("[CameraLoader] cameras table returned 0 enabled rows; keeping .env inventory.")
        return

    cameras: dict = {}
    decrypt_failures: list[str] = []

    for row in rows:
        cam_id = normalize_camera_id(row.camera_id)
        if not cam_id:
            continue

        # .env baseline for this camera (may be empty for DB-only cameras).
        env = settings.CAMERAS.get(cam_id, {})

        password = decrypt(row.password_encrypted)
        if password is None:
            # Blank token, undecryptable, or no key -> fall back to .env. Only
            # flag a real failure when a key WAS set but the token didn't
            # decrypt (a blank key is the expected "not rolled out yet" state).
            if key_set and row.password_encrypted:
                decrypt_failures.append(cam_id)
            password = env.get("password", "")

        cameras[cam_id] = {
            "ip": row.ip_address or env.get("ip", ""),
            "user": row.username or env.get("user", ""),
            "password": password,
            "phase": 2 if cam_id in _PHASE2_IDS else 1,
            "name": row.name or env.get("name", cam_id),
        }

    apply_gate_rules(cameras)

    settings.CAMERAS = cameras
    settings.CAMERA_IP_MAP = {
        cam["ip"]: cam_id for cam_id, cam in cameras.items() if cam.get("ip")
    }

    logger.info(
        f"[CameraLoader] Loaded {len(cameras)} cameras from DB: "
        f"{sorted(cameras.keys())}"
    )
    if decrypt_failures:
        logger.warning(
            "[CameraLoader] Password decrypt failed for "
            f"{sorted(set(decrypt_failures))} — used .env fallback. "
            "Check CAMERAS_ENCRYPTION_KEY."
        )
