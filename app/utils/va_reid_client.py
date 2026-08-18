"""Ask Video Analytics which open session a departing car looks like.

VA owns the appearance model and the per-plate gallery; PMS-AI owns the sessions
and the decision. This client carries a crop one way and similarities back, and
NEVER raises into the exit path: an unreachable or unhappy VA returns None, and
the caller then refuses the match — the same outcome as before this existed.

The gallery is keyed by plate, which is the very field we suspect is wrong — but
that is not a problem here. We look candidates up by the plate on their OPEN
SESSION, and the session and its gallery folder were written under the same
(possibly wrong) key by the same entry event. They stay consistent, so the
folder holds images of the physical car that entered under that name.
"""

import base64
import os
from typing import Optional

import httpx

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

COMPARE_PATH = "/api/reid/compare"
RENAME_PATH = "/api/reid/rename"
# Guards against handing a multi-megabyte overview frame to the model.
_MAX_IMAGE_BYTES = 6 * 1024 * 1024


def _read_image(path: Optional[str]) -> Optional[bytes]:
    """Load a local snapshot, or None when it is missing/absurd."""
    if not path or not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
        if size == 0 or size > _MAX_IMAGE_BYTES:
            logger.warning("[ReID] snapshot %s has implausible size %s", path, size)
            return None
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as exc:
        logger.warning("[ReID] could not read snapshot %s: %r", path, exc)
        return None


async def compare(image_path: str, plates: list) -> Optional[dict]:
    """Score `image_path` against each plate's VA gallery.

    Returns VA's payload, or None when scoring was not possible for ANY reason —
    disabled, unconfigured, unreadable image, timeout, transport error, or a
    non-200. Every one of those means "no appearance evidence", never "no match".
    """
    if not settings.EXIT_MATCH_REID_ENABLED:
        return None
    if not settings.PMS_API_URL or not settings.ENTRY_V2_SERVICE_KEY:
        logger.debug("[ReID] compare skipped — PMS_API_URL/service key not set")
        return None
    if not plates:
        return None

    raw = _read_image(image_path)
    if raw is None:
        return None

    url = settings.PMS_API_URL.rstrip("/") + COMPARE_PATH
    body = {
        "image_base64": base64.b64encode(raw).decode("ascii"),
        "plates": list(plates),
    }
    try:
        async with httpx.AsyncClient(
            timeout=settings.EXIT_MATCH_REID_TIMEOUT_SECONDS
        ) as client:
            response = await client.post(
                url,
                json=body,
                headers={"X-Service-Key": settings.ENTRY_V2_SERVICE_KEY},
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning("[ReID] compare unreachable: %r", exc)
        return None

    if response.status_code != 200:
        logger.warning(
            "[ReID] compare HTTP %s: %s", response.status_code, response.text[:200]
        )
        return None
    try:
        return response.json()
    except ValueError:
        logger.warning("[ReID] compare returned non-JSON")
        return None


async def rename(old_plate: str, new_plate: str) -> bool:
    """Tell VA a car was filed under the wrong plate. Never raises.

    Fire-and-forget by contract: `apply_correction` has already committed by the
    time this runs, and a VA that is down must not undo a correction PMS-AI has
    made. Returns whether VA acknowledged, for logging only — no caller branches
    on it.

    What is lost when this fails is not the correction but its propagation: VA
    keeps the crops under the misread and rewrites `parking_slots.current_plate`
    back to it on the next slot update. The exit sweep re-runs corrections, so a
    missed call is repaired rather than permanent.
    """
    if not settings.PMS_API_URL or not old_plate or not new_plate:
        return False
    if old_plate == new_plate:
        return False

    url = settings.PMS_API_URL.rstrip("/") + RENAME_PATH
    try:
        async with httpx.AsyncClient(
            timeout=settings.EXIT_MATCH_REID_TIMEOUT_SECONDS
        ) as client:
            response = await client.post(
                url,
                json={"from": old_plate, "to": new_plate},
                headers={"X-Service-Key": settings.ENTRY_V2_SERVICE_KEY},
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning(
            "[ReID] rename %s -> %s unreachable: %r", old_plate, new_plate, exc
        )
        return False

    if response.status_code != 200:
        logger.warning(
            "[ReID] rename %s -> %s HTTP %s: %s",
            old_plate, new_plate, response.status_code, response.text[:200],
        )
        return False
    logger.info("[ReID] renamed %s -> %s in VA: %s",
                old_plate, new_plate, response.text[:200])
    return True
