"""HTTP transport for HikCentral, over the Artemis OpenAPI (AK/SK signed).

Two endpoints, both verified live against HCP Professional V3.0.0 at 10.1.20.51:

  POST /artemis/api/pms/v1/crossRecords/page   — ANPR vehicle passing records
  POST /artemis/api/pms/v1/image               — the vehicle imagery

Auth is AppKey/AppSecret HmacSHA256 request signing (OpenAPI guide §3.2) — no
login, no session cookie, no session to keep alive. Every request is
self-contained and stateless, so it survives platform logouts and password
changes (the earlier web-login path did not).

Contract with the rest of the app is unchanged: **this module never raises into
the event path**. A failure returns an empty list / None and logs. HikCentral
being down must never take the camera webhook down with it.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import datetime
from typing import Any, Optional

import httpx

from app.config import settings
from app.services.hikcentral.models import VehicleLogRecord
from app.utils.logger import get_logger

logger = get_logger(__name__)

CROSS_RECORDS_PATH = "/artemis/api/pms/v1/crossRecords/page"
IMAGE_PATH = "/artemis/api/pms/v1/image"

# The OpenAPI reports success/failure in a top-level `code` string ("0" == ok),
# not an HTTP status. HTTP 200 with code "69" means "UnAuthorized API", so the
# body must be inspected before its data is trusted.
_CODE_OK = "0"

# Shared with snapshot_service / event_parser: one directory for every image
# this service persists, served by the /snapshots mount.
SNAPSHOT_DIR = "detection_images"

# Statuses that mean "ask again later". 401/403 are configuration faults, not
# blips — retrying them just burns the request budget (same rule as
# snapshot_service). 502 is included because the gateway returns it while the
# artemis service is restarting.
_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_TRANSPORT_MAX_ATTEMPTS = 2
_TRANSPORT_BACKOFF_S = 0.4

_http_client: Optional[httpx.AsyncClient] = None
# Rate-limits the "HikCentral unreachable" warning so a platform outage cannot
# flood the log at camera-event frequency.
_last_transport_warning_at: float = 0.0
_TRANSPORT_WARNING_INTERVAL_S = 30.0

os.makedirs(SNAPSHOT_DIR, exist_ok=True)


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.HIK_CONNECT_TIMEOUT_SECONDS,
        read=settings.HIK_READ_TIMEOUT_SECONDS,
        write=settings.HIK_READ_TIMEOUT_SECONDS,
        pool=settings.HIK_CONNECT_TIMEOUT_SECONDS,
    )


def _new_client() -> httpx.AsyncClient:
    """Build the stateless HTTP client. No cookies, no auth= — every request is
    signed individually in `_sign`."""
    return httpx.AsyncClient(
        base_url=settings.HIK_BASE_URL,
        timeout=_timeout(),
        verify=settings.HIK_VERIFY_TLS,
    )


def _sign(path: str, body: bytes) -> dict:
    """Build the AK/SK signature headers for one request (OpenAPI guide §3.2).

    stringToSign = METHOD \\n Accept \\n Content-MD5 \\n Content-Type \\n
                   <signed headers "k:v\\n" sorted> URI
    signature = base64(HMAC-SHA256(stringToSign, appSecret)).
    A fresh timestamp and nonce are generated per call for anti-replay.
    """
    accept = content_type = "application/json"
    content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    signed = {
        "x-ca-key": settings.HIK_APP_KEY,
        "x-ca-nonce": str(uuid.uuid4()),
        "x-ca-timestamp": str(int(datetime.now().timestamp() * 1000)),
    }
    signed_lines = "".join(f"{k}:{signed[k]}\n" for k in sorted(signed))
    string_to_sign = (
        f"POST\n{accept}\n{content_md5}\n{content_type}\n{signed_lines}{path}"
    )
    signature = base64.b64encode(
        hmac.new(
            settings.HIK_APP_SECRET.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode()
    return {
        "Accept": accept,
        "Content-Type": content_type,
        "Content-MD5": content_md5,
        "X-Ca-Key": signed["x-ca-key"],
        "X-Ca-Nonce": signed["x-ca-nonce"],
        "X-Ca-Timestamp": signed["x-ca-timestamp"],
        "X-Ca-Signature-Headers": ",".join(sorted(signed)),
        "X-Ca-Signature": signature,
    }


async def start_hikcentral_http_client() -> None:
    """Create the one app-lifetime HikCentral client. No login to perform."""
    global _http_client
    # Config could not use the logger (app.utils.logger imports app.config), so
    # a forced fallback to "off" is surfaced here, where operators will see it.
    disabled_reason = settings.hik_disabled_reason()
    if disabled_reason:
        logger.error("[Hik] %s", disabled_reason)
    if _http_client is None and settings.HIK_VALIDATION_MODE != "off":
        _http_client = _new_client()
        logger.info(
            "[Hik] client started base_url=%s mode=%s appKey=%s",
            settings.HIK_BASE_URL,
            settings.HIK_VALIDATION_MODE,
            settings.HIK_APP_KEY,
        )


async def close_hikcentral_http_client() -> None:
    """Close and detach the shared client."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None


