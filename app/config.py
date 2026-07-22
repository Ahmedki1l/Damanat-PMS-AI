# app/config.py
"""
Application configuration using Pydantic-Settings.
All settings can be overridden via environment variables or .env file.
"""

from ipaddress import ip_network
from typing import Any, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings


# Canonical gate assignments (entry/exit) for the internal floor-to-floor and
# main entrance/exit cameras. Single source of truth shared by the .env
# validator (_build_camera_dicts) and the DB loader (camera_loader.py) so both
# code paths derive gates identically.
GATE_RULES: dict[str, str] = {
    "CAM-03": "entry",   # Main entrance internal
    "CAM-08": "exit",    # Main exit internal
    "CAM-09": "entry",   # To B2
    "CAM-10": "exit",    # From B2
    "CAM-ENTRY": "entry",
    "CAM-EXIT": "exit",
}


def parse_camera_source_networks(value: str) -> tuple[Any, ...]:
    """Parse exact peer IPs or CIDRs; invalid entries fail configuration."""
    return tuple(
        ip_network(item.strip(), strict=False)
        for item in value.split(",")
        if item.strip()
    )


def apply_gate_rules(cameras: dict) -> dict:
    """Stamp the canonical entry/exit gate onto each known camera in-place."""
    for cam_id, gate in GATE_RULES.items():
        if cam_id in cameras:
            cameras[cam_id]["gate"] = gate
    return cameras


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = "mssql://damanat:damanat@pms-mssql:1433"
    DB_NAME: str = "damanat_pms"
    DB_DRIVER: str = "ODBC Driver 18 for SQL Server"
    # Max time a statement may wait on a lock before erroring out (SQL Server
    # 1222). Bounded so a lock wait can never freeze the event loop; see the
    # connect hook in database.py.
    DB_LOCK_TIMEOUT_MS: int = 15000

    @property
    def db_url(self) -> str:
        """Return a SQLAlchemy-ready database URL.

        Supports:
        - postgres://... -> postgresql://...
        - mssql://user:pass@host:port with DB_NAME provided separately
        - mssql+pyodbc://... with missing driver/query defaults
        """
        raw_url = self.DATABASE_URL.strip()

        if raw_url.startswith("postgres://"):
            return raw_url.replace("postgres://", "postgresql://", 1)

        if raw_url.startswith(("mssql://", "mssql+pyodbc://")):
            split = urlsplit(raw_url)
            path = split.path or f"/{self.DB_NAME}"
            if path in {"", "/"}:
                path = f"/{self.DB_NAME}"

            query = dict(parse_qsl(split.query, keep_blank_values=True))
            query.setdefault("driver", self.DB_DRIVER)
            query.setdefault("Encrypt", "no")

            scheme = "mssql+pyodbc"
            return urlunsplit((scheme, split.netloc, path, urlencode(query), split.fragment))

        return raw_url

    # ── Network ───────────────────────────────────────────────────────────
    BACKEND_IP: str = "127.0.0.1"  # override via .env per deployment
    BACKEND_PORT: int = 8080

    # Externally-reachable origin used to build full snapshot URLs.
    # Snapshots are written into detection_images/ and served by the
    # /snapshots StaticFiles mount; consumers store the full URL form
    # (e.g. http://pms-ai:8080/snapshots/foo.jpg) directly in the DB.
    PUBLIC_BASE_URL: str = "http://localhost:8080"

    # ── Security ──────────────────────────────────────────────────────────
    API_KEY: Optional[str] = None   # Set in .env to enable auth on API endpoints
    CAMERA_EVENT_MAX_BODY_BYTES: int = Field(
        default=16 * 1024 * 1024,
        gt=0,
    )
    CAMERA_EVENT_ALLOWED_SOURCE_CIDRS: str = ""

    # ── Node.js Core Backend Integration ─────────────────────────────────
    NODEBACK_URL: str = ""          # e.g. "http://localhost:3000"; empty = disabled
    NODEBACK_SITE_ID: str = ""      # UUID of this parking site in the website backend
    NODEBACK_SERVICE_KEY: str = ""  # X-Service-Key header value for service-to-service auth

    # ── PMS Tracking API Integration ─────────────────────────────────────
    PMS_API_URL: str = ""           # e.g. "http://localhost:8000"; empty = disabled

    # ── Entry validation V2 (PMS-AI ↔ Video Analytics) ─────────────────
    # off:           existing burst/FIFO entry flow only (safe default)
    # shadow:        existing flow remains authoritative; evidence is mirrored
    # authoritative: VA confirmations are the only writer of entry log/session
    ENTRY_V2_MODE: Literal["off", "shadow", "authoritative"] = "off"
    ENTRY_V2_SERVICE_KEY: str = ""
    ENTRY_V2_CAMERA_ALIASES: str = ""
    ENTRY_V2_CONNECT_TIMEOUT_SECONDS: float = Field(
        default=2.0, gt=0, allow_inf_nan=False
    )
    ENTRY_V2_READ_TIMEOUT_SECONDS: float = Field(
        default=30.0, gt=0, allow_inf_nan=False
    )
    ENTRY_V2_WRITE_TIMEOUT_SECONDS: float = Field(
        default=10.0, gt=0, allow_inf_nan=False
    )
    ENTRY_V2_POOL_TIMEOUT_SECONDS: float = Field(
        default=2.0, gt=0, allow_inf_nan=False
    )
    ENTRY_V2_APPLOCK_TIMEOUT_MS: int = Field(default=1000, ge=0, le=4000)
    ENTRY_V2_MAX_IMAGE_BYTES: int = Field(
        default=4 * 1024 * 1024,
        gt=0,
        le=4 * 1024 * 1024,
    )
    ENTRY_V2_MAX_SOURCE_IMAGE_BYTES: int = Field(
        default=16 * 1024 * 1024,
        gt=0,
        le=16 * 1024 * 1024,
    )
    ENTRY_V2_MAX_IMAGES: int = Field(default=4, gt=0, le=4)
    # The first two bounds are the exact VA decoded-image intake envelope.
    # Source images may be larger because PMS crops them before forwarding, but
    # their decode is independently bounded to prevent compressed image bombs.
    ENTRY_V2_MAX_DECODED_PIXELS: int = Field(
        default=12_000_000,
        gt=0,
        le=12_000_000,
    )
    ENTRY_V2_MAX_IMAGE_DIMENSION: int = Field(default=8192, gt=0, le=8192)
    ENTRY_V2_MAX_SOURCE_DECODED_PIXELS: int = Field(
        default=30_000_000,
        gt=0,
        le=30_000_000,
    )
    ENTRY_V2_CROP_PADDING_RATIO: float = Field(
        default=0.12,
        ge=0,
        le=0.5,
        allow_inf_nan=False,
    )
    ENTRY_V2_ONE_WAY_LINES: str = ""

    # ── Camera credential decryption ─────────────────────────────────────
    # Shared urlsafe-base64 Fernet key with the API Gateway. Used to decrypt
    # cameras.password_encrypted when the inventory is loaded from the DB at
    # startup (see app/services/camera_loader.py). Empty = DB passwords can't
    # be decrypted and the loader falls back to the .env CAM_XX_PASSWORD values.
    CAMERAS_ENCRYPTION_KEY: str = ""

    # ── Zone UUID mappings (from Node.js backend database) ────────────────
    # Camera ID → Zone UUID (used for alert zone_id in violation/intrusion/ANPR)
    CAMERA_ZONE_MAP: dict = {
        "CAM-03":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-04":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-05":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-06":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-07":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-08":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-09":    "93651f64-fb84-4082-b51e-9477cf7c06ac",  # B2-PARKING
        "CAM-11":    "93651f64-fb84-4082-b51e-9477cf7c06ac",
        "CAM-12":    "93651f64-fb84-4082-b51e-9477cf7c06ac",
        "CAM-13":    "93651f64-fb84-4082-b51e-9477cf7c06ac",
        "CAM-14":    "93651f64-fb84-4082-b51e-9477cf7c06ac",
        "CAM-ENTRY": "b17a1403-2b37-4a03-8e49-0977a1d16736",  # GF-GATES
        "CAM-EXIT":  "b17a1403-2b37-4a03-8e49-0977a1d16736",  # GF-GATES
    }

    # Zone name → Zone UUID (used for occupancy HTTP push)
    # "GARAGE-TOTAL" is intentionally excluded — Node.js handles total counts internally
    ZONE_NAME_TO_UUID: dict = {
        "B1-PARKING":     "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",
        "B2-PARKING":     "93651f64-fb84-4082-b51e-9477cf7c06ac",
    }
    ZONE_METADATA: dict[str, dict[str, Any]] = {
        "GARAGE-TOTAL": {
            "zone_name": "Garage Total",
            "floor": "ALL",
            "max_capacity": 18,
        },
        "B1-PARKING": {
            "zone_name": "B1 Parking",
            "floor": "B1",
            "max_capacity": 9,
        },
        "B2-PARKING": {
            "zone_name": "B2 Parking",
            "floor": "B2",
            "max_capacity": 9,
        },
        "entry": {
            "zone_name": "Entry Gate",
            "floor": "GF",
            "max_capacity": None,
        },
        "exit": {
            "zone_name": "Exit Gate",
            "floor": "GF",
            "max_capacity": None,
        },
    }
    # ── Phase 1 Camera credentials (read from .env) ──────────────────────
    CAM_01_IP: str = ""
    CAM_01_USER: str = ""
    CAM_01_PASSWORD: str = ""
    CAM_01_NAME: str = "B1-ENTRANCE-INTERNAL"

    CAM_02_IP: str = ""
    CAM_02_USER: str = ""
    CAM_02_PASSWORD: str = ""
    CAM_02_NAME: str = "B1-ENTRANCE-INTERNAL"
    
    CAM_03_IP: str = ""
    CAM_03_USER: str = ""
    CAM_03_PASSWORD: str = ""
    CAM_03_NAME: str = "B1-ENTRANCE-INTERNAL"

    CAM_04_IP: str = ""
    CAM_04_USER: str = ""
    CAM_04_PASSWORD: str = ""
    CAM_04_NAME: str = "B1-PARKING"

    CAM_05_IP: str = ""
    CAM_05_USER: str = ""
    CAM_05_PASSWORD: str = ""
    CAM_05_NAME: str = "B1-PARKING"

    CAM_06_IP: str = ""
    CAM_06_USER: str = ""
    CAM_06_PASSWORD: str = ""
    CAM_06_NAME: str = "B1-PARKING"

    CAM_07_IP: str = ""
    CAM_07_USER: str = ""
    CAM_07_PASSWORD: str = ""
    CAM_07_NAME: str = "B1-PARKING"

    CAM_08_IP: str = ""
    CAM_08_USER: str = ""
    CAM_08_PASSWORD: str = ""
    CAM_08_NAME: str = "B1-EXIT-INTERNAL"

    CAM_09_IP: str = ""
    CAM_09_USER: str = ""
    CAM_09_PASSWORD: str = ""
    CAM_09_NAME: str = "B2-PARKING"

    CAM_10_IP: str = ""
    CAM_10_USER: str = ""
    CAM_10_PASSWORD: str = ""
    CAM_10_NAME: str = "B2-PARKING"

    CAM_11_IP: str = ""
    CAM_11_USER: str = ""
    CAM_11_PASSWORD: str = ""
    CAM_11_NAME: str = "B2-PARKING"

    CAM_12_IP: str = ""
    CAM_12_USER: str = ""
    CAM_12_PASSWORD: str = ""
    CAM_12_NAME: str = "B2-PARKING"

    CAM_13_IP: str = ""
    CAM_13_USER: str = ""
    CAM_13_PASSWORD: str = ""
    CAM_13_NAME: str = "B2-PARKING"

    CAM_14_IP: str = ""
    CAM_14_USER: str = ""
    CAM_14_PASSWORD: str = ""
    CAM_14_NAME: str = "B2-PARKING"


    CAM_35_IP: str = ""
    CAM_35_USER: str = ""
    CAM_35_PASSWORD: str = ""
    CAM_35_NAME: str = "B1-DATA CENTER"

    # ── Phase 2 — ANPR cameras ───────────────────────────────────────────
    CAM_ENTRY_IP: str = ""
    CAM_ENTRY_USER: str = ""
    CAM_ENTRY_PASSWORD: str = ""
    CAM_ENTRY_NAME: str = "ENTRY-GATE"

    CAM_EXIT_IP: str = ""
    CAM_EXIT_USER: str = ""
    CAM_EXIT_PASSWORD: str = ""
    CAM_EXIT_NAME: str = "EXIT-GATE"

    # Serial Number mapping (for environments where IP is masked/proxied)
    CAM_ENTRY_SERIAL: str = ""
    CAM_EXIT_SERIAL: str = ""

    # ── Derived camera dicts (built from env vars above) ──────────────────
    CAMERAS: dict = {}
    CAMERA_IP_MAP: dict = {}
    CAMERA_SERIAL_MAP: dict = {}

    @model_validator(mode="after")
    def _build_camera_dicts(self) -> "Settings":
        """Build CAMERAS and CAMERA_IP_MAP from individual env vars."""
        try:
            source_networks = parse_camera_source_networks(
                self.CAMERA_EVENT_ALLOWED_SOURCE_CIDRS
            )
        except ValueError as exc:
            raise ValueError(
                "CAMERA_EVENT_ALLOWED_SOURCE_CIDRS contains an invalid IP/CIDR"
            ) from exc
        if self.ENTRY_V2_MODE == "authoritative" and not source_networks:
            raise ValueError(
                "CAMERA_EVENT_ALLOWED_SOURCE_CIDRS is required when "
                "ENTRY_V2_MODE=authoritative"
            )
        if self.ENTRY_V2_MODE != "off":
            base_url = self.PMS_API_URL.strip()
            try:
                parsed_url = urlsplit(base_url)
                _ = parsed_url.port
            except ValueError as exc:
                raise ValueError(
                    "PMS_API_URL must be a valid absolute HTTP(S) URL when "
                    "ENTRY_V2_MODE is active"
                ) from exc
            if (
                parsed_url.scheme not in {"http", "https"}
                or not parsed_url.hostname
                or parsed_url.username
                or parsed_url.password
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError(
                    "PMS_API_URL must be a credential-free absolute HTTP(S) "
                    "base URL without a query or fragment when ENTRY_V2_MODE "
                    "is active"
                )
            if not self.ENTRY_V2_SERVICE_KEY.strip():
                raise ValueError(
                    "ENTRY_V2_SERVICE_KEY is required when ENTRY_V2_MODE is active"
                )
            self.PMS_API_URL = base_url.rstrip("/")

        confirmation_cameras = {
            camera.strip()
            for camera in self.ENTRY_CONFIRM_CAMERAS.split(",")
            if camera.strip()
        }
        if (
            self.ENTRY_V2_MODE == "authoritative"
            and "CAM-23" in confirmation_cameras
            and not (
                self.CAM23_ENTRY_LINE.strip()
                or self.CAM23_ENTRY_DIRECTION.strip()
            )
        ):
            raise ValueError(
                "CAM23_ENTRY_LINE or CAM23_ENTRY_DIRECTION is required when "
                "CAM-23 is enabled in authoritative Entry V2"
            )

        if (
            self.ENTRY_V2_MAX_SOURCE_DECODED_PIXELS
            < self.ENTRY_V2_MAX_DECODED_PIXELS
        ):
            raise ValueError(
                "ENTRY_V2_MAX_SOURCE_DECODED_PIXELS must be greater than or "
                "equal to ENTRY_V2_MAX_DECODED_PIXELS"
            )
        if self.ENTRY_V2_MAX_SOURCE_IMAGE_BYTES > self.CAMERA_EVENT_MAX_BODY_BYTES:
            raise ValueError(
                "ENTRY_V2_MAX_SOURCE_IMAGE_BYTES must be less than or equal to "
                "CAMERA_EVENT_MAX_BODY_BYTES"
            )

        cameras = {}
        ip_map = {}

        # Phase 1 cameras
        phase1_cams = {
            "CAM-01": (self.CAM_01_IP, self.CAM_01_USER, self.CAM_01_PASSWORD, self.CAM_01_NAME),
            "CAM-02": (self.CAM_02_IP, self.CAM_02_USER, self.CAM_02_PASSWORD, self.CAM_02_NAME),
            "CAM-03": (self.CAM_03_IP, self.CAM_03_USER, self.CAM_03_PASSWORD, self.CAM_03_NAME),
            "CAM-04": (self.CAM_04_IP, self.CAM_04_USER, self.CAM_04_PASSWORD, self.CAM_04_NAME),
            "CAM-05": (self.CAM_05_IP, self.CAM_05_USER, self.CAM_05_PASSWORD, self.CAM_05_NAME),
            "CAM-06": (self.CAM_06_IP, self.CAM_06_USER, self.CAM_06_PASSWORD, self.CAM_06_NAME),
            "CAM-07": (self.CAM_07_IP, self.CAM_07_USER, self.CAM_07_PASSWORD, self.CAM_07_NAME),
            "CAM-08": (self.CAM_08_IP, self.CAM_08_USER, self.CAM_08_PASSWORD, self.CAM_08_NAME),
            "CAM-09": (self.CAM_09_IP, self.CAM_09_USER, self.CAM_09_PASSWORD, self.CAM_09_NAME),
            "CAM-10": (self.CAM_10_IP, self.CAM_10_USER, self.CAM_10_PASSWORD, self.CAM_10_NAME),
            "CAM-11": (self.CAM_11_IP, self.CAM_11_USER, self.CAM_11_PASSWORD, self.CAM_11_NAME),
            "CAM-12": (self.CAM_12_IP, self.CAM_12_USER, self.CAM_12_PASSWORD, self.CAM_12_NAME),
            "CAM-13": (self.CAM_13_IP, self.CAM_13_USER, self.CAM_13_PASSWORD, self.CAM_13_NAME),
            "CAM-14": (self.CAM_14_IP, self.CAM_14_USER, self.CAM_14_PASSWORD, self.CAM_14_NAME),
            "CAM-35": (self.CAM_35_IP, self.CAM_35_USER, self.CAM_35_PASSWORD, self.CAM_35_NAME),
        }
        for cam_id, (ip, user, password, name) in phase1_cams.items():
            if ip:
                cameras[cam_id] = {
                    "ip": ip, "user": user, "password": password,
                    "phase": 1, "name": name,
                }
                ip_map[ip] = cam_id

        # Phase 2 cameras
        if self.CAM_ENTRY_IP:
            cameras["CAM-ENTRY"] = {
                "ip": self.CAM_ENTRY_IP, "user": self.CAM_ENTRY_USER,
                "password": self.CAM_ENTRY_PASSWORD, "phase": 2,
                "gate": "entry", "name": self.CAM_ENTRY_NAME,
            }
            ip_map[self.CAM_ENTRY_IP] = "CAM-ENTRY"

        if self.CAM_EXIT_IP:
            cameras["CAM-EXIT"] = {
                "ip": self.CAM_EXIT_IP, "user": self.CAM_EXIT_USER,
                "password": self.CAM_EXIT_PASSWORD, "phase": 2,
                "gate": "exit", "name": self.CAM_EXIT_NAME,
            }
            ip_map[self.CAM_EXIT_IP] = "CAM-EXIT"

        # Internal Floor-to-Floor and Main Entrance/Exit Internal Gates
        apply_gate_rules(cameras)

        self.CAMERAS = cameras
        self.CAMERA_IP_MAP = ip_map

        # Build Serial Number map
        serial_map = {}
        if self.CAM_ENTRY_SERIAL:
            serial_map[self.CAM_ENTRY_SERIAL] = "CAM-ENTRY"
        if self.CAM_EXIT_SERIAL:
            serial_map[self.CAM_EXIT_SERIAL] = "CAM-EXIT"
        self.CAMERA_SERIAL_MAP = serial_map
        return self

    # ── Zone configuration ───────────────────────────────────────────────
    CAMERA_REGION_ZONE_MAP: str = ""  # e.g. CAM-04:0=emergency-exit;CAM-04:1=restricted-vip

    # ── Thresholds ────────────────────────────────────────────────────────
    OCCUPANCY_ALERT_THRESHOLD: float = 0.90
    DEFAULT_ZONE_CAPACITY: int = 9

    # ── Facility timezone (must match the Gateway) ───────────────────────
    # Offset from UTC for "today"/"since-local-midnight" math. The Gateway
    # carries the same env var; both services must use the same value or
    # per-service "today" windows diverge. See Damanat PMS Cameras audit
    # `HIGH_SEVERITY_FIX_PLAN.md` Fix #2 for the operational caveat about
    # Hikvision camera clocks and when 0.0 is the right value.
    FACILITY_TIMEZONE_OFFSET_HOURS: float = 3.0
    
    # False Exit Prevention (Bug #23)
    EXIT_CONFIRM_SECONDS: int = 5
    FORWARD_DIRECTION_FIELD: str = "B-to-A"  # Primary flow (entry/exit/transition)
    USE_EXIT_DIRECTION_VALIDATION: bool = True
    USE_EXIT_CONFIRM_WINDOW: bool = True
    USE_CAM03_ENTRY_CONFIRMATION: bool = True

    # ── ANPR entry burst aggregation (UC1) ───────────────────────────────
    # The entry ANPR camera fires several reads for one car as it approaches
    # (each <picNum> with its own <licensePlate>/<confidenceLevel>). The early
    # read is often wrong; a later read is correct. We buffer the burst and
    # write ONE entry labeled by the LAST read, committed after a short debounce
    # window (idle gap after the final read) — never the first/wrong read.
    ANPR_BURST_WINDOW_SECONDS: float = 2.5   # idle gap after last read → flush
    # Hard cap on a buffer's lifetime = the max time from the FIRST plate read
    # (CAM-ENTRY) within which the ramp crossing (CAM-23) must arrive to confirm
    # the burst. Real-world read→crossing travel time is ~8s at this site, so 8s
    # dropped valid entries by ~1s; 20s covers the travel gap plus a slow driver.
    ANPR_BURST_MAX_SECONDS: float = 20.0     # hard cap on a buffer's lifetime
    # Ramp line-crossing cameras whose crossing confirms "one car physically
    # entered" — used as the entry confirmation + per-car burst boundary, and to
    # detect silent entries (a crossing with no plate read). CAM-23 is the new
    # ramp cam (line-crossing only, no ANPR); CAM-03 is the in-garage backstop.
    ENTRY_CONFIRM_CAMERAS: str = "CAM-23,CAM-03"
    # CAM-23 line id + direction meaning "into the garage" (set from real events,
    # like OCCUPANCY_ENTRANCE_ZONES). At least one is required when CAM-23 is an
    # authoritative V2 confirmation camera; both may stay empty during shadow
    # calibration, where legacy processing remains authoritative.
    CAM23_ENTRY_LINE: str = ""
    CAM23_ENTRY_DIRECTION: str = ""
    # Per-confirmation-camera PMS `direction` marker, so the PMS can tell the
    # ramp-top (CAM-23) and in-garage (CAM-03) images apart. "source:direction"
    # pairs; unlisted sources fall back to "B-entry".
    ENTRY_CONFIRM_DIRECTIONS: str = "CAM-23:ramp-entry,CAM-03:B-entry"
    # How long after an entry is written a late confirmation crossing (CAM-03,
    # deep in the garage) may still attach its image to that entry.
    ENTRY_CONFIRM_MATCH_SECONDS: float = 30.0

    # ── Anti-bounce on entry events (UC1) ────────────────────────────────
    # Suppress an entry-camera ANPR firing if the same plate had an exit
    # within this many seconds — handles the case where the entry camera
    # captures a car driving away from the exit gate. Default 30s
    # (empirical minimum gap between physically passing one camera and
    # then the other). Set to 0 to disable suppression entirely. Was
    # 120s historically; reduced after customer-prod cycling traffic
    # (taxis / delivery vans) lost legitimate re-entries — see
    # entry_exit_service.py:73-103.
    ENTRY_ANTIBOUNCE_SECONDS: int = 30

    # ── PMS/VA forward reliability (port 8000) ───────────────────────────
    # notify_pms_anpr forwards ANPR identity images to the VA core backend. A
    # single fire-and-forget POST silently loses the image whenever VA is
    # momentarily unreachable, so on a transient failure the payload is spooled
    # to disk and re-POSTed from a background task (every DRAIN_INTERVAL) until
    # VA acks. The drain interval is the retry cadence — the live forward stays a
    # single attempt so it never adds latency to the exit webhook / burst flusher.
    PMS_FORWARD_SPOOL_DIR: str = "./pms_forward_spool"
    PMS_FORWARD_DRAIN_INTERVAL_SECONDS: float = 15.0   # background re-POST cadence
    PMS_FORWARD_SPOOL_MAX_AGE_SECONDS: float = 3600.0  # drop spooled payloads older than this

    # ── Snapshot fetch auth fallback ─────────────────────────────────────
    # Default empty = Digest only (Hikvision standard). Set to "basic" to
    # try Basic auth as a second pass when Digest gets 401/403. Used to
    # diagnose firmwares that reject Digest, without changing code.
    # See snapshot_service.py:fetch_snapshot.
    SNAPSHOT_AUTH_FALLBACK: str = ""

    # ── UC3: Occupancy Zone Config ───────────────────────────────────────
    GARAGE_TOTAL_ZONE: str = "GARAGE-TOTAL"
    B1_PARKING_ZONE: str = "B1-PARKING"
    B2_PARKING_ZONE: str = "B2-PARKING"

    OCCUPANCY_CAMERA_ID: str = "CAM-04"
    OCCUPANCY_ZONE_NAME: str = "B1-PARKING"  # Legacy/Default fallback
    
    # Internal Ground Truth Zones (Line detection IDs from CAM-09/CAM-10)
    OCCUPANCY_ENTRANCE_ZONES: str = "1"
    OCCUPANCY_EXIT_ZONES: str= "2"

    # ── Storage ───────────────────────────────────────────────────────────
    STORAGE_MODE: str = "local"          # "local" or "spaces"
    DO_SPACES_KEY: str = ""
    DO_SPACES_SECRET: str = ""
    DO_SPACES_ENDPOINT: str = ""         # e.g. sfo3.digitaloceanspaces.com
    DO_SPACES_BUCKET: str = ""
    DO_SPACES_REGION: str = ""
    DO_SPACES_CDN_URL: str = ""          # e.g. https://bucket.sfo3.cdn.digitaloceanspaces.com

    # ── Event Stream Deduplication ─────────────────────────────────────────
    EVENT_STREAM_SUPPRESS_SECONDS: int = 30      # Suppress duplicate events within this window
    EVENT_STREAM_MAX_DURATION_SECONDS: int = 300  # Reset session after this elapsed time

    # ── Alert Cooldowns ───────────────────────────────────────────────────
    CAPACITY_ALERT_COOLDOWN_SECONDS: int = 5    # Min seconds between capacity_exceeded alerts per zone

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    model_config = ConfigDict(env_file=".env", extra="ignore")

    LOG_CAMERA_FILTER: str = ""
    LOG_CAMERA_EXCLUDE: str = ""

    def get_zone_metadata(self, zone_id: Optional[str]) -> dict[str, Any]:
        """Return canonical metadata for a logical zone or gate."""
        if not zone_id:
            return {}
        return dict(self.ZONE_METADATA.get(zone_id, {}))


settings = Settings()


# ── Facility-local "today" helpers ─────────────────────────────────────────
# Mirrors the Gateway's `app/config.py:facility_tz()` / `facility_today_utc()`
# so PMS-AI's `/api/v1/entry-exit/count/today` and `/api/v1/stats/*` use the
# same window the dashboard shows. Import via:
#     from app.config import settings, facility_today_utc
def facility_tz():
    """The local timezone for "today"-style date math. Configured via
    FACILITY_TIMEZONE_OFFSET_HOURS env var (default 3.0 = UTC+3, Saudi Arabia).

    NEW (2026-05-07): the DB convention shifted from UTC-naive to
    facility-local-naive — every writer now stores the wall-clock time
    operators see, so the customer's dashboard shows the right values
    without any UTC->local math on the frontend. This `facility_tz()`
    helper is still useful for parsing tz-aware camera timestamps before
    stripping the tz."""
    from datetime import timezone, timedelta
    return timezone(timedelta(hours=settings.FACILITY_TIMEZONE_OFFSET_HOURS))


def facility_now_naive():
    """Current facility-local datetime, NAIVE (no tzinfo). Use this for
    every DB write where you used to call `datetime.now(UTC)` or
    `datetime.utcnow()`. The DB convention is "naive datetime is the
    facility wall clock"; calling utcnow() in a UTC-tzed container
    silently subtracts the facility offset and lands rows 3h behind.

    Equivalent (but explicit) to `datetime.now(facility_tz()).replace(tzinfo=None)`,
    works regardless of the host OS / container TZ."""
    from datetime import datetime
    return datetime.now(facility_tz()).replace(tzinfo=None)


def facility_today_utc():
    """[name kept for back-compat; semantics shifted 2026-05-07]
    Naive facility-local datetime of midnight today. Use this when
    filtering DB columns stored as facility-local-naive datetimes
    against "since local midnight today" — pair with
    `facility_tomorrow_utc()` for an exclusive upper bound. Despite the
    name, this no longer involves UTC."""
    from datetime import datetime
    now_local = datetime.now(facility_tz())
    midnight_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_local.replace(tzinfo=None)


def facility_tomorrow_utc():
    """Naive facility-local datetime of midnight at the END of today
    (start of tomorrow). Pairs with `facility_today_utc()` for range
    filters."""
    from datetime import timedelta
    return facility_today_utc() + timedelta(days=1)


def facility_day_range_utc(target_date_str: str | None = None):
    """Return (start, end) naive facility-local range for a given day. If
    target_date_str is None, uses today. Pass a string like "2026-04-13"
    to get any date's window. Both bounds are naive facility-local
    datetimes suitable for filtering DB columns stored facility-local-naive."""
    from datetime import datetime, date as date_cls, timedelta
    if target_date_str is None:
        start = facility_today_utc()
    else:
        d = date_cls.fromisoformat(target_date_str)
        # Naive facility-local datetime at midnight of the target date.
        start = datetime(d.year, d.month, d.day)
    end = start + timedelta(days=1)
    return start, end

