"""HTTP transport for HikCentral.

Two endpoints, both discovered and verified against the production platform:

  POST /ISAPI/Bumblebee/VehicleBiz/V0/LPR/VehicleLogs      — vehicle passes
  GET  /ISAPI/Bumblebee/Platform/V0/Storage/Picture?URL=…  — the imagery

Contract with the rest of the app: **this module never raises into the event
path**. A failure returns an empty list / None and logs. HikCentral being down
must never take the camera webhook down with it.
"""

import asyncio
import base64
import os
from datetime import datetime, timedelta
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.asymmetric import padding as _rsa_padding
from cryptography.hazmat.primitives.asymmetric import rsa as _rsa

from app.config import settings
from app.services.hikcentral.models import VehicleLogRecord
from app.utils.logger import get_logger

logger = get_logger(__name__)

VEHICLE_LOGS_PATH = "/ISAPI/Bumblebee/VehicleBiz/V0/LPR/VehicleLogs"
PICTURE_PATH = "/ISAPI/Bumblebee/Platform/V0/Storage/Picture"
# The login handshake, captured from the real web client against HCP
# Professional V3.0.0 (see authenticate() for the full sequence):
#   PreLogin  initialises server login state (fires on page load)
#   Crypto    returns the RSA public key used to encrypt the password
#   Login     posts {"LoginRequest": {...}} and returns Data.Login.SID
PRELOGIN_PATH = "/ISAPI/Bumblebee/Platform/V0/PreLogin"
CRYPTO_PATH = "/ISAPI/Bumblebee/Platform/V0/Security/Crypto"
LOGIN_PATH = "/ISAPI/Bumblebee/Platform/V0/Login"
# Keeps a session from idling out; confirmed to return ErrorCode 0 on the cookie
# alone. A background task pings it so the SID survives without re-login.
KEEPLIVE_PATH = "/ISAPI/Bumblebee/Platform/V0/KeepLive"
# Session idle timeout is ~30 min; ping well inside it.
_KEEPLIVE_INTERVAL_S = 300.0

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
# Background KeepLive pinger; runs only in login (non-seeded-cookie) mode.
_keepalive_task: Optional["asyncio.Task"] = None
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


def _parse_der_len(data: bytes, i: int) -> tuple[int, int]:
    """Return (length, next_index) for the DER length field at data[i]."""
    b = data[i]
    i += 1
    if b < 0x80:
        return b, i
    n = b & 0x7F
    return int.from_bytes(data[i:i + n], "big"), i + n


def _load_pkcs1_public_key(der: bytes) -> _rsa.RSAPublicKey:
    """Parse HikCentral's CryptoKey: a PKCS#1 RSAPublicKey DER (SEQ{ n, e }).

    JSEncrypt (what the web client uses) emits this raw form, not a
    SubjectPublicKeyInfo, so `load_der_public_key` cannot read it directly.
    """
    assert der[0] == 0x30, "expected SEQUENCE"
    _, i = _parse_der_len(der, 1)
    assert der[i] == 0x02, "expected INTEGER n"
    i += 1
    nlen, i = _parse_der_len(der, i)
    n = int.from_bytes(der[i:i + nlen], "big")
    i += nlen
    assert der[i] == 0x02, "expected INTEGER e"
    i += 1
    elen, i = _parse_der_len(der, i)
    e = int.from_bytes(der[i:i + elen], "big")
    return _rsa.RSAPublicNumbers(e, n).public_key()


def _encrypt_password(public_key_b64: str, password: str) -> str:
    """RSA-encrypt the password exactly as the web client's JSEncrypt does:
    PKCS#1 v1.5 padding, base64 of the ciphertext."""
    public_key = _load_pkcs1_public_key(base64.b64decode(public_key_b64))
    cipher = public_key.encrypt(password.encode(), _rsa_padding.PKCS1v15())
    return base64.b64encode(cipher).decode()


