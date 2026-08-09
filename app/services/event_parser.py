# app/services/event_parser.py
"""
Parses both Phase 1 (XML) and Phase 2 (JSON/ANPR) camera event payloads.
Returns a unified ParsedCameraEvent regardless of source.
Handles multipart/form-data for image extraction.
"""

import json
from io import BytesIO
import math
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict
from PIL import Image, UnidentifiedImageError
from app.config import facility_now_naive, facility_tz, settings
from app.services.snapshot_service import to_public_snapshot_url
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Namespaces commonly used by Hikvision ISAPI
NS_ISAPI = "http://www.isapi.org/ver20/XMLSchema"
NS_HIKVISION = "http://www.hikvision.com/ver20/XMLSchema"

SNAPSHOT_DIR = "detection_images"
os.makedirs(SNAPSHOT_DIR, exist_ok=True)


class CameraPayloadRejected(ValueError):
    """The camera request is deterministically malformed and must not retry."""


def _parse_camera_time(raw_value: Optional[str]) -> tuple[datetime, str]:
    """Return an aware event time plus auditable timestamp provenance."""
    received_at = datetime.now(facility_tz())
    value = str(raw_value or "").strip()
    if not value:
        return received_at, "pms_receive_missing"
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        logger.warning("Could not parse camera timestamp: %r", raw_value)
        return received_at, "pms_receive_invalid"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        logger.warning(
            "Camera timestamp has no timezone; assuming configured facility timezone"
        )
        return (
            parsed.replace(tzinfo=facility_tz()),
            "camera_assumed_facility_timezone",
        )
    return parsed, "camera"


@dataclass(frozen=True)
class VehicleBoundingBox:
    """Camera-reported vehicle box in pixels or normalized 0..1 units."""

    x: float
    y: float
    width: float
    height: float
    normalized: bool


@dataclass(frozen=True)
class VehicleImageRegion:
    """A camera rectangle tied to one multipart filename or Content-ID."""

    identifiers: tuple[str, ...]
    bbox: VehicleBoundingBox


@dataclass(frozen=True)
class TransientImage:
    """One multipart image or derived vehicle crop kept only in memory."""

    data: bytes
    content_type: str
    filename: str
    source_name: Optional[str] = None
    role: Optional[str] = None


@dataclass
class ParsedCameraEvent:
    camera_id: str
    device_serial: str
    channel_id: int
    event_type: str            # fielddetection | linedetection | regionEntrance | regionExiting | AccessControllerEvent
    detection_target: Optional[str]  # vehicle | human | others (Phase 1)
    region_id: Optional[str]
    channel_name: Optional[str]
    trigger_time: datetime
    raw_xml: str
    trigger_time_source: str = "camera"
    # Extra XML fields
    event_state: Optional[str] = None      # active | inactive
    event_description: Optional[str] = None  # human-readable description
    snapshot_path: Optional[str] = None    # path to saved snapshot image
    local_snapshot_path: Optional[str] = None  # original local file (preserved for PMS forwarding)
    # Phase 2 ANPR fields
    plate_number: Optional[str] = None     # cardNo from AccessControllerEvent
    user_type: Optional[str] = None        # normal | visitor | blacklist
    person_name: Optional[str] = None
    employee_id: Optional[str] = None
    gate: Optional[str] = None             # entry | exit (from settings mapping)
    crossing_direction: Optional[str] = None # A-to-B | B-to-A
    # ANPR per-read burst fields (one car fires several reads as it approaches).
    # picNum resets per vehicle; confidence is for audit / tie-break only.
    plate_confidence: Optional[int] = None  # <confidenceLevel> 0-100
    pic_num: Optional[int] = None           # <picNum> picture index within the burst
    # V2 evidence is forwarded directly from request memory. It is deliberately
    # not a filesystem path and is never written by the authoritative V2 flow.
    transient_images: tuple[TransientImage, ...] = ()
    vehicle_bbox: Optional[VehicleBoundingBox] = None
    vehicle_image_regions: tuple[VehicleImageRegion, ...] = ()
    pending_legacy_image: Optional[TransientImage] = None


def _split_multipart(raw_body: bytes, content_type: str) -> List[Dict]:
    """Split multipart body into a list of {headers, body, content_type, filename} dicts."""
    match = re.search(
        r'(?:^|;)\s*boundary\s*=\s*(?:"((?:\\.|[^"\\])*)"|([^;\s]+))',
        content_type,
        flags=re.IGNORECASE,
    )
    if not match:
        logger.warning("Multipart content-type but no boundary found")
        return []

    if match.group(1) is not None:
        boundary_value = re.sub(r"\\(.)", r"\1", match.group(1))
    else:
        boundary_value = match.group(2)
    try:
        boundary = boundary_value.encode("ascii")
    except (AttributeError, UnicodeEncodeError):
        logger.warning("Multipart boundary is not valid ASCII")
        return []
    if not boundary or len(boundary) > 70:
        logger.warning("Multipart boundary length is invalid")
        return []
    # Boundaries in body start with --
    raw_parts = raw_body.split(b"--" + boundary)
    result = []

    for part in raw_parts:
        part = part.strip()
        if not part or part == b"--":
            continue

        # Split headers from body
        if b"\r\n\r\n" in part:
            headers_block, body = part.split(b"\r\n\r\n", 1)
        elif b"\n\n" in part:
            headers_block, body = part.split(b"\n\n", 1)
        else:
            continue

        headers_str = headers_block.decode("utf-8", errors="replace").lower()

        # Extract content-type
        ct_match = re.search(r"content-type:\s*([^\r\n;]+)", headers_str)
        part_ct = ct_match.group(1).strip() if ct_match else ""

        # Extract filename
        fn_match = re.search(r'filename="([^"]+)"', headers_str)
        filename = fn_match.group(1) if fn_match else None
        name_match = re.search(
            r'content-disposition:[^\r\n]*;\s*name="([^"]+)"',
            headers_str,
        )
        part_name = name_match.group(1) if name_match else None
        content_id_match = re.search(r"content-id:\s*([^\r\n]+)", headers_str)
        content_id = content_id_match.group(1).strip() if content_id_match else None

        result.append({
            "headers": headers_str,
            "body": body.rstrip(b"\r\n"),
            "content_type": part_ct,
            "filename": filename,
            "name": part_name,
            "content_id": content_id,
        })

    return result