def _log_transport_failure(what: str, exc: Exception) -> None:
    """Warn about an unreachable platform, at most twice a minute."""
    global _last_transport_warning_at
    now = asyncio.get_event_loop().time()
    if now - _last_transport_warning_at >= _TRANSPORT_WARNING_INTERVAL_S:
        _last_transport_warning_at = now
        logger.warning("[Hik] %s failed: %r", what, exc)
    else:
        logger.debug("[Hik] %s failed: %r", what, exc)


async def _signed_post(path: str, body_obj: Any) -> Optional[httpx.Response]:
    """One signed POST, with a bounded transport-level retry.

    The retry covers a dropped connection or a 5xx — it is NOT a second logical
    lookup, so it does not violate the "exactly one lookup per candidate" rule.
    The signature (timestamp + nonce) is regenerated per attempt.
    """
    body = json.dumps(body_obj).encode("utf-8")
    for attempt in range(1, _TRANSPORT_MAX_ATTEMPTS + 1):
        headers = _sign(path, body)
        client = _http_client
        try:
            if client is not None:
                response = await client.post(path, headers=headers, content=body)
            else:
                # Scoped fallback for scripts and tests that never ran startup.
                async with _new_client() as scoped:
                    response = await scoped.post(path, headers=headers, content=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= _TRANSPORT_MAX_ATTEMPTS:
                _log_transport_failure(f"POST {path}", exc)
                return None
            await asyncio.sleep(_TRANSPORT_BACKOFF_S)
            continue

        if (
            response.status_code in _RETRYABLE_STATUSES
            and attempt < _TRANSPORT_MAX_ATTEMPTS
        ):
            await asyncio.sleep(_TRANSPORT_BACKOFF_S)
            continue
        return response
    return None


def response_code(payload: Any) -> Optional[str]:
    """Return the OpenAPI top-level `code`, or None when absent.

    crossRecords success and failure are both HTTP 200; only `code == "0"` is
    success. `code "69"` = UnAuthorized API, others map to the guide's table.
    """
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    return str(code) if code is not None else None


async def query_vehicle_logs(
    begin: datetime,
    end: datetime,
    resource_ids: str,
    page_size: int,
) -> list[VehicleLogRecord]:
    """Fetch ANPR passing records in one narrow window. Never raises.

    `begin`/`end` must be tz-aware so the platform is asked in an unambiguous
    timezone; the OpenAPI honours the offset exactly (unlike the old web
    `VehicleLogs`, there is no `+6h` shift to compensate for). `resource_ids` is
    the camera's OpenAPI indexCode (e.g. "447" for ANPR-1 Entry) — a single code
    per lookup; if a comma list is passed, the first code is used. Returns [] on
    any failure, which callers treat as "no match".
    """
    camera_index_code = (resource_ids or "").split(",")[0].strip()
    if not camera_index_code:
        return []

    body = {
        "cameraIndexCode": camera_index_code,
        "startTime": begin.isoformat(),
        "endTime": end.isoformat(),
        "pageNo": 1,
        "pageSize": page_size,
        "sortField": "PassTime",
        "orderType": 1,  # newest first
    }

    response = await _signed_post(CROSS_RECORDS_PATH, body)
    if response is None:
        return []
    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            "[Hik] crossRecords returned non-JSON: %s", response.text[:200]
        )
        return []

    # HTTP 200 is not success — check the `code` before trusting `data`.
    code = response_code(payload)
    if code != _CODE_OK:
        logger.warning(
            "[Hik] crossRecords refused with code=%s msg=%s (camera=%s). Check "
            "the partner's authorized APIs and cameraIndexCode.",
            code,
            payload.get("msg") if isinstance(payload, dict) else None,
            camera_index_code,
        )
        return []

    rows = ((payload.get("data") or {}).get("list")) or []
    records = []
    for raw in rows:
        record = VehicleLogRecord.from_openapi_record(raw)
        if record is None:
            continue
        # The API already filters by start/end, but enforce the exact window
        # locally too so recovery's "exactly one candidate" rule is precise.
        if begin <= record.pass_time <= end:
            records.append(record)
    return records


