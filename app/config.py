# app/config.py
"""
Application configuration using Pydantic-Settings.
All settings can be overridden via environment variables or .env file.
"""

import warnings
from ipaddress import ip_network
from typing import Any, Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ConfigDict, Field, PrivateAttr, model_validator
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


def join_resource_ids(value: str) -> str:
    """Normalize a comma-separated HikCentral resource-ID list.

    HikCentral's VehicleLogs `ResourceIDs` is a single comma-joined string, so
    this trims blanks and de-duplicates while preserving configured order.
    """
    seen: list[str] = []
    for item in (value or "").split(","):
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return ",".join(seen)


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
    # Shadow delivery is detached from the camera request. Bound the number of
    # image-bearing events retained in memory while VA is slow or unavailable.
    ENTRY_V2_SHADOW_QUEUE_CAPACITY: int = Field(default=8, gt=0, le=16)
    ENTRY_V2_SHADOW_SHUTDOWN_TIMEOUT_SECONDS: float = Field(
        default=5.0,
        gt=0,
        le=30.0,
        allow_inf_nan=False,
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
    # The ANPR detection picture is a COMPOSITE frame: Hikvision paints its own
    # plate crop and an OSD annotation band into the top-left corner, and the
    # `vehicelRect` inside pictureInfo describes THAT overlay box - not the car in
    # the scene. Cropping to the rectangle therefore handed VA a re-read of
    # Hikvision's own plate output (or, when the crop clipped the band, the
    # timestamp text) and never the vehicle itself.
    #
    # With this on, the ANPR overview is forwarded as a bounded full frame so VA
    # localizes the plate with its own detector - independent evidence rather than
    # a re-read, and immune to the camera's overlay setting being toggled off.
    # Measured over 87 production frames: VA's LPD finds the real plate at
    # 138-293px (median 171, ~222px after crop padding) and detection improves from
    # 27% to 42% once the overlay stops competing for the top box. 2688x1552 is
    # 4.2Mpx against ENTRY_V2_MAX_DECODED_PIXELS=12Mpx, so nothing is downscaled.
    #
    # Set False to restore the rectangle crop if field results regress.
    ENTRY_V2_ANPR_FULL_FRAME: bool = True
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

    # ── Unmatched-exit resolution (UC2) ──────────────────────────────────
    # An exit whose plate matches no open session is usually one of two things:
    # a car whose ENTRY was lost (plate is right), or a car whose entry plate was
    # misread (plate is wrong). exit_match_service separates them; these bound
    # what it may consider.
    EXIT_MATCH_ENABLED: bool = True
    # A digit group shorter than this is too weak to nominate a match on — plenty
    # of real plates share one or two digits.
    EXIT_MATCH_MIN_DIGITS: int = 3
    # How many ranked candidates to keep for logging / appearance scoring. Only
    # ever a shortlist: the deterministic rules below decide on uniqueness, not
    # on rank.
    EXIT_MATCH_SHORTLIST: int = 5
    # Never match an exit against a session older than this. A long-abandoned
    # phantom must not be revived by an unrelated car days later.
    # RETIRED 2026-08-18. Bounded the matcher's candidate pool to 72h while
    # `close_session` had no bound at all, so the two halves of one decision
    # disagreed about which stays exist — and the sessions that most needed
    # resolving (ABR-8000 at 98h, KBD-6795 at 120h) were exactly the ones it
    # hid. Age is now an attribute of a candidate, not a filter on the pool.
    # Kept as a no-op so a deployed ConfigMap still validates; remove it there.
    EXIT_MATCH_MAX_AGE_HOURS: float = 72.0
    # Entry time comes from the entry camera's clock, exit time from the exit
    # camera's, so "a car cannot leave before it arrived" needs a tolerance for
    # the drift between two Hikvision devices.
    #
    # 20s, derived rather than guessed. HIK_MATCH_MAX_SKEW_SECONDS below refuses
    # to pair a HikCentral record with a gate event more than 10s apart, and all
    # 129 entry validations in ai-logs.txt (8/10-8/16) passed that gate — so each
    # camera sits within 10s of the platform, and two cameras within 20s of each
    # other at worst.
    #
    # This is NOT a "shortest stay" allowance and must not be widened into one.
    # The 124 matched stays in that window run 248s (4.1 min) at the shortest,
    # p50 6.8h; not one is under 120s. Nothing real lives down here.
    #
    # The cost of widening is asymmetric and easy to miss. A car that exits and
    # RE-ENTERS within the tolerance has two stays: the exit belongs to the old
    # one, but a reconciled exit arriving late would find the NEW stay inside the
    # window, match its plate exactly, and close a car that is sitting in the
    # garage. Every extra second buys nothing measurable and widens that hole.
    #
    # To measure it properly now that HIK_EXIT_RESOURCE_IDS is configured: pull
    # one car's entry and exit passes from HikCentral, compare each against the
    # gate event this service recorded, and difference the two offsets. The
    # platform is the shared clock.
    EXIT_CLOCK_SKEW_SECONDS: float = Field(
        default=20.0, ge=0, le=3600.0, allow_inf_nan=False,
    )
    # Appearance scoring (VA /api/reid/compare) for exits the plate rules cannot
    # settle — the "both letters AND digits misread" case, where no string logic
    # can help. VA scores, PMS-AI decides.
    EXIT_MATCH_REID_ENABLED: bool = True
    EXIT_MATCH_REID_TIMEOUT_SECONDS: float = 5.0
    # Gap between the best and second-best candidate. A margin, not a threshold:
    # absolute similarity drifts with light and viewpoint, while the gap to the
    # runner-up is what actually carries the decision. 0.35 is the 100%-precision
    # floor measured on the 50-identity gallery (see slot_recovery_solo_min_margin
    # in VA). It is a starting point for a DIFFERENT geometry — entry camera vs
    # exit camera — and should be re-measured against exits that matched on plate.
    EXIT_MATCH_REID_MIN_MARGIN: float = 0.35
    # Absolute floor. Without it a margin can be "won" by two equally bad scores.
    EXIT_MATCH_REID_MIN_SCORE: float = 0.50

    # ── ANPR entry burst aggregation (UC1) ───────────────────────────────
    # The entry ANPR camera fires several reads for one car as it approaches
    # (each <picNum> with its own <licensePlate>/<confidenceLevel>). The early
    # read is often wrong; a later read is correct. We buffer the burst and
    # write ONE entry labeled by the LAST read, committed after a short debounce
    # window (idle gap after the final read) — never the first/wrong read.
    ANPR_BURST_WINDOW_SECONDS: float = 2.5   # idle gap after last read → flush
    # How close two reads must be for a REPEATED picNum to count as the same car
    # re-read rather than the next car. Only consulted when the plates are also
    # the same or one is a truncation of the other (see _is_same_car_reread), so
    # this bounds an already plate-gated exception.
    #
    # Measured against the CAMERA's trigger time, not arrival time — the two are
    # not the same clock. On 2026-08-09 the reads arrived 1s apart but carried
    # trigger times 3s apart (09:27:16 and 09:27:19), so a 2s window would have
    # split the car anyway and the fix would not have fixed anything. One car
    # sits in front of the gate ANPR for several seconds; 5s covers that.
    #
    # It does not need to be tight. ANPR_BURST_WINDOW_SECONDS already closes a
    # burst that goes idle, so two reads can only reach this check if they landed
    # inside that debounce — this window just rejects a stale trigger time.
    ANPR_BURST_SAME_CAR_SECONDS: float = 5.0
    # A CAM-23 crossing can reach PMS-AI before Hikvision emits the associated
    # ANPR webhook. This is a separate correlation window, not the read-idle
    # debounce above. Keep it tight because the crossing itself has no plate.
    ENTRY_PENDING_CROSSING_SECONDS: float = Field(
        default=10.0,
        gt=0,
        le=60.0,
        allow_inf_nan=False,
    )
    # Hard cap on a buffer's lifetime = the max time from the FIRST plate read
    # (CAM-ENTRY) within which the ramp crossing (CAM-23) must arrive to confirm
    # the burst. Real-world read→crossing travel time is ~8s at this site, so 8s
    # dropped valid entries by ~1s; 20s covers the travel gap plus a slow driver.
    #
    # 20s covered TRAVEL but not WAITING. The plate is read at the barrier, so the
    # clock starts there - a driver held at a closed barrier, queued behind another
    # car, or paused for a pedestrian blows the cap and the entry is discarded with
    # a perfectly good plate. Measured read->CAM-03 is already 7-20s on a car that
    # never stops. The pre-burst path allowed 60s (PENDING_ENTRY_TTL_SECONDS) and
    # did not have this failure, so 60s restores the tolerance the site was built
    # around. Superseded by the no-timeout Entry V2 design once it is authoritative.
    ANPR_BURST_MAX_SECONDS: float = 60.0     # hard cap on a buffer's lifetime
    # The VMR->ANPR camera-identity hint is bounded SEPARATELY and deliberately
    # stays tight. It used to borrow ANPR_BURST_MAX_SECONDS, so raising the burst
    # cap would have silently tripled the window in which a stale hint - or the
    # plate-independent FIFO pairing - can attach one car's gate identity AND its
    # plate to a different car. A correctness guard, not a patience setting.
    VMR_GATE_HINT_TTL_SECONDS: float = Field(
        default=20.0,
        gt=0,
        le=60.0,
        allow_inf_nan=False,
    )
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

    # Cameras whose line crossings drive the B2 count as a running DELTA
    # (+1 on the entrance-facing line, -1 on the exit-facing one). Each camera
    # covers its own passage, so a car going down to B2 crosses exactly ONE of
    # them and every crossing counts — do NOT add a camera here that sees the
    # same ramp as another, or one car will be counted twice.
    B2_CROSSING_CAMERAS: str = "CAM-09,CAM-10"

    # Dedup window for the delta cameras above, in seconds. Deliberately MUCH
    # shorter than EVENT_STREAM_SUPPRESS_SECONDS: that 30s window exists to
    # suppress a redundant *recount*, which is free to drop. A delta is not —
    # two cars down the same ramp 10s apart are two events that must both
    # count, so a 30s window would silently lose the second one. This only
    # needs to absorb Hikvision firing one physical crossing twice.
    OCCUPANCY_CROSSING_DEDUP_SECONDS: float = 2.0

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

    # ── Alert Notification Suppression ────────────────────────────────────
    # Comma-separated alert_type values that must NOT be pushed to the
    # real-time SSE stream (/api/v1/alerts/stream). Suppression is
    # notification-only: the row is still written to `alerts`, still logged,
    # and still served by GET /api/v1/alerts — the dashboard just stops
    # popping a live notification for it. Set to "" to notify on everything.
    SUPPRESSED_ALERT_NOTIFICATION_TYPES: str = "silent_entry"
    # Comma-separated alert_type values that are turned OFF entirely: no DB row,
    # no log, no stream — create_alert() drops them before anything is written.
    # Use this (not the notification list above) to make an alert type vanish
    # completely, e.g. DISABLED_ALERT_TYPES=silent_entry once HikCentral
    # recovery/reconciliation makes those alerts redundant.
    DISABLED_ALERT_TYPES: str = ""

    # ── HikCentral plate validation / recovery ────────────────────────────
    # HikCentral is NOT the normal plate source — the ANPR camera already
    # reports the plate. HikCentral has exactly two jobs: validate the plate
    # the camera reported, and recover a plate when the camera reported none
    # (~22% of cars, today lost entirely as `silent_entry`).
    #
    # off           — never contacted; behaviour identical to before this layer.
    # shadow        — looked up and logged, but the ANPR plate always wins and
    #                 no recovery session is created. Changes NO behaviour;
    #                 exists to measure mismatch/recovery rates before trusting
    #                 them. This is the safe rollout default.
    # authoritative — a disagreeing HikCentral plate replaces the ANPR plate,
    #                 and a unique HikCentral record recovers a missing plate.
    HIK_VALIDATION_MODE: Literal["off", "shadow", "authoritative"] = "off"
    HIK_BASE_URL: str = ""
    # Artemis OpenAPI credentials (AppKey/AppSecret) from the HikCentral
    # Integration Partner. Every request is HmacSHA256-signed with these — there
    # is no login, session, or cookie. Keep HIK_APP_SECRET secret (env only).
    HIK_APP_KEY: str = ""
    HIK_APP_SECRET: str = ""
    HIK_VERIFY_TLS: bool = True
    # OpenAPI camera indexCode for the entry LPR camera (e.g. "447" = ANPR-1
    # Entry, discovered via /artemis/api/resource/v1/cameras). One code per
    # lookup.
    HIK_ENTRY_RESOURCE_IDS: str = ""
    # OpenAPI camera indexCode for the exit LPR camera ("510" = ANPR-2 Exit,
    # confirmed against /artemis/api/resource/v1/cameras on 2026-08-10).
    # Only used by the reconciliation poller to close sessions for missed exits.
    #
    # This was "453" — an indexCode that exists on NO camera. crossRecords
    # answers an unknown cameraIndexCode with HTTP 200, code=0 and an empty
    # list, exactly like a camera that genuinely had no passes, so the exit
    # reconciler swept and found nothing every time without ever erroring. It
    # had never closed a single missed exit. Verify any change to this value
    # with `backfill_missed_exits.py --list-cameras`, never by eye.
    HIK_EXIT_RESOURCE_IDS: str = ""
    # Lookup window around the anchor event (the ANPR read for a validation,
    # the ramp crossing for a recovery). Deliberately tiny: HikCentral is asked
    # about ONE car, never for history. Lookback covers the camera->platform
    # ingestion delay; lookahead covers clock skew between the two systems.
    HIK_QUERY_LOOKBACK_SECONDS: float = Field(
        default=30.0, gt=0, le=300.0, allow_inf_nan=False,
    )
    HIK_QUERY_LOOKAHEAD_SECONDS: float = Field(
        default=5.0, ge=0, le=60.0, allow_inf_nan=False,
    )
    # PageSize must leave room for cars that passed AFTER the anchor, because
    # results come back newest-first and the upper bound is filtered locally.
    HIK_QUERY_PAGE_SIZE: int = Field(default=10, gt=0, le=50)
    # HikCentral shifts BeginTime by the facility's UTC offset applied TWICE
    # (measured: +6h at UTC+3 — a window sent as 09:00 filtered from 15:00), and
    # ignores EndTime entirely. The client pre-subtracts this and re-applies the
    # upper bound locally. Configurable because it was derived by measurement,
    # not documentation, and may differ on another platform build or timezone.
    HIK_QUERY_TIME_SHIFT_HOURS: float = Field(
        default=6.0, ge=-24.0, le=24.0, allow_inf_nan=False,
    )
    # How long after an exit read to ask HikCentral a SECOND time, when the
    # first lookup found no record for the pass at all.
    #
    # Measured on ai-logs.txt (8/10-8/16): 129 entry validations, zero misses —
    # but the entry path asks LATE. It waits for the ramp crossing and the burst
    # debounce, so its lookups land 7-44s after the pass (p50 12s). The exit path
    # has nothing to wait for and asks at ~2-3s, earlier than any measurement in
    # that set: 129/129 proves the record exists by 7s, and says nothing about 2s.
    #
    # Widening HIK_QUERY_LOOKBACK_SECONDS cannot cover this. That window is in
    # RECORD time — it decides which passes match, not whether the platform has
    # written one yet. Only asking again does. 0 disables the second ask.
    EXIT_HIK_RECHECK_SECONDS: float = Field(
        default=15.0, ge=0, le=300.0, allow_inf_nan=False,
    )
    # A HikCentral record may only be paired with a gate event this far away.
    HIK_MATCH_MAX_SKEW_SECONDS: float = Field(
        default=10.0, gt=0, le=120.0, allow_inf_nan=False,
    )
    # How close a FULLER record must be to be treated as the same car's better
    # read of a plate this camera truncated. Deliberately tighter than the match
    # skew above: the two reads of one car are seconds apart, while a genuinely
    # short-plated car passing near a long-plated one is a different car whose
    # plate must not be rewritten. Effective value is capped by
    # HIK_MATCH_MAX_SKEW_SECONDS — a candidate must clear both.
    HIK_PARTIAL_MATCH_MAX_SKEW_SECONDS: float = Field(
        default=5.0, gt=0, le=120.0, allow_inf_nan=False,
    )
    HIK_CONNECT_TIMEOUT_SECONDS: float = Field(
        default=3.0, gt=0, le=30.0, allow_inf_nan=False,
    )
    HIK_READ_TIMEOUT_SECONDS: float = Field(
        default=5.0, gt=0, le=60.0, allow_inf_nan=False,
    )
    # Hard cap on a single downloaded vehicle/plate image.
    HIK_IMAGE_MAX_BYTES: int = Field(default=8 * 1024 * 1024, gt=0)

    # ── Reconciliation (event-driven) ─────────────────────────────────────
    # HikCentral is swept for gate events the edge pipeline never saw (ANPR +
    # CAM-23/08 both missed), and they are applied: a missed ENTRY opens a
    # recovery session, a missed EXIT closes an open one. This is NOT a timer —
    # it is triggered by real gate-area events (below) and debounced, so it only
    # runs when cars are actually moving. Runs in shadow (logs would-do) and
    # authoritative (acts); off = never. GUID dedup (hik_validations) makes
    # overlapping sweeps idempotent.
    #
    # Cameras whose events trigger an entry/exit sweep. Any event from one of
    # these acts as the heartbeat for that direction.
    HIK_RECONCILE_ENTRY_TRIGGER_CAMERAS: str = "CAM-23,CAM-03,CAM-ENTRY"
    HIK_RECONCILE_EXIT_TRIGGER_CAMERAS: str = "CAM-08,CAM-EXIT"
    # Minimum gap between sweeps of the same direction, so a busy camera does
    # not fire a HikCentral call on every frame. Event-driven, just debounced.
    HIK_RECONCILE_DEBOUNCE_SECONDS: float = Field(
        default=30.0, ge=0, le=600.0, allow_inf_nan=False,
    )
    # Only reconcile records OLDER than this, so the live edge pipeline (burst
    # buffer + pending-crossing timeout) has already had its chance — the sweep
    # never races an in-flight car.
    HIK_RECONCILE_GRACE_SECONDS: float = Field(
        default=120.0, gt=0, le=3600.0, allow_inf_nan=False,
    )
    # How far back each poll looks. Overlaps are safe (GUID dedup), so this is
    # generous enough to survive a poll or two being skipped.
    HIK_RECONCILE_LOOKBACK_SECONDS: float = Field(
        default=900.0, gt=0, le=86400.0, allow_inf_nan=False,
    )
    # A HikCentral pass counts as "already noticed" if an EntryExitLog for the
    # same plate/gate sits within this many seconds of it.
    HIK_RECONCILE_MATCH_SECONDS: float = Field(
        default=60.0, gt=0, le=600.0, allow_inf_nan=False,
    )
    # Max records pulled per camera per poll (crossRecords pageSize, ≤ 500).
    HIK_RECONCILE_PAGE_SIZE: int = Field(default=100, gt=0, le=500)

    # ── Restart catch-up ──────────────────────────────────────────────────
    # The rolling sweep above is near-sighted by design (it runs on every gate
    # event), so it cannot heal downtime: a 4h outage on 2026-08-09 stranded 25
    # exits that a 15-minute window could never reach. On startup, sweep from
    # the last HikCentral pass actually consumed (hik_validations.pass_time)
    # through to now instead of a fixed lookback.
    HIK_CATCHUP_ON_STARTUP: bool = True
    # Upper bound on how far back a catch-up will reach, so a DB restored from
    # an old backup cannot trigger a week-long sweep. Older gaps are a
    # deliberate operator decision: scripts/setup/backfill_missed_exits.py.
    HIK_CATCHUP_MAX_HOURS: float = Field(default=24.0, gt=0, le=168.0)
    # The gap is walked in chunks because query_vehicle_logs does NOT paginate
    # (pageNo=1, newest-first): a window holding more than
    # HIK_RECONCILE_PAGE_SIZE records silently drops the OLDEST ones, which are
    # exactly what a catch-up is looking for. Keep chunk * peak-arrival-rate
    # comfortably under the page size.
    HIK_CATCHUP_CHUNK_MINUTES: float = Field(default=30.0, gt=0, le=1440.0)

    # ── VA slot recovery ──────────────────────────────────────────────────
    # Opens a session for a car Video Analytics finds parked with no record of it
    # entering — the entry was missed entirely (no ANPR, no HikCentral, no ramp
    # crossing). OFF by default: it creates sessions from evidence that never
    # passed the gate, so it is opt-in per deployment.
    SLOT_RECOVERY_ENABLED: bool = False
    # PMS-AI's own bar, applied on top of VA's. Deliberately duplicated rather than
    # trusted: the two services deploy independently, and this is the side that
    # owns parking_sessions. Defaults match VA's measured floors — verified-correct
    # answers scored 0.620-0.909 with margins 0.102-0.483 on the live fleet
    # (2026-07-30), while an unenrolled car scores 0.218-0.339 against everything.
    SLOT_RECOVERY_MIN_REID_SCORE: float = Field(default=0.55, ge=0.0, le=1.0)
    SLOT_RECOVERY_MIN_REID_MARGIN: float = Field(default=0.10, ge=0.0, le=1.0)

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    model_config = ConfigDict(env_file=".env", extra="ignore")

    LOG_CAMERA_FILTER: str = ""
    LOG_CAMERA_EXCLUDE: str = ""

    # Set when an active HIK_VALIDATION_MODE was forced to "off" because the
    # layer was misconfigured. Startup logs it; see _validate_hikcentral.
    _hik_disabled_reason: str = PrivateAttr(default="")

    def hik_disabled_reason(self) -> str:
        """Why HikCentral was force-disabled at startup, or "" if it wasn't."""
        return self._hik_disabled_reason

    def _hikcentral_config_error(self) -> Optional[str]:
        """Why the HikCentral layer cannot run, or None when it is usable."""
        base_url = self.HIK_BASE_URL.strip()
        if not base_url:
            return "HIK_BASE_URL is required"
        try:
            parsed_url = urlsplit(base_url)
            _ = parsed_url.port
        except ValueError:
            return f"HIK_BASE_URL={base_url!r} is not a valid URL"
        if parsed_url.scheme not in {"http", "https"}:
            return (
                f"HIK_BASE_URL={base_url!r} has no http:// or https:// scheme "
                f"(use e.g. 'https://{base_url}')"
            )
        if (
            not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            return (
                f"HIK_BASE_URL={base_url!r} must be a credential-free base URL "
                "with no query or fragment"
            )
        # The OpenAPI signs every request with these, so both are required for
        # any non-off mode. A missing one degrades to off (below), never raises.
        if not self.HIK_APP_KEY.strip():
            return "HIK_APP_KEY is required"
        if not self.HIK_APP_SECRET.strip():
            return "HIK_APP_SECRET is required"
        if not self.hik_entry_resource_ids():
            return "HIK_ENTRY_RESOURCE_IDS is required"
        return None

    @model_validator(mode="after")
    def _validate_hikcentral(self) -> "Settings":
        """Disable the HikCentral layer when it is on but unconfigured.

        This deliberately does NOT mirror the ENTRY_V2_MODE guard, which raises.
        The difference is what "off" means for each. Entry V2 owns the entry
        path, so a misconfigured one has no safe fallback and must stop the
        process. HikCentral is purely additive — `off` is exactly the behaviour
        that shipped before this layer existed — so degrading to it is both
        correct and survivable.

        Raising here instead cost a production outage on 2026-07-27: a deployed
        `HIK_BASE_URL` without its scheme crash-looped every worker at import
        time, taking down the whole backend for a feature nothing depends on.
        A validation add-on must never be able to stop the app from booting.
        """
        if self.HIK_VALIDATION_MODE == "off":
            return self

        reason = self._hikcentral_config_error()
        if reason:
            requested = self.HIK_VALIDATION_MODE
            self.HIK_VALIDATION_MODE = "off"
            message = (
                f"HIK_VALIDATION_MODE={requested} was requested but the layer "
                f"is misconfigured: {reason}. Falling back to "
                "HIK_VALIDATION_MODE=off. Plate validation and recovery are "
                "DISABLED. Nothing else is affected."
            )
            self._hik_disabled_reason = message
            # app.utils.logger imports this module, so no logger exists yet;
            # startup re-emits this at ERROR once logging is configured.
            warnings.warn(f"[Hik] {message}", RuntimeWarning, stacklevel=2)
            return self

        self.HIK_BASE_URL = self.HIK_BASE_URL.strip().rstrip("/")

        # The reconcile sweep must never see a pass the burst buffer is still
        # deciding about. A refused burst only becomes invisible to the sweep once
        # it has been dropped (at ANPR_BURST_MAX_SECONDS) and its HikCentral GUID
        # consumed — so a grace shorter than that lets the sweep re-open an entry
        # the crossing gate was about to refuse. Clamp rather than raise: this
        # layer is additive and must not stop the app from booting.
        min_grace = self.ANPR_BURST_MAX_SECONDS + 30.0
        if self.HIK_RECONCILE_GRACE_SECONDS < min_grace:
            warnings.warn(
                f"[Hik] HIK_RECONCILE_GRACE_SECONDS="
                f"{self.HIK_RECONCILE_GRACE_SECONDS} is below "
                f"ANPR_BURST_MAX_SECONDS={self.ANPR_BURST_MAX_SECONDS} + 30s "
                f"margin; raising it to {min_grace} so the reconcile sweep "
                "cannot re-open an entry the crossing gate refused.",
                RuntimeWarning,
                stacklevel=2,
            )
            self.HIK_RECONCILE_GRACE_SECONDS = min_grace
        return self

    def hik_entry_resource_ids(self) -> str:
        """Entry LPR camera indexCode."""
        return join_resource_ids(self.HIK_ENTRY_RESOURCE_IDS)

    def hik_exit_resource_ids(self) -> str:
        """Exit LPR camera indexCode (reconciliation only)."""
        return join_resource_ids(self.HIK_EXIT_RESOURCE_IDS)

    def hik_reconcile_entry_trigger_cameras(self) -> set[str]:
        """Cameras whose events trigger an entry reconcile sweep."""
        return {
            c.strip()
            for c in self.HIK_RECONCILE_ENTRY_TRIGGER_CAMERAS.split(",")
            if c.strip()
        }

    def hik_reconcile_exit_trigger_cameras(self) -> set[str]:
        """Cameras whose events trigger an exit reconcile sweep."""
        return {
            c.strip()
            for c in self.HIK_RECONCILE_EXIT_TRIGGER_CAMERAS.split(",")
            if c.strip()
        }

    def suppressed_alert_notification_types(self) -> set[str]:
        """alert_type values excluded from the real-time SSE stream."""
        return {
            t.strip()
            for t in self.SUPPRESSED_ALERT_NOTIFICATION_TYPES.split(",")
            if t.strip()
        }

    def disabled_alert_types(self) -> set[str]:
        """alert_type values turned off entirely (no DB row, no stream)."""
        return {
            t.strip()
            for t in self.DISABLED_ALERT_TYPES.split(",")
            if t.strip()
        }

    def b2_crossing_cameras(self) -> set[str]:
        """Camera ids whose line crossings move the B2 count by a delta."""
        return {c.strip() for c in self.B2_CROSSING_CAMERAS.split(",") if c.strip()}

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