def _extract_from_multipart(
    raw_body: bytes,
    content_type: str,
) -> tuple[bytes, tuple[TransientImage, ...]]:
    """
    Extract the XML/JSON payload from a multipart body.
    Image parts are returned as in-memory values; the caller decides whether a
    legacy path should persist the first image after the event is classified.
    """
    parts = _split_multipart(raw_body, content_type)
    if not parts:
        raise CameraPayloadRejected("invalid multipart camera payload")
    images: list[TransientImage] = []

    # Priority 1: collect every image part for the V2 repeated-images contract.
    for p in parts:
        ct = p["content_type"]
        if "image/" in ct.lower():
            images.append(
                TransientImage(
                    data=p["body"],
                    content_type=ct or "application/octet-stream",
                    filename=p["filename"] or f"evidence-{len(images) + 1}.jpg",
                    source_name=p["content_id"] or p["name"],
                )
            )

    # Priority 2: Return XML or JSON parts
    for p in parts:
        ct = p["content_type"]
        if any(t in ct for t in ("text/xml", "application/xml", "application/json", "text/plain")):
            logger.debug(f"Extracted multipart metadata part ({len(p['body'])} bytes)")
            return p["body"], tuple(images)

    raise CameraPayloadRejected("multipart camera payload has no metadata part")


def _is_v2_evidence(event: ParsedCameraEvent) -> bool:
    if event.event_type in ("ANPR", "AccessControllerEvent"):
        gate = settings.CAMERAS.get(event.camera_id, {}).get("gate", event.gate)
        return gate != "exit"
    return (
        event.event_type == "linedetection"
        and event.camera_id in {"CAM-23", "CAM-03"}
        and str(event.event_state or "").strip().lower() == "active"
        and str(event.detection_target or "").strip().lower() == "vehicle"
    )


def _configured_confirmation_marker(camera_id: str) -> str:
    """Return the existing semantic marker for one confirmation camera."""
    for item in settings.ENTRY_CONFIRM_DIRECTIONS.split(","):
        camera, separator, direction = item.strip().partition(":")
        if separator and camera.strip() == camera_id and direction.strip():
            return direction.strip()
    return ""


def crossing_matches_configured_entry_filter(event: ParsedCameraEvent) -> bool:
    """Match the configured inward boundary without authoritative wildcards."""
    if event.event_type != "linedetection" or event.camera_id not in {
        "CAM-23",
        "CAM-03",
    }:
        return False
    confirmation_cameras = {
        camera.strip()
        for camera in settings.ENTRY_CONFIRM_CAMERAS.split(",")
        if camera.strip()
    }
    if event.camera_id not in confirmation_cameras:
        return False

    if event.camera_id == "CAM-03":
        # CAM-03 is also an occupancy camera. Reuse its existing calibrated
        # entrance line/raw direction instead of treating every line as entry.
        # The confirmation marker must also exist so the fallback cannot become
        # active under a partial confirmation-camera configuration.
        if not _configured_confirmation_marker("CAM-03"):
            return False
        expected_line = settings.OCCUPANCY_ENTRANCE_ZONES.strip()
        expected_direction = settings.FORWARD_DIRECTION_FIELD.strip()
        actual_line = str(event.region_id or "").strip()
        actual_direction = str(event.crossing_direction or "").strip()
        line_matches = bool(expected_line and actual_line == expected_line)
        direction_matches = bool(
            expected_direction and actual_direction == expected_direction
        )
        if expected_line and actual_line and not line_matches:
            return False
        if expected_direction and actual_direction and not direction_matches:
            return False
        return line_matches or direction_matches

    expected_line = settings.CAM23_ENTRY_LINE.strip()
    expected_direction = settings.CAM23_ENTRY_DIRECTION.strip()
    if not expected_line and not expected_direction:
        # Shadow deliberately permits uncalibrated observation so operators can
        # learn the raw Hikvision values. Authoritative mode must never turn two
        # empty filters into an implicit match-all entry boundary.
        return settings.ENTRY_V2_MODE != "authoritative"
    return (
        (not expected_line or str(event.region_id or "") == expected_line)
        and (
            not expected_direction
            or str(event.crossing_direction or "") == expected_direction
        )
    )


def _is_authoritative_v2_evidence(event: ParsedCameraEvent) -> bool:
    """Whether this event must remain memory-only in authoritative V2."""
    # An unresolved ANPR stays memory-only until the router applies trusted
    # alias/VMR camera identity. It is never assumed to be entry for upload.
    return settings.ENTRY_V2_MODE == "authoritative" and _is_v2_evidence(event)


def _image_identifier(image: TransientImage) -> str:
    return " ".join(
        part.lower()
        for part in (image.filename, image.source_name or "")
        if part
    )


def _bbox_presence_is_plausible(bbox: VehicleBoundingBox) -> bool:
    """Whether a rectangle is sane enough to mean "a vehicle was detected here".

    The ANPR full-frame path uses only the rectangle's PRESENCE, never its
    coordinates — but a rectangle carrying non-finite or overflowing values is
    corrupt payload, not a detection. Without this check such a body would stop
    being rejected (its coordinates are no longer read), get forwarded, and turn
    a deterministically-malformed event into a retryable failure the camera
    repeats forever. Mirrors the padding-overflow guard in _crop_vehicle_image.
    """
    padding = settings.ENTRY_V2_CROP_PADDING_RATIO
    if not all(
        math.isfinite(value)
        for value in (bbox.x, bbox.y, bbox.width, bbox.height)
    ):
        return False
    return all(
        math.isfinite(edge)
        for edge in (
            bbox.x - bbox.width * padding,
            bbox.y - bbox.height * padding,
            bbox.x + bbox.width * (1 + padding),
            bbox.y + bbox.height * (1 + padding),
        )
    )


