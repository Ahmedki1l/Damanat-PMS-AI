"""HTTP transport for HikCentral.

Two endpoints, both discovered and verified against the production platform:

  POST /ISAPI/Bumblebee/VehicleBiz/V0/LPR/VehicleLogs      — vehicle passes
  GET  /ISAPI/Bumblebee/Platform/V0/Storage/Picture?URL=…  — the imagery

Contract with the rest of the app: **this module never raises into the event
path**. A failure returns an empty list / None and logs. HikCentral being down
must never take the camera webhook down with it.
"""

import asyncio
import os
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.services.hikcentral.models import VehicleLogRecord
from app.utils.logger import get_logger

logger = get_logger(__name__)

VEHICLE_LOGS_PATH = "/ISAPI/Bumblebee/VehicleBiz/V0/LPR/VehicleLogs"
PICTURE_PATH = "/ISAPI/Bumblebee/Platform/V0/Storage/Picture"
# Confirmed to exist on the live platform (HTTPS only). The request/response
# shape has not been captured yet.
LOGIN_PATH = "/ISAPI/Bumblebee/Platform/V0/Login"

# HikCentral answers EVERYTHING with HTTP 200 and puts the real outcome in
# `ResponseStatus.ErrorCode`. Verified against 10.1.20.51 on 2026-07-27:
# an unauthenticated VehicleLogs call returns 200 + ErrorCode 1016, not a 401.
# Treating the status line as the result would make every failure look like
# "no vehicles found", so success is decided by this field instead.
_ERROR_CODE_OK = 0

# Shared with snapshot_service / event_parser: one directory for every image
# this service persists, served by the /snapshots mount.
SNAPSHOT_DIR = "detection_images"

# HikCentral wants a local ISO-8601 string with offset, matching PassTime.
# NOTE: the declared offset is *ignored* by the platform — verified live by
# sending the same instant as +03:00, +00:00 and with no suffix at all, which
# produced identical results. Only the wall-clock digits matter.
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

# Every Bumblebee call is a POST carrying its real verb in the `MT` query
# parameter. Verified live: without `?MT=GET` the VehicleLogs call returns
# ErrorCode 217 and no data; with it, ErrorCode 0 and records.
_METHOD_TYPE_GET = {"MT": "GET"}

# Statuses that mean "ask again later". 401/403 are configuration faults, not
# blips — retrying them just burns the request budget (same rule as
# snapshot_service).
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
    """Build a client that carries the platform session cookie.

    Deliberately NO `auth=`. HTTP Digest was the original assumption and it is
    wrong: against the live platform, Digest, Basic and no-auth all return the
    same `ErrorCode 1016`, so credentials were never being consulted. HikCentral
    is session-based, so the cookie jar on this client is what carries identity.
    """
    client = httpx.AsyncClient(
        base_url=settings.HIK_BASE_URL,
        timeout=_timeout(),
        verify=settings.HIK_VERIFY_TLS,
    )
    if settings.HIK_SESSION_COOKIE:
        parsed = urlparse(settings.HIK_BASE_URL)
        client.cookies.set(
            settings.HIK_SESSION_COOKIE_NAME,
            settings.HIK_SESSION_COOKIE,
            domain=parsed.hostname,
            path="/",
        )
    return client


async def authenticate(client: Optional[httpx.AsyncClient] = None) -> bool:
    """Prepare the HikCentral session, without guessing the login payload.

    Production probing succeeded with an already-authenticated browser cookie.
    The actual login request has not been captured, so this method deliberately
    does not POST to `LOGIN_PATH` or synthesize credentials. When the real flow
    is recorded, implement it here and keep the rest of the client cookie-based.
    For now, operators may seed the client with HIK_SESSION_COOKIE.
    """
    active = client or _http_client
    if active is None:
        return False
    if settings.HIK_SESSION_COOKIE:
        return True
    logger.warning(
        "[Hik] authentication flow is not implemented; set HIK_SESSION_COOKIE "
        "from a captured browser session until the real login request is known"
    )
    return False


