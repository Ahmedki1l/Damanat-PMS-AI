# app/utils/spaces_client.py
"""
DigitalOcean Spaces client for uploading detection snapshots.
Uses boto3 (S3-compatible API). Returns a public CDN URL on success.
Only active when STORAGE_MODE=spaces in settings.
"""

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3",
            region_name=settings.DO_SPACES_REGION,
            endpoint_url=f"https://{settings.DO_SPACES_ENDPOINT}",
            aws_access_key_id=settings.DO_SPACES_KEY,
            aws_secret_access_key=settings.DO_SPACES_SECRET,
        )
    return _client


def upload_image(image_bytes: bytes, filename: str, folder: str = "detection_images") -> str | None:
    """
    Upload raw image bytes to DO Spaces.
    Returns the public CDN URL on success, or None on failure.

    Args:
        image_bytes: Raw JPEG bytes from the camera.
        filename:    e.g. "snap_linedetection_CAM-04_20260311_033025.jpg"
        folder:      Subfolder in the bucket (default: "detection_images")
    """
    if not settings.DO_SPACES_KEY or not settings.DO_SPACES_BUCKET:
        logger.warning("[Spaces] Upload skipped — DO Spaces not configured")
        return None

    object_key = f"{folder}/{filename}"

    try:
        client = _get_client()
        client.put_object(
            Bucket=settings.DO_SPACES_BUCKET,
            Key=object_key,
            Body=image_bytes,
            ContentType="image/jpeg",
            ACL="public-read",
        )
        cdn_base = settings.DO_SPACES_CDN_URL.rstrip("/")
        url = f"{cdn_base}/{object_key}"
        logger.info(f"[Spaces] Uploaded {filename} → {url}")
        return url
    except (BotoCoreError, ClientError) as e:
        logger.error(f"[Spaces] Upload failed for {filename}: {e}")
        return None