def _image_pixel_size(image: TransientImage) -> str:
    """``WxH`` from the encoded header alone, or ``?x?`` when unreadable.

    Header-only read (PIL does not decode until ``load()``), so this stays cheap
    enough for the per-part diagnostic below and can never raise into the
    webhook path.
    """
    try:
        with Image.open(BytesIO(image.data)) as decoded:
            return f"{decoded.size[0]}x{decoded.size[1]}"
    except Exception:
        return "?x?"


def _log_v2_part_selection(
    event: ParsedCameraEvent,
    image: TransientImage,
    route: str,
    forwarded: Optional[TransientImage] = None,
    bbox: Optional[VehicleBoundingBox] = None,
) -> None:
    """Record which multipart part became Entry V2 evidence, and at what size.

    VA's plate OCR can only be as good as the pixels it is handed, but the crop
    it receives is memory-only — when a plate arrives already truncated there is
    no artefact left showing WHICH camera part it came from or how it was cut.
    Logging the identifier beside the source and forwarded dimensions makes a
    camera-side crop that is framed on the plate rather than the vehicle
    distinguishable from a bad rectangle, without persisting evidence images.
    """
    logger.info(
        "[EntryV2][part] camera=%s type=%s route=%s id=%r source=%s bytes=%d "
        "bbox=%s forwarded=%s",
        event.camera_id,
        event.event_type,
        route,
        _image_identifier(image) or image.filename,
        _image_pixel_size(image),
        len(image.data),
        "none" if bbox is None else (
            f"{bbox.x:.4g},{bbox.y:.4g},{bbox.width:.4g},{bbox.height:.4g}"
            f"{'n' if bbox.normalized else 'px'}"
        ),
        "dropped" if forwarded is None else _image_pixel_size(forwarded),
    )


def _identifier_keys(*values: Optional[str]) -> set[str]:
    """Normalize MIME/XML image identifiers without fuzzy substring matching."""
    keys: set[str] = set()
    for value in values:
        if not value:
            continue
        normalized = value.strip().strip("<>").lower()
        if not normalized:
            continue
        basename = re.split(r"[/\\]", normalized)[-1]
        keys.update({normalized, basename})
        if "." in basename:
            keys.add(basename.rsplit(".", 1)[0])
    return keys


def _vehicle_bbox_for_image(
    event: ParsedCameraEvent,
    image: TransientImage,
    image_count: int,
) -> Optional[VehicleBoundingBox]:
    """Resolve the rectangle belonging to this exact multipart image part."""
    regions = event.vehicle_image_regions
    if not regions:
        return event.vehicle_bbox

    image_keys = _identifier_keys(image.filename, image.source_name)
    matches = [
        region
        for region in regions
        if image_keys.intersection(_identifier_keys(*region.identifiers))
    ]
    if not matches and image_count == 1 and len(regions) == 1:
        return regions[0].bbox
    if len(matches) == 1:
        return matches[0].bbox
    if len(matches) > 1 and len({region.bbox for region in matches}) == 1:
        return matches[0].bbox

    reason = "ambiguous" if matches else "missing"
    logger.warning(
        "[EntryV2] %s pictureInfo rectangle for multipart image %s",
        reason,
        image.filename,
    )
    return None


def _crop_vehicle_image(
    source: TransientImage,
    bbox: VehicleBoundingBox,
    output_index: int,
    *,
    full_frame_fallback: bool = False,
) -> Optional[TransientImage]:
    """Decode, clamp, pad, and JPEG-encode one vehicle crop in memory."""
    if len(source.data) > settings.ENTRY_V2_MAX_SOURCE_IMAGE_BYTES:
        logger.warning(
            "[EntryV2] Rejecting oversized source image %s (%d bytes)",
            source.filename,
            len(source.data),
        )
        return None
    if bbox.width <= 0 or bbox.height <= 0:
        logger.warning("[EntryV2] Rejecting non-positive vehicle rectangle")
        return None
    if bbox.normalized and not (
        0 <= bbox.x <= 1
        and 0 <= bbox.y <= 1
        and 0 < bbox.width <= 1
        and 0 < bbox.height <= 1
    ):
        logger.warning("[EntryV2] Rejecting invalid normalized vehicle rectangle")
        return None

    try:
        with Image.open(BytesIO(source.data)) as decoded:
            image_width, image_height = decoded.size
            if (
                image_width <= 0
                or image_height <= 0
                or image_width * image_height
                > settings.ENTRY_V2_MAX_SOURCE_DECODED_PIXELS
            ):
                logger.warning(
                    "[EntryV2] Rejecting decoded image dimensions %sx%s",
                    image_width,
                    image_height,
                )
                return None

            if bbox.normalized:
                if not all(
                    math.isfinite(value)
                    for value in (bbox.x, bbox.y, bbox.width, bbox.height)
                ):
                    return None
                x = bbox.x * image_width
                y = bbox.y * image_height
                width = bbox.width * image_width
                height = bbox.height * image_height
            else:
                x, y, width, height = bbox.x, bbox.y, bbox.width, bbox.height

            if not all(math.isfinite(value) for value in (x, y, width, height)):
                return None
            # Require the unpadded box to overlap the decoded image. Padding is
            # contextual only; it must never turn an off-image box into evidence.
            if x >= image_width or y >= image_height or x + width <= 0 or y + height <= 0:
                logger.warning("[EntryV2] Vehicle rectangle is outside decoded image")
                return None

            padding = settings.ENTRY_V2_CROP_PADDING_RATIO
            if padding < 0 or padding > 0.5:
                logger.error("[EntryV2] Invalid crop padding ratio: %s", padding)
                return None
            padded_edges = (
                x - width * padding,
                y - height * padding,
                x + width * (1 + padding),
                y + height * (1 + padding),
            )
            if not all(math.isfinite(value) for value in padded_edges):
                logger.warning("[EntryV2] Vehicle rectangle padding overflowed")
                return None
            padded_left, padded_top, padded_right, padded_bottom = padded_edges
            left = max(0, math.floor(padded_left))
            top = max(0, math.floor(padded_top))
            right = min(image_width, math.ceil(padded_right))
            bottom = min(image_height, math.ceil(padded_bottom))
            if right - left < 2 or bottom - top < 2:
                logger.warning("[EntryV2] Vehicle crop is empty after clamping")
                return None

            decoded.load()
            crop = decoded.crop((left, top, right, bottom))
            try:
                crop = _fit_crop_to_outbound_envelope(crop)
                if crop.mode != "RGB":
                    converted = crop.convert("RGB")
                    crop.close()
                    crop = converted
                data = _encode_jpeg_within_outbound_byte_limit(crop)
            finally:
                crop.close()
    except (
        OSError,
        UnidentifiedImageError,
        OverflowError,
        Image.DecompressionBombError,
    ) as exc:
        logger.warning(
            "[EntryV2] Could not derive vehicle crop from %s: %s",
            source.filename,
            exc,
        )
        return None

    if data is None:
        logger.warning(
            "[EntryV2] Could not fit encoded vehicle crop %s within %d bytes",
            source.filename,
            settings.ENTRY_V2_MAX_IMAGE_BYTES,
        )
        return None

    return TransientImage(
        data=data,
        content_type="image/jpeg",
        filename=(
            f"vehicle-full-frame-{output_index}.jpg"
            if full_frame_fallback
            else f"vehicle-crop-{output_index}.jpg"
        ),
        source_name=source.filename,
        role="vehicle",
    )


