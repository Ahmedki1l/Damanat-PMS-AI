"""Configuration gates required before Entry V2 can become authoritative."""

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_authoritative_requires_camera_source_allowlist():
    with pytest.raises(ValidationError, match="CAMERA_EVENT_ALLOWED_SOURCE_CIDRS"):
        Settings(
            _env_file=None,
            ENTRY_V2_MODE="authoritative",
            CAMERA_EVENT_ALLOWED_SOURCE_CIDRS="",
        )


def test_invalid_camera_source_cidr_fails_all_modes():
    with pytest.raises(ValidationError, match="invalid IP/CIDR"):
        Settings(
            _env_file=None,
            ENTRY_V2_MODE="off",
            CAMERA_EVENT_ALLOWED_SOURCE_CIDRS="not-a-network",
        )


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_non_authoritative_modes_allow_empty_source_allowlist(mode):
    kwargs = {
        "PMS_API_URL": "http://pms-video-analytics:8000",
        "ENTRY_V2_SERVICE_KEY": "pms-va-secret",
    } if mode == "shadow" else {}
    configured = Settings(
        _env_file=None,
        ENTRY_V2_MODE=mode,
        CAMERA_EVENT_ALLOWED_SOURCE_CIDRS="",
        **kwargs,
    )

    assert configured.CAMERA_EVENT_ALLOWED_SOURCE_CIDRS == ""


def test_authoritative_accepts_exact_ips_and_cidrs():
    configured = Settings(
        _env_file=None,
        ENTRY_V2_MODE="authoritative",
        CAMERA_EVENT_ALLOWED_SOURCE_CIDRS="10.1.20.0/24,192.168.1.104",
        PMS_API_URL="http://pms-video-analytics:8000",
        ENTRY_V2_SERVICE_KEY="pms-va-secret",
        CAM23_ENTRY_LINE="park-entry",
    )

    assert configured.ENTRY_V2_MODE == "authoritative"


def test_authoritative_rejects_enabled_cam23_with_empty_entry_filters():
    with pytest.raises(
        ValidationError,
        match="CAM23_ENTRY_LINE or CAM23_ENTRY_DIRECTION",
    ):
        Settings(
            _env_file=None,
            ENTRY_V2_MODE="authoritative",
            CAMERA_EVENT_ALLOWED_SOURCE_CIDRS="10.1.20.60",
            PMS_API_URL="http://pms-video-analytics:8000",
            ENTRY_V2_SERVICE_KEY="pms-va-secret",
            ENTRY_CONFIRM_CAMERAS="CAM-23,CAM-03",
            CAM23_ENTRY_LINE="",
            CAM23_ENTRY_DIRECTION="",
        )


@pytest.mark.parametrize(
    ("line_id", "direction"),
    [("park-entry", ""), ("", "B-to-A")],
)
def test_authoritative_accepts_an_explicit_cam23_entry_filter(
    line_id,
    direction,
):
    configured = Settings(
        _env_file=None,
        ENTRY_V2_MODE="authoritative",
        CAMERA_EVENT_ALLOWED_SOURCE_CIDRS="10.1.20.60",
        PMS_API_URL="http://pms-video-analytics:8000",
        ENTRY_V2_SERVICE_KEY="pms-va-secret",
        ENTRY_CONFIRM_CAMERAS="CAM-23,CAM-03",
        CAM23_ENTRY_LINE=line_id,
        CAM23_ENTRY_DIRECTION=direction,
    )

    assert configured.CAM23_ENTRY_LINE == line_id
    assert configured.CAM23_ENTRY_DIRECTION == direction


def test_shadow_keeps_empty_cam23_filters_for_calibration():
    configured = Settings(
        _env_file=None,
        ENTRY_V2_MODE="shadow",
        PMS_API_URL="http://pms-video-analytics:8000",
        ENTRY_V2_SERVICE_KEY="pms-va-secret",
        ENTRY_CONFIRM_CAMERAS="CAM-23,CAM-03",
        CAM23_ENTRY_LINE="",
        CAM23_ENTRY_DIRECTION="",
    )

    assert configured.CAM23_ENTRY_LINE == ""
    assert configured.CAM23_ENTRY_DIRECTION == ""


def test_authoritative_cam03_only_configuration_does_not_require_cam23_filter():
    configured = Settings(
        _env_file=None,
        ENTRY_V2_MODE="authoritative",
        CAMERA_EVENT_ALLOWED_SOURCE_CIDRS="10.1.20.60",
        PMS_API_URL="http://pms-video-analytics:8000",
        ENTRY_V2_SERVICE_KEY="pms-va-secret",
        ENTRY_CONFIRM_CAMERAS="CAM-03",
        CAM23_ENTRY_LINE="",
        CAM23_ENTRY_DIRECTION="",
    )

    assert configured.ENTRY_CONFIRM_CAMERAS == "CAM-03"


@pytest.mark.parametrize("mode", ["shadow", "authoritative"])
def test_active_entry_v2_requires_peer_url_and_service_key(mode):
    kwargs = {
        "_env_file": None,
        "ENTRY_V2_MODE": mode,
        "CAMERA_EVENT_ALLOWED_SOURCE_CIDRS": "10.1.20.60",
    }
    with pytest.raises(ValidationError, match="PMS_API_URL"):
        Settings(**kwargs)

    with pytest.raises(ValidationError, match="ENTRY_V2_SERVICE_KEY"):
        Settings(
            **kwargs,
            PMS_API_URL="http://pms-video-analytics:8000",
        )