async def authenticate(client: Optional[httpx.AsyncClient] = None) -> bool:
    """Log in and leave a valid session cookie on the client. Never raises.

    Reproduces the exact sequence captured from the real web client against HCP
    Professional V3.0.0:

        PreLogin  ->  Crypto (RSA public key)  ->  Login (RSA-encrypted password)

    The session is carried by the ``SID`` cookie the platform sets; the crypto
    handshake is bound by the transient ``CRYPTO`` cookie, so the httpx cookie
    jar is all the state that is needed. A misconfigured credential fails once
    and returns False — it is NEVER retried in a loop, so a wrong password can
    never lock the shared account (mirrors the "no random login payloads" rule).
    """
    active = client or _http_client
    if active is None:
        return False

    # An operator-seeded cookie wins: it lets a browser session drive the layer
    # without storing the password, and is how the flow was first proven.
    if settings.HIK_SESSION_COOKIE:
        return True

    if not settings.HIK_USERNAME or not settings.HIK_PASSWORD:
        logger.error(
            "[Hik] cannot authenticate: HIK_USERNAME/HIK_PASSWORD are unset and "
            "no HIK_SESSION_COOKIE was provided. Layer stays unauthenticated."
        )
        return False

    try:
        # 1. PreLogin — initialises server login state (empty body).
        await active.post(PRELOGIN_PATH, params=_METHOD_TYPE_GET, json={})

        # 2. Crypto — fetch the RSA public key; sets the CRYPTO cookie.
        crypto_resp = await active.post(CRYPTO_PATH, params=_METHOD_TYPE_GET, json={})
        crypto = crypto_resp.json()
        if response_error_code(crypto) != _ERROR_CODE_OK:
            logger.error("[Hik] Crypto handshake failed: %s", crypto.get("ResponseStatus"))
            return False
        public_key_b64 = crypto["ResponseStatus"]["Data"]["CryptoResponse"]["CryptoKey"]

        # 3. Login — RSA-encrypt the password and post the LoginRequest.
        body = {
            "LoginRequest": {
                "UserName": settings.HIK_USERNAME,
                "Password": _encrypt_password(public_key_b64, settings.HIK_PASSWORD),
                "LoginAddress": urlparse(settings.HIK_BASE_URL).hostname or "",
                "LoginModel": 1,
                "IsRSMWebLogin": 0,
            }
        }
        login_resp = await active.post(LOGIN_PATH, params={"MT": "POST"}, json=body)
        login = login_resp.json()
        error_code = response_error_code(login)
        if error_code != _ERROR_CODE_OK:
            # Do NOT retry — a credential/decrypt rejection here must fail closed,
            # not hammer the account. Operator fixes HIK_PASSWORD and restarts.
            logger.error(
                "[Hik] Login refused with ErrorCode=%s. Check HIK_USERNAME/"
                "HIK_PASSWORD. Not retrying (would risk locking the account).",
                error_code,
            )
            return False

        # The platform sets the SID cookie itself; set it explicitly too so the
        # session is carried even if a proxy strips Set-Cookie.
        sid = login["ResponseStatus"]["Data"]["Login"]["SID"]
        parsed = urlparse(settings.HIK_BASE_URL)
        active.cookies.set(
            settings.HIK_SESSION_COOKIE_NAME, sid, domain=parsed.hostname, path="/"
        )
        logger.info("[Hik] authenticated as %s", settings.HIK_USERNAME)
        return True
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        logger.error("[Hik] authentication error: %r", exc)
        return False


async def keep_session_alive() -> None:
    """Ping KeepLive on an interval so the SID never idles out. Never raises."""
    while True:
        await asyncio.sleep(_KEEPLIVE_INTERVAL_S)
        if _http_client is None or settings.HIK_SESSION_COOKIE:
            # Seeded-cookie mode owns its own lifetime; nothing to keep alive.
            continue
        try:
            resp = await _http_client.post(
                KEEPLIVE_PATH, params=_METHOD_TYPE_GET, json={}
            )
            if response_error_code(resp.json()) != _ERROR_CODE_OK:
                # Session dropped (logout elsewhere, reboot) — re-authenticate.
                logger.warning("[Hik] KeepLive rejected; re-authenticating")
                await authenticate(_http_client)
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("[Hik] KeepLive ping failed: %r", exc)


async def start_hikcentral_http_client() -> None:
    """Create the one app-lifetime HikCentral client."""
    global _http_client
    # Config could not use the logger (app.utils.logger imports app.config), so
    # a forced fallback to "off" is surfaced here, where operators will see it.
    disabled_reason = settings.hik_disabled_reason()
    if disabled_reason:
        logger.error("[Hik] %s", disabled_reason)
    global _keepalive_task
    if _http_client is None and settings.HIK_VALIDATION_MODE != "off":
        _http_client = _new_client()
        await authenticate(_http_client)
        # Keep the login session warm. Not started for seeded-cookie mode, whose
        # lifetime the operator owns.
        if not settings.HIK_SESSION_COOKIE:
            _keepalive_task = asyncio.create_task(keep_session_alive())
        logger.info(
            "[Hik] client started base_url=%s mode=%s",
            settings.HIK_BASE_URL,
            settings.HIK_VALIDATION_MODE,
        )


async def close_hikcentral_http_client() -> None:
    """Close and detach the shared client."""
    global _http_client, _keepalive_task
    if _keepalive_task is not None:
        _keepalive_task.cancel()
        _keepalive_task = None
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