def _fit_crop_to_outbound_envelope(crop: Image.Image) -> Image.Image:
    """Downscale a crop to VA's exact decoded-pixel and dimension limits.

    The returned object owns its pixels. When resizing is needed this function
    closes the superseded image so callers still have exactly one object to
    close in their ``finally`` block.
    """
    width, height = crop.size
    max_pixels = settings.ENTRY_V2_MAX_DECODED_PIXELS
    max_dimension = settings.ENTRY_V2_MAX_IMAGE_DIMENSION
    if (
        width <= max_dimension
        and height <= max_dimension
        and width * height <= max_pixels
    ):
        return crop

    scale = min(
        1.0,
        max_dimension / width,
        max_dimension / height,
        math.sqrt(max_pixels / (width * height)),
    )
    target_width = max(1, min(max_dimension, math.floor(width * scale)))
    target_height = max(1, min(max_dimension, math.floor(height * scale)))

    # Floating-point rounding must never produce a one-pixel contract breach.
    if target_width * target_height > max_pixels:
        if target_width >= target_height:
            target_width = max(1, max_pixels // target_height)
        else:
            target_height = max(1, max_pixels // target_width)

    resized = crop.resize(
        (target_width, target_height),
        resample=Image.Resampling.LANCZOS,
    )
    crop.close()
    return resized


def _encode_jpeg_within_outbound_byte_limit(
    crop: Image.Image,
) -> Optional[bytes]:
    """Adapt JPEG quality and dimensions until the VA byte envelope is met."""
    max_bytes = settings.ENTRY_V2_MAX_IMAGE_BYTES
    qualities = (90, 82, 74, 66, 58, 50)
    working = crop
    owns_working = False
    try:
        for _ in range(8):
            last_size = 0
            for quality in qualities:
                output = BytesIO()
                working.save(
                    output,
                    format="JPEG",
                    quality=quality,
                    optimize=False,
                )
                data = output.getvalue()
                last_size = len(data)
                if last_size <= max_bytes:
                    return data

            width, height = working.size
            if width <= 2 and height <= 2:
                return None
            scale = min(0.9, math.sqrt(max_bytes / last_size) * 0.95)
            target_width = max(2, math.floor(width * scale))
            target_height = max(2, math.floor(height * scale))
            if (target_width, target_height) == (width, height):
                if width >= height and width > 2:
                    target_width -= 1
                elif height > 2:
                    target_height -= 1
                else:
                    return None
            resized = working.resize(
                (target_width, target_height),
                resample=Image.Resampling.LANCZOS,
            )
            if owns_working:
                working.close()
            working = resized
            owns_working = True
        return None
    finally:
        if owns_working:
            working.close()


def _prepare_v2_vehicle_images(
    event: ParsedCameraEvent,
    images: tuple[TransientImage, ...],
) -> tuple[TransientImage, ...]:
    """Select/crop only vehicle evidence; never forward plate or overview."""
    if not _is_v2_evidence(event):
        return ()

    if (
        event.event_type == "linedetection"
        and not crossing_matches_configured_entry_filter(event)
    ):
        # Apply the physical-boundary filter before every image path for both
        # the primary and fallback crossing cameras. Camera-side vehicle crops
        # and events carrying a target rectangle must not bypass calibration.
        logger.warning(
            "[EntryV2] Rejecting crossing images outside the configured entry "
            "boundary camera=%s line=%s direction=%s",
            event.camera_id,
            event.region_id,
            event.crossing_direction,
        )
        return ()

    prepared: list[TransientImage] = []
    for image in images:
        if len(prepared) >= settings.ENTRY_V2_MAX_IMAGES:
            logger.warning(
                "[EntryV2] Vehicle image limit reached; remaining parts ignored"
            )
            break
        identifier = _image_identifier(image)
        if "plate" in identifier:
            _log_v2_part_selection(event, image, "skipped_plate_part")
            continue
        if "vehiclepicture" in identifier or "vehiclecrop" in identifier:
            # Explicit vehicle-picture parts are camera-side crops, but still
            # decode/re-encode them to enforce byte/pixel bomb limits.
            whole = VehicleBoundingBox(0, 0, 1, 1, normalized=True)
            crop = _crop_vehicle_image(image, whole, len(prepared) + 1)
            # Forwarded as-is: whatever the camera framed IS the evidence, so a
            # part cropped tight on the plate reaches OCR already truncated.
            _log_v2_part_selection(event, image, "camera_side_crop", crop, whole)
            if crop is not None:
                prepared.append(crop)
            continue

        is_anpr_overview = "detectionpicture" in identifier
        is_line_overview = "linecrossimage" in identifier
        # Some firmware emits a single generically-named JPEG. It is usable only
        # when the matching camera rectangle is present, and it is still cropped.
        generic_single = len(images) == 1
        if not (is_anpr_overview or is_line_overview or generic_single):
            _log_v2_part_selection(event, image, "skipped_unmatched_id")
            continue

        vehicle_bbox = _vehicle_bbox_for_image(event, image, len(images))

        # `vehicelRect` on an ANPR overview points at Hikvision's own composited
        # plate/OSD overlay in the top-left, not at the car — so cropping to it
        # could only ever re-read the camera's own output. Discard its
        # COORDINATES and forward the bounded full frame so VA localizes the real
        # plate itself (see the setting's comment for measured numbers).
        #
        # The rectangle's PRESENCE is still required. It is Hikvision's "a vehicle
        # was detected here" signal, and it is the only thing separating a real
        # car from a spurious ANPR trigger. Keeping that gate preserves the
        # invariant that an ANPR event with no rectangle forwards nothing at all,
        # so a frame of the open road is never shipped as vehicle evidence.
        #
        # Line overviews are NOT affected: their rectangles are normalized 0..1
        # and correctly describe the vehicle, so they keep the exact crop.
        if (
            is_anpr_overview
            and settings.ENTRY_V2_ANPR_FULL_FRAME
            and vehicle_bbox is not None
            and _bbox_presence_is_plausible(vehicle_bbox)
        ):
            whole = VehicleBoundingBox(0, 0, 1, 1, normalized=True)
            crop = _crop_vehicle_image(
                image, whole, len(prepared) + 1, full_frame_fallback=True
            )
            _log_v2_part_selection(event, image, "anpr_full_frame", crop, whole)
            if crop is not None:
                prepared.append(crop)
            continue

        if vehicle_bbox is None:
            if crossing_matches_configured_entry_filter(event):
                logger.warning(
                    "[EntryV2][full_frame_fallback] Using bounded full frame "
                    "for camera=%s line=%s direction=%s image=%s because the "
                    "camera omitted a vehicle rectangle",
                    event.camera_id,
                    event.region_id,
                    event.crossing_direction,
                    image.filename,
                )
                whole = VehicleBoundingBox(0, 0, 1, 1, normalized=True)
                crop = _crop_vehicle_image(
                    image,
                    whole,
                    len(prepared) + 1,
                    full_frame_fallback=True,
                )
                _log_v2_part_selection(
                    event, image, "full_frame_fallback", crop, whole
                )
                if crop is not None:
                    prepared.append(crop)
                continue
            logger.warning(
                "[EntryV2] No vehicle rectangle for %s; full frame not forwarded",
                image.filename,
            )
            _log_v2_part_selection(event, image, "skipped_no_rectangle")
            continue
        crop = _crop_vehicle_image(image, vehicle_bbox, len(prepared) + 1)
        _log_v2_part_selection(event, image, "vehicle_rect_crop", crop, vehicle_bbox)
        if crop is not None:
            prepared.append(crop)
    return tuple(prepared)


def _save_legacy_multipart_image(
    event: ParsedCameraEvent,
    image: TransientImage,
) -> None:
    """Persist one multipart image for off/shadow legacy behavior only."""
    timestamp = facility_now_naive().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"snapshot_{event.event_type}_{event.camera_id}_{timestamp}.jpg"
    filepath = os.path.join(SNAPSHOT_DIR, filename)
    try:
        with open(filepath, "wb") as f:
            f.write(image.data)
    except OSError as exc:
        logger.error(f"Failed to save multipart image: {exc}")
        return

    logger.debug(f"Saved multipart image: {filename} ({len(image.data)} bytes)")
    event.local_snapshot_path = filepath
    event.snapshot_path = to_public_snapshot_url(filepath)


def finalize_camera_event_images(event: ParsedCameraEvent) -> None:
    """Resolve deferred authoritative multipart persistence after gate identity.

    UNKNOWN ANPR can later resolve to CAM-EXIT through a trusted alias or exact
    VMR hint. Exit must retain the legacy inline snapshot; entry/unresolved
    evidence must release the raw image without writing it.
    """
    image = event.pending_legacy_image
    if image is None:
        return
    event.pending_legacy_image = None
    if _is_authoritative_v2_evidence(event):
        return
    _save_legacy_multipart_image(event, image)
    event.transient_images = ()


def parse_camera_event(raw_body: bytes, camera_ip: str, content_type: str = "") -> ParsedCameraEvent:
    """Auto-detect format (Multipart, JSON, or XML) and parse accordingly."""
    # Handle multipart/form-data (common in Hikvision events with snapshots)
    multipart_images: tuple[TransientImage, ...] = ()
    if "multipart" in content_type.lower():
        raw_body, multipart_images = _extract_from_multipart(
            raw_body,
            content_type,
        )

    # Detect if body is JSON or XML. Format-specific parsers translate only
    # deterministic wire/coercion failures into CameraPayloadRejected; an
    # unexpected ValueError from later internal processing must remain retryable.
    is_json = "json" in content_type.lower() or raw_body.lstrip()[:1] == b"{"
    if is_json:
        event = _parse_json_event(raw_body, camera_ip)
    else:
        event = _parse_xml_event(raw_body, camera_ip)

    if multipart_images:
        if settings.ENTRY_V2_MODE != "off":
            event.transient_images = _prepare_v2_vehicle_images(
                event,
                multipart_images,
            )
        if _is_authoritative_v2_evidence(event):
            # Gate identity can still change from UNKNOWN to a trusted exit
            # before forwarding, so defer the legacy persistence decision.
            event.pending_legacy_image = multipart_images[0]
        else:
            _save_legacy_multipart_image(event, multipart_images[0])

    return event


def _parse_xml_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 1 XML events using ElementTree with full Namespace support."""
    xml_str = raw_body.decode("utf-8", errors="replace")
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        logger.error(f"Failed to parse XML: {e}")
        raise CameraPayloadRejected("malformed XML camera metadata") from e

    # Auto-detect namespace from root tag
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    logger.debug(f"XML namespace detected: {ns or '(none)'}")

    def find_text(tag, parent=root):
        """Helper to find text in a tag considering namespaces."""
        if ns:
            el = parent.find(f"{ns}{tag}")
            if el is not None:
                return el.text.strip() if el.text else None
        el = parent.find(tag)
        return el.text.strip() if el is not None and el.text else None

    def parse_bbox(parent, *, normalized: bool) -> Optional[VehicleBoundingBox]:
        if parent is None:
            return None
        try:
            x = float(find_text("X", parent) or find_text("x", parent))
            y = float(find_text("Y", parent) or find_text("y", parent))
            width = float(find_text("width", parent))
            height = float(find_text("height", parent))
        except (TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        return VehicleBoundingBox(x, y, width, height, normalized)

    # Resolve Trigger Time. Entry V2 requires an offset-aware value; when the
    # camera omits/breaks it, preserve the exact PMS receive instant and mark
    # that fallback in metadata instead of silently forwarding a naive value.
    t_str = find_text("triggerTime") or find_text("dateTime")
    trigger_time, trigger_time_source = _parse_camera_time(t_str)

    # Resolve Regions and Targets (UC3, UC6)
    region_id, detection_target = None, None
    vehicle_bbox = None
    reg_list = root.find(f"{ns}DetectionRegionList") if ns else root.find("DetectionRegionList")
    if reg_list is not None:
        entry = reg_list.find(f"{ns}DetectionRegionEntry") if ns else reg_list.find("DetectionRegionEntry")
        if entry is not None:
            region_id = find_text("regionID", entry)
            detection_target = find_text("detectionTarget", entry)
            target_rect = entry.find(f"{ns}TargetRect") if ns else entry.find("TargetRect")
            vehicle_bbox = parse_bbox(target_rect, normalized=True)

    # Fallback: real cameras nest regionID inside DetectionRegionList, but simple test
    # XMLs (and some older firmware) put it at the top level — support both.
    if region_id is None:
        region_id = find_text("regionID")
    if detection_target is None:
        detection_target = find_text("detectionTarget")

    # Prefer the <ipAddress> from XML body over the HTTP request's client IP.
    # This allows manual testing from localhost while still correctly identifying the camera.
    xml_ip = find_text("ipAddress") or camera_ip
    device_serial = find_text("deviceSerial") or "unknown"
    resolved_camera_id = settings.CAMERA_SERIAL_MAP.get(device_serial) \
        or settings.CAMERA_IP_MAP.get(xml_ip) \
        or settings.CAMERA_IP_MAP.get(camera_ip) \
        or f"UNKNOWN-{camera_ip}"

    # Resolve ANPR Plate (UC1, UC2, UC4)
    plate_number = find_text("licensePlateNumber") or find_text("plateNumber")

    # If it's an ANPR event, we should try to extract more info
    if (find_text("eventType") in ("ANPR", "vehicleMatchResult", "AccessControllerEvent")):
         # Robust plate search: check multiple common Hikvision tags
         if not plate_number:
            plate_number = find_text("licensePlateNumber") or find_text("plateNumber") or find_text("cardNo")

         # Try looking inside nested nodes like <ANPR> or <AccessControllerEvent>
         for node_name in ("ANPR", "AccessControllerEvent"):
             node = root.find(f"{ns}{node_name}") if ns else root.find(node_name)
             if node is not None and not plate_number:
                 plate_number = find_text("licensePlateNumber", node) or find_text("plateNumber", node) or find_text("cardNo", node)

         pass  # plate search continues below in the ANPR extraction block

    event_type = find_text("eventType") or "unknown"
    camera_id = resolved_camera_id

    # Bind every Hikvision pictureInfo rectangle to that picture's filename or
    # type. A global first-rectangle shortcut can crop the wrong vehicle when a
    # multipart event carries more than one overview image.
    vehicle_image_regions: list[VehicleImageRegion] = []
    picture_path = f".//{ns}pictureInfo" if ns else ".//pictureInfo"
    for picture_info in root.findall(picture_path):
        identifiers = tuple(
            identifier
            for identifier in (
                find_text("fileName", picture_info),
                find_text("type", picture_info),
                find_text("contentID", picture_info),
                find_text("contentId", picture_info),
            )
            if identifier
        )
        for rect_name in ("vehicelRect", "vehicleRect", "VehicleRect"):
            rect = picture_info.find(
                f"{ns}{rect_name}" if ns else rect_name
            )
            pixel_rect = parse_bbox(rect, normalized=False)
            if pixel_rect is not None:
                vehicle_image_regions.append(
                    VehicleImageRegion(identifiers=identifiers, bbox=pixel_rect)
                )
                break

    if len(vehicle_image_regions) == 1:
        # Safe fallback for firmware that emits one anonymous image part.
        vehicle_bbox = vehicle_image_regions[0].bbox
    elif not vehicle_image_regions:
        # Older single-image payloads can expose a rectangle outside
        # pictureInfoList. Retain that shape only when there is no keyed list.
        for rect_name in ("vehicelRect", "vehicleRect", "VehicleRect"):
            rect_path = f".//{ns}{rect_name}" if ns else f".//{rect_name}"
            pixel_rect = parse_bbox(root.find(rect_path), normalized=False)
            if pixel_rect is not None:
                vehicle_bbox = pixel_rect
                break
    elif event_type in ("ANPR", "vehicleMatchResult", "AccessControllerEvent"):
        vehicle_bbox = None



    # ── ANPR XML extraction: try common Hikvision plate paths ──
    plate_number = None
    plate_confidence = None
    pic_num = None
    gate = None
    if event_type in ("ANPR", "vehicleMatchResult"):
        detection_target = "vehicle"
        # Determine gate from camera config
        cam_config = settings.CAMERAS.get(camera_id, {})
        gate = cam_config.get("gate")
        region_id = gate  # entry | exit

        # Try all known Hikvision ANPR XML paths for plate number
        plate_paths = [
            # ANPR event: <ANPR><licensePlate>
            f".//{ns}ANPR/{ns}licensePlate" if ns else ".//ANPR/licensePlate",
            # Direct licensePlate
            f".//{ns}licensePlate" if ns else ".//licensePlate",
            # plateNumber (some firmware versions)
            f".//{ns}plateNumber" if ns else ".//plateNumber",
            # VehicleMatchResult paths
            f".//{ns}VehicleInfo/{ns}plateNumber" if ns else ".//VehicleInfo/plateNumber",
            f".//{ns}VehicleInfo/{ns}plate" if ns else ".//VehicleInfo/plate",
            # AccessControllerEvent inside XML
            f".//{ns}AccessControllerEvent/{ns}cardNo" if ns else ".//AccessControllerEvent/cardNo",
            # Generic plate/cardNo anywhere
            f".//{ns}cardNo" if ns else ".//cardNo",
            f".//{ns}plate" if ns else ".//plate",
        ]
        for path in plate_paths:
            el = root.find(path)
            if el is not None and el.text and el.text.strip():
                plate_number = el.text.strip()
                break

        if plate_number:
            plate_number = _normalize_plate(plate_number)
            logger.debug(f"[ANPR] Normalized plate: {plate_number} ({camera_id})")
        else:
            logger.debug(f"[ANPR] No plate found in XML for {event_type} from {camera_id}")

        # Per-read burst fields: <confidenceLevel> (audit/tie-break) and <picNum>
        # (resets to 1 per physical vehicle — the burst-grouping signal). Both are
        # optional; a missing/non-numeric tag leaves the field None.
        def _find_int(tag: str) -> Optional[int]:
            val = find_text(tag)
            if val is None:
                # ANPR-specific values are normally nested under <ANPR>.
                # Keep the top-level lookup first for older firmware shapes.
                path = f".//{ns}{tag}" if ns else f".//{tag}"
                el = root.find(path)
                val = el.text.strip() if el is not None and el.text else None
            try:
                return int(val) if val is not None and str(val).strip() != "" else None
            except (TypeError, ValueError):
                return None
        plate_confidence = _find_int("confidenceLevel")
        pic_num = _find_int("picNum")

    crossing_direction = find_text("direction") or find_text("crossingDirection")

    # Diagnostic: log parsed linedetection fields so we can verify
    # that region_id and crossing_direction are correctly extracted.
    parsed_event_type = find_text("eventType") or "unknown"
    if parsed_event_type == "linedetection":
        logger.info(
            f"[Parser] {resolved_camera_id} | linedetection | "
            f"region_id={region_id!r} | direction={crossing_direction!r} | "
            f"target={detection_target!r}"
        )

    channel_id_value = find_text("channelID")
    try:
        channel_id = int(channel_id_value or 1)
    except (TypeError, ValueError) as exc:
        raise CameraPayloadRejected(
            "camera XML channelID must be an integer"
        ) from exc

    return ParsedCameraEvent(
        camera_id=resolved_camera_id,
        device_serial=find_text("deviceSerial") or "unknown",
        channel_id=channel_id,
        event_type=parsed_event_type,
        detection_target=detection_target or ("vehicle" if parsed_event_type in ("ANPR", "vehicleMatchResult") else None),
        region_id=region_id,
        channel_name=find_text("channelName"),
        trigger_time=trigger_time,
        raw_xml=xml_str,
        trigger_time_source=trigger_time_source,
        event_state=find_text("eventState"),
        event_description=find_text("eventDescription"),
        plate_number=plate_number,
        gate=gate,
        crossing_direction=crossing_direction,
        plate_confidence=plate_confidence,
        pic_num=pic_num,
        vehicle_bbox=vehicle_bbox,
        vehicle_image_regions=tuple(vehicle_image_regions),
    )


def normalize_plate(raw: str) -> Optional[str]:
    """
    Normalize Saudi plate format from camera output to display format.
    Examples: "9444HUD" → "HUD-9444",  "7800ERD" → "ERD-7800"
    Returns None for unknown/partial/invalid plates.

    Plates are stored exactly as they always have been — this function does NOT
    change the stored format (the frontend handles digit/letter display order).
    The only added rule (bug 5) is that a plausible plate must contain BOTH a
    letter and a digit; pure OCR garbage like "6466466" or "1211" is rejected
    instead of being stored as a fake plate (which is what made saved numbers
    not match their snapshot on the dashboard).
    """
    if not raw:
        return None
    raw = raw.strip().upper()

    # Camera explicitly couldn't read the plate
    if raw in ("N/A", "NONE", "NULL", ""):
        return None

    # If the plate is EXACTLY "UNKNOWN", it means the camera failed.
    # But if it is "UNKNOWN-X", it's a valid test plate.
    if raw == "UNKNOWN":
        return None

    # Bug 5: a real plate has BOTH letters and digits. Reject reads missing
    # either (all-digit "6466466"/"1211", all-letter strings) so OCR garbage
    # isn't stored. str.isdigit() is Unicode-aware, so Arabic-Indic numerals
    # still count as digits. Storage of accepted plates is otherwise unchanged.
    if not (any(c.isalpha() for c in raw) and any(c.isdigit() for c in raw)):
        logger.debug(f"[ANPR] Rejected implausible plate (needs letters+digits): {raw!r}")
        return None

    # Already normalised (e.g. "HUD-9444") — validate full format 3 letters + 4 digits
    if "-" in raw:
        # 1. Strict match for official International format (e.g., ABC-1234)
        m = re.match(r"^([A-Z]{2,3})-(\d{3,4})$", raw)
        if m:
            return raw
        # 2. Keep any other dashed read as-is (e.g. "9444-HUD", "TEST-001")
        if len(raw) >= 3:
            return raw
        return None

    # Saudi format: 1-4 digits then 2-3 letters  e.g. "9444HUD", "7HDU"
    m = re.match(r"^(\d{1,4})([A-Z]{2,3})$", raw)
    if m:
        return f"{m.group(2)}-{m.group(1)}"

    # Letters then digits  e.g. "HUD9444", "HDU7"
    m = re.match(r"^([A-Z]{2,3})(\d{1,4})$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}"

    # If no specific pattern matches, return as is (lenient for testing/non-Saudi plates)
    if len(raw) >= 3:
        return raw

    # Partial or unrecognized — reject
    logger.debug(f"[ANPR] Rejected too short/invalid plate: {raw!r}")
    return None


# Backward-compatible private name for any existing internal imports.
_normalize_plate = normalize_plate


# A normalized plate in its canonical "LLL-DDDD" shape. Only reads that parse
# here can be compared for truncation; anything else (test plates, non-Saudi
# formats, "UNKNOWN-3") falls back to exact equality.
_CANONICAL_PLATE_RE = re.compile(r"^([A-Z]{2,3})-(\d{1,4})$")


def plate_digits_lost(partial: Optional[str], full: Optional[str]) -> bool:
    """True when `partial` is `full` with one or more digits dropped.

    The entry ANPR can emit a truncated read of a plate it read completely a
    moment earlier — "6294KKR" normalises to KKR-6294, then "4KKR" to KKR-4 on a
    blurrier frame. Same letter group with a strictly shorter digit group that is
    a prefix or suffix of the fuller one means digits were lost off one end, not
    that a different car arrived.

    Deliberately strict: equal-length digits are a genuine disagreement (KKR-6294
    vs KKR-6295 are two cars), and a digit group that is only a *middle*
    substring is not a truncation any camera produces.
    """
    if not partial or not full or partial == full:
        return False
    m_partial = _CANONICAL_PLATE_RE.match(partial)
    m_full = _CANONICAL_PLATE_RE.match(full)
    if not m_partial or not m_full:
        return False
    if m_partial.group(1) != m_full.group(1):
        return False
    short, long = m_partial.group(2), m_full.group(2)
    if len(short) >= len(long):
        return False
    return long.startswith(short) or long.endswith(short)


def same_vehicle_plate(a: Optional[str], b: Optional[str]) -> bool:
    """True when two reads name the same car — identical, or one truncated."""
    if not a or not b:
        return False
    return a == b or plate_digits_lost(a, b) or plate_digits_lost(b, a)


def _parse_json_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 2 JSON events (AccessControllerEvent / vehicleMatchResult from ANPR cameras)."""
    try:
        data = json.loads(raw_body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        raise CameraPayloadRejected("malformed JSON camera metadata") from e

    if not isinstance(data, dict):
        raise CameraPayloadRejected("camera JSON root must be an object")

    for field_name in (
        "dateTime",
        "ipAddress",
        "deviceSerial",
        "deviceID",
        "eventType",
    ):
        value = data.get(field_name)
        if value is not None and not isinstance(value, str):
            raise CameraPayloadRejected(
                f"camera JSON {field_name} must be a string"
            )
    try:
        channel_id = int(data.get("channelID", 1))
    except (TypeError, ValueError) as exc:
        raise CameraPayloadRejected(
            "camera JSON channelID must be an integer"
        ) from exc

    dt_str = data.get("dateTime", "")
    trigger_time, trigger_time_source = _parse_camera_time(dt_str)

    acs = data.get("AccessControllerEvent", {})
    vmr = data.get("VehicleMatchResult", {})
    if not isinstance(acs, dict) or not isinstance(vmr, dict):
        raise CameraPayloadRejected("camera JSON event fields must be objects")

    # Prefer the ipAddress declared inside the JSON body over the HTTP source IP.
    # This handles NAT/proxy scenarios where the HTTP client IP differs from the camera IP.
    json_ip = data.get("ipAddress") or camera_ip
    device_serial = data.get("deviceSerial", data.get("deviceID", "unknown"))
    camera_id = (
        settings.CAMERA_SERIAL_MAP.get(device_serial)
        or settings.CAMERA_IP_MAP.get(json_ip)
        or settings.CAMERA_IP_MAP.get(camera_ip)
        or f"UNKNOWN-{camera_ip}"
    )

    # Determine gate direction from camera config in settings
    cam_config = settings.CAMERAS.get(camera_id, {})
    gate = cam_config.get("gate")  # "entry" or "exit"

    # Extract plate — check both event structures:
    #   AccessControllerEvent → cardNo
    #   VehicleMatchResult    → PlateInfo.plate
    plate_info = vmr.get("PlateInfo", {})
    if not isinstance(plate_info, dict):
        raise CameraPayloadRejected("camera JSON PlateInfo must be an object")
    raw_plate = (
        acs.get("plateNumber")
        or acs.get("licensePlateNumber")
        or acs.get("cardNo")
        or plate_info.get("plate")
        or plate_info.get("plateNumber")
    )
    if raw_plate is not None and not isinstance(raw_plate, (str, int)):
        raise CameraPayloadRejected("camera JSON plate must be a string or integer")
    plate_number = _normalize_plate(str(raw_plate)) if raw_plate else None

    if plate_number:
        logger.debug(f"[ANPR] Extracted plate: {raw_plate!r} -> {plate_number!r} ({camera_id})")
    else:
        logger.debug(f"[ANPR] No plate found in JSON event from {camera_id}")

    # Per-read burst fields (best-effort — JSON firmware may omit them).
    def _json_int(*candidates) -> Optional[int]:
        for c in candidates:
            if c is None or str(c).strip() == "":
                continue
            try:
                return int(c)
            except (TypeError, ValueError):
                continue
        return None
    plate_confidence = _json_int(
        acs.get("confidenceLevel"), plate_info.get("confidenceLevel"),
    )
    pic_num = _json_int(acs.get("picNum"), plate_info.get("picNum"))

    return ParsedCameraEvent(
        camera_id=camera_id,
        device_serial=data.get("deviceSerial", data.get("deviceID", "unknown")),
        channel_id=channel_id,
        event_type=data.get("eventType", "unknown"),
        detection_target="vehicle",
        region_id=gate,
        channel_name=acs.get("deviceName") or data.get("channelName"),
        trigger_time=trigger_time,
        raw_xml=raw_body.decode("utf-8", errors="replace"),
        trigger_time_source=trigger_time_source,
        plate_number=plate_number,
        user_type=acs.get("userType") or vmr.get("listType"),
        person_name=acs.get("name"),
        employee_id=acs.get("employeeNoString"),
        gate=gate,
        plate_confidence=plate_confidence,
        pic_num=pic_num,
    )