async def start_hikcentral_http_client() -> None:
    """Create the one app-lifetime HikCentral client."""
    global _http_client
    # Config could not use the logger (app.utils.logger imports app.config), so
    # a forced fallback to "off" is surfaced here, where operators will see it.
    disabled_reason = settings.hik_disabled_reason()
    if disabled_reason:
        logger.error("[Hik] %s", disabled_reason)
    if _http_client is None and settings.HIK_VALIDATION_MODE != "off":
        _http_client = _new_client()
        await authenticate(_http_client)
        logger.info(
            "[Hik] client started base_url=%s mode=%s",
            settings.HIK_BASE_URL,
            settings.HIK_VALIDATION_MODE,
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


async def _request(method: str, url: str, **kwargs) -> Optional[httpx.Response]:
    """One logical HikCentral call, with a bounded transport-level retry.

    The retry covers a dropped connection or a 5xx — it is NOT a second logical
    lookup, so it does not violate the "exactly one lookup per candidate" rule.
    """
    for attempt in range(1, _TRANSPORT_MAX_ATTEMPTS + 1):
        client = _http_client
        try:
            if client is not None:
                response = await client.request(method, url, **kwargs)
            else:
                # Scoped fallback for scripts and tests that never ran startup.
                async with _new_client() as scoped:
                    response = await scoped.request(method, url, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            if attempt >= _TRANSPORT_MAX_ATTEMPTS:
                _log_transport_failure(f"{method} {url}", exc)
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


def response_error_code(payload: Any) -> Optional[int]:
    """Return HikCentral's `ResponseStatus.ErrorCode`, or None when absent.

    VehicleLogs success and failure are both reported inside this envelope.
    HTTP 200 is not success unless ErrorCode is present and equal to 0.
    """
    if not isinstance(payload, dict):
        return None
    status = payload.get("ResponseStatus")
    if not isinstance(status, dict) or "ErrorCode" not in status:
        return None
    try:
        return int(status.get("ErrorCode"))
    except (TypeError, ValueError):
        return None


def _extract_raw_records(payload: Any) -> list[dict]:
    """Pull the vehicle-log rows out of a HikCentral response envelope.

    The envelope key has varied across HikCentral builds, so rather than
    hard-coding one spelling this walks the decoded JSON for the first list of
    objects carrying a GUID. Structural, not name-based — it keeps working if
    the wrapper is renamed.
    """
    found: list[dict] = []

    def walk(node: Any) -> bool:
        if isinstance(node, list):
            rows = [
                item
                for item in node
                if isinstance(item, dict) and "GUID" in item
            ]
            if rows:
                found.extend(rows)
                return True
            return any(walk(item) for item in node)
        if isinstance(node, dict):
            # A bare single record, not wrapped in a list.
            if "GUID" in node and "PassTime" in node:
                found.append(node)
                return True
            return any(walk(value) for value in node.values())
        return False

    walk(payload)
    return found


async def query_vehicle_logs(
    begin: datetime,
    end: datetime,
    resource_ids: str,
    page_size: int,
) -> list[VehicleLogRecord]:
    """Fetch vehicle passes in one narrow window. Never raises.

    `begin`/`end` must be tz-aware so HikCentral is asked in an unambiguous
    timezone. Returns [] on any failure, which callers treat as "no match".
    """
    # Two platform quirks, both measured against 10.1.20.51 on 2026-07-27.
    # Neither is documented, and each one alone silently breaks this layer.
    #
    # 1. BeginTime is shifted. The server applies the facility's UTC offset
    #    twice, so a time sent as 09:00 filters from 15:00 (+6h at UTC+3).
    #    Sending the honest window returned ZERO rows for a car that was
    #    provably in the log. We pre-subtract the shift to compensate.
    # 2. EndTime is ignored outright — `end 13:00` still returned passes from
    #    17:58 — so the upper bound has to be enforced here, client-side.
    #
    # Both are hidden at this boundary: callers pass a real window and get back
    # only records inside it.
    shift = timedelta(hours=settings.HIK_QUERY_TIME_SHIFT_HOURS)
    body = {
        "VehicleLogsRequest": {
            "PageIndex": 1,
            "PageSize": page_size,
            "SearchCriteria": {
                "ResourceType": 0,
                "RequestTimeType": 0,
                "BeginTime": (begin - shift).strftime(_TIME_FORMAT),
                "EndTime": (end - shift).strftime(_TIME_FORMAT),
                # Must be a comma-joined STRING. A JSON array is accepted but
                # silently NOT applied, returning every camera's traffic — which
                # would look like a successful query and match the wrong car.
                "ResourceIDs": resource_ids,
            },
            "RequestSortType": {"SortType": 1},
        }
    }

    response = await _request(
        "POST", VEHICLE_LOGS_PATH, params=_METHOD_TYPE_GET, json=body
    )
    if response is None:
        return []
    try:
        payload = response.json()
    except ValueError:
        logger.warning(
            "[Hik] VehicleLogs returned non-JSON: %s", response.text[:200]
        )
        return []

    # HTTP 200 is not success here — check the envelope before trusting it.
    # Without this, an expired session or a bad resource ID is indistinguishable
    # from "this car was never seen", and the layer silently validates nothing.
    error_code = response_error_code(payload)
    if error_code != _ERROR_CODE_OK:
        logger.warning(
            "[Hik] VehicleLogs refused with ErrorCode=%s (HTTP %s). The "
            "session is likely missing or expired, or ResourceIDs=%s is wrong.",
            error_code,
            response.status_code,
            resource_ids,
        )
        return []

    records = []
    for raw in _extract_raw_records(payload):
        record = VehicleLogRecord.from_payload(raw)
        if record is None:
            continue
        # Enforce the window the caller actually asked for (see quirk 2 above).
        # Without this a "±30 second" lookup would really mean "everything since
        # BeginTime", and recovery's one-candidate rule would be meaningless.
        if begin <= record.pass_time <= end:
            records.append(record)
    return records


async def download_picture(url: str) -> Optional[bytes]:
    """Download one HikCentral image (a `Vsm://` handle). Never raises."""
    if not url:
        return None

    response = await _request("GET", PICTURE_PATH, params={"URL": url})
    if response is None:
        return None
    if response.status_code >= 400:
        logger.warning("[Hik] Picture HTTP %s for %s", response.status_code, url)
        return None

    content = response.content
    if not content:
        logger.warning("[Hik] Picture empty for %s", url)
        return None
    if len(content) > settings.HIK_IMAGE_MAX_BYTES:
        logger.warning(
            "[Hik] Picture too large (%d bytes > HIK_IMAGE_MAX_BYTES=%d) for %s",
            len(content),
            settings.HIK_IMAGE_MAX_BYTES,
            url,
        )
        return None

    content_type = (response.headers.get("content-type") or "").lower()
    if content_type and not content_type.startswith("image/"):
        logger.warning(
            "[Hik] Picture content-type %r is not an image for %s",
            content_type,
            url,
        )
        return None
    return content


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