async def download_picture(url: str) -> Optional[bytes]:
    """Download one HikCentral image by its `Vsm://` picUri. Never raises.

    `pms/v1/image` returns the picture as a `data:image/...;base64,<...>` URI
    string (not raw bytes), so it is decoded here before being handed back.
    """
    if not url:
        return None

    response = await _signed_post(IMAGE_PATH, {"picUri": url})
    if response is None:
        return None
    if response.status_code >= 400:
        logger.warning("[Hik] image HTTP %s for %s", response.status_code, url)
        return None

    body = response.content
    if not body:
        logger.warning("[Hik] image empty for %s", url)
        return None

    content = _decode_image_body(body, response.headers.get("content-type", ""))
    if content is None:
        # A JSON error envelope (e.g. UnAuthorized) rather than an image.
        logger.warning("[Hik] image not returned for %s: %s", url, body[:200])
        return None

    if len(content) > settings.HIK_IMAGE_MAX_BYTES:
        logger.warning(
            "[Hik] image too large (%d bytes > HIK_IMAGE_MAX_BYTES=%d) for %s",
            len(content),
            settings.HIK_IMAGE_MAX_BYTES,
            url,
        )
        return None
    return content


def _decode_image_body(body: bytes, content_type: str) -> Optional[bytes]:
    """Turn an `pms/v1/image` response body into raw image bytes, or None.

    The endpoint returns a `data:<mime>;base64,<data>` URI. Raw image bytes and
    a plain base64 payload are also accepted defensively.
    """
    if body[:3] == b"\xff\xd8\xff" or body[:8] == b"\x89PNG\r\n\x1a\n":
        return body  # already raw image bytes
    try:
        text = body.decode("latin1")
    except ValueError:
        return None
    if text.startswith("data:"):
        _, _, b64 = text.partition(",")
        try:
            return base64.b64decode(b64)
        except (ValueError, base64.binascii.Error):
            return None
    if "image" in content_type.lower():
        try:
            return base64.b64decode(text)
        except (ValueError, base64.binascii.Error):
            return None
    return None


def save_picture(content: bytes, guid: str, kind: str) -> Optional[str]:
    """Persist image bytes to detection_images/ and return the local path."""
    if not content:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_guid = "".join(c for c in guid if c.isalnum())[:40] or "unknown"
    filename = f"hik_{kind}_{safe_guid}_{stamp}.jpg"
    path = os.path.join(SNAPSHOT_DIR, filename)
    try:
        with open(path, "wb") as handle:
            handle.write(content)
    except OSError as exc:
        logger.warning("[Hik] could not write %s: %r", path, exc)
        return None
    return path