@pytest.mark.parametrize(
    "url",
    [
        "[http://pms-video-analytics:8000](http://pms-video-analytics:8000)",
        "pms-video-analytics:8000",
        "ftp://pms-video-analytics:8000",
        "http://user:password@pms-video-analytics:8000",
        "http://pms-video-analytics:8000?mode=v2",
        "http://pms-video-analytics:8000#entry",
    ],
)
def test_active_entry_v2_rejects_unsafe_or_malformed_peer_url(url):
    with pytest.raises(ValidationError, match="PMS_API_URL"):
        Settings(
            _env_file=None,
            ENTRY_V2_MODE="shadow",
            PMS_API_URL=url,
            ENTRY_V2_SERVICE_KEY="pms-va-secret",
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("ENTRY_V2_CONNECT_TIMEOUT_SECONDS", 0),
        ("ENTRY_V2_READ_TIMEOUT_SECONDS", -1),
        ("ENTRY_V2_WRITE_TIMEOUT_SECONDS", float("nan")),
        ("ENTRY_V2_POOL_TIMEOUT_SECONDS", float("inf")),
        ("ENTRY_V2_SHADOW_QUEUE_CAPACITY", 0),
        ("ENTRY_V2_SHADOW_QUEUE_CAPACITY", 17),
        ("ENTRY_V2_SHADOW_SHUTDOWN_TIMEOUT_SECONDS", 0),
        ("ENTRY_V2_SHADOW_SHUTDOWN_TIMEOUT_SECONDS", 31),
        ("ENTRY_V2_SHADOW_SHUTDOWN_TIMEOUT_SECONDS", float("nan")),
        ("ENTRY_V2_MAX_IMAGE_BYTES", 0),
        ("ENTRY_V2_MAX_IMAGE_BYTES", 4 * 1024 * 1024 + 1),
        ("ENTRY_V2_MAX_SOURCE_IMAGE_BYTES", 0),
        ("ENTRY_V2_MAX_SOURCE_IMAGE_BYTES", 16 * 1024 * 1024 + 1),
        ("ENTRY_V2_MAX_IMAGES", 5),
        ("ENTRY_V2_MAX_DECODED_PIXELS", -1),
        ("ENTRY_V2_MAX_DECODED_PIXELS", 12_000_001),
        ("ENTRY_V2_MAX_IMAGE_DIMENSION", 0),
        ("ENTRY_V2_MAX_IMAGE_DIMENSION", 8193),
        ("ENTRY_V2_MAX_SOURCE_DECODED_PIXELS", 0),
        ("ENTRY_V2_MAX_SOURCE_DECODED_PIXELS", 30_000_001),
        ("ENTRY_V2_CROP_PADDING_RATIO", -0.01),
        ("ENTRY_V2_CROP_PADDING_RATIO", 0.51),
        ("ENTRY_V2_CROP_PADDING_RATIO", float("nan")),
    ],
)
def test_entry_v2_transport_and_image_limits_fail_fast(field_name, value):
    with pytest.raises(ValidationError, match=field_name):
        Settings(_env_file=None, **{field_name: value})


def test_entry_v2_source_decode_limit_cannot_be_below_outbound_limit():
    with pytest.raises(
        ValidationError,
        match="ENTRY_V2_MAX_SOURCE_DECODED_PIXELS",
    ):
        Settings(
            _env_file=None,
            ENTRY_V2_MAX_DECODED_PIXELS=12_000_000,
            ENTRY_V2_MAX_SOURCE_DECODED_PIXELS=11_999_999,
        )


def test_entry_v2_source_image_limit_cannot_exceed_camera_body_limit():
    with pytest.raises(
        ValidationError,
        match="ENTRY_V2_MAX_SOURCE_IMAGE_BYTES",
    ):
        Settings(
            _env_file=None,
            CAMERA_EVENT_MAX_BODY_BYTES=8 * 1024 * 1024,
            ENTRY_V2_MAX_SOURCE_IMAGE_BYTES=8 * 1024 * 1024 + 1,
        )


def test_entry_v2_image_defaults_match_va_intake_contract():
    configured = Settings(_env_file=None)

    assert configured.ENTRY_V2_SHADOW_QUEUE_CAPACITY == 8
    assert configured.ENTRY_V2_SHADOW_SHUTDOWN_TIMEOUT_SECONDS == 5.0
    assert configured.ENTRY_V2_MAX_DECODED_PIXELS == 12_000_000
    assert configured.ENTRY_V2_MAX_IMAGE_DIMENSION == 8192
    assert configured.ENTRY_V2_MAX_IMAGE_BYTES == 4 * 1024 * 1024
    assert configured.ENTRY_V2_MAX_SOURCE_IMAGE_BYTES == 16 * 1024 * 1024
    assert configured.ENTRY_V2_MAX_SOURCE_DECODED_PIXELS == 30_000_000


@pytest.mark.parametrize(
    "value",
    [0, 61, float("inf"), float("nan")],
)
def test_pending_crossing_window_fails_fast_outside_safe_bounds(value):
    with pytest.raises(ValidationError, match="ENTRY_PENDING_CROSSING_SECONDS"):
        Settings(
            _env_file=None,
            ENTRY_PENDING_CROSSING_SECONDS=value,
        )
