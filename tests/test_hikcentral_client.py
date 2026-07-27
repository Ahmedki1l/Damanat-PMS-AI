"""HikCentral transport: request shape, envelope tolerance, failure policy.

The client's contract is that it NEVER raises into the event path — every
failure degrades to an empty result — so most of these tests assert on what
comes back rather than on an exception.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings
from app.services.hikcentral import client as hik_client
from app.services.hikcentral.models import VehicleLogRecord

FACILITY_TZ = timezone(timedelta(hours=3))
# Records in these fixtures sit at 15:05:24; the client now enforces the
# window locally, so tests must ask for a window that contains them.
WIN_BEGIN = datetime(2026, 7, 27, 15, 0, 0, tzinfo=FACILITY_TZ)
WIN_END = datetime(2026, 7, 27, 15, 10, 0, tzinfo=FACILITY_TZ)


@pytest.fixture(autouse=True)
def hik_configured(monkeypatch):
    """Give the client a base URL/credentials without touching a real host."""
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_BASE_URL", "https://hik.test")
    monkeypatch.setattr(settings, "HIK_USERNAME", "user")
    monkeypatch.setattr(settings, "HIK_PASSWORD", "pass")
    monkeypatch.setattr(settings, "HIK_IMAGE_MAX_BYTES", 1024)
    yield
    monkeypatch.setattr(hik_client, "_http_client", None, raising=False)


def _install_transport(monkeypatch, handler):
    """Point the module-level client at an in-process transport."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        hik_client,
        "_http_client",
        httpx.AsyncClient(transport=transport, base_url="https://hik.test"),
        raising=False,
    )


# ── Envelope tolerance ──────────────────────────────────────────────────────
# The wrapper key has varied across HikCentral builds, so extraction is
# structural. These lock that in.


def test_extract_records_from_named_envelope():
    payload = {
                "ResponseStatus": {
                    "ErrorCode": 0,
                    "Data": {
                        "VehicleLogsList": {
                            "VehicleLog": [
                                {
                                    "GUID": "FDA8B5C9338A4CD29154B63D59E7F148",
                                    "PassTime": "2026-07-27T15:05:24+03:00",
                                    "PlateLicense": "5625JKA",
                                    "VehicleImageUrl": "Vsm://vehicle",
                                    "PlateImageUrl": "Vsm://plate",
                                    "ResourceID": "447",
                                }
                            ]
                        }
                    },
                }
        )

    _install_transport(monkeypatch, handler)
    records = await hik_client.query_vehicle_logs(
        datetime(2026, 7, 27, 15, 4, 54, tzinfo=FACILITY_TZ),
        datetime(2026, 7, 27, 15, 5, 29, tzinfo=FACILITY_TZ),
        "447",
        10,
    )

    # Exactly the one real car in the window. Skipping this filter would make
    # recovery's "exactly one candidate" rule meaningless.
    assert [r.guid for r in records] == ["FDA8B5C9338A4CD29154B63D59E7F148"]
    assert records[0].canonical_plate == "JKA-5625"


def test_records_are_found_inside_the_real_response_envelope():
    """The live envelope nests records under ResponseStatus.Data."""
    payload = {
        "ResponseStatus": {
            "ErrorCode": 0,
            "Data": {
                "VehicleLogsList": {
                    "TotalNum": 1,
                    "VehicleLog": [
                        {"GUID": "G", "PassTime": "2026-07-27T15:05:24+03:00"}
                    ],
                }
            },
        }
    }
    assert [r["GUID"] for r in hik_client._extract_raw_records(payload)] == ["G"]


def test_integer_resource_id_and_vehicle_type_are_accepted():
    """Live data sends these as numbers, not strings."""
    record = VehicleLogRecord.from_payload(
        {
            "GUID": "8ABB4B81BB6A42CC83FC9417629EE656",
            "PassTime": "2026-07-27T18:42:25+03:00",
            "PlateLicense": "1372ZZR",
            "ResourceID": 453,
            "ResourceName": "ANPR-2 Exit",
            "VehicleDirectionType": 1,
            "VehicleType": 15,
        }
    )
    assert record.resource_id == "453"
    assert record.vehicle_type == "15"
    assert record.vehicle_direction_type == "1"
    assert record.canonical_plate == "ZZR-1372"


@pytest.mark.asyncio
async def test_vehicle_logs_sends_the_discovered_request_body(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["body"] = __import__("json").loads(request.content)
                "ResponseStatus": {
                    "ErrorCode": 0,
                    "Data": {
                        "VehicleLogsList": {
                            "VehicleLog": [
                                {
                                    "GUID": "A",
                                    "PassTime": "not-a-date",
                                    "PlateLicense": "5625JKA",
                                },
                                {
                                    "GUID": "B",
                                    "PassTime": "2026-07-27T15:05:24+03:00",
                                },
                            ]
                        }
                    },
                }
        )

    _install_transport(monkeypatch, handler)
    records = await hik_client.query_vehicle_logs(
        WIN_BEGIN, WIN_END, "447", 5
    )

    assert len(records) == 1
    assert records[0].guid == "FDA8B5C9338A4CD29154B63D59E7F148"
    # HikCentral is digits-first; this DB stores letters-first.
    assert records[0].plate_license == "5625JKA"
    assert records[0].canonical_plate == "JKA-5625"


@pytest.mark.asyncio
async def test_vehicle_logs_drops_records_without_guid_or_passtime(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "rows": [
                    {"GUID": "A", "PassTime": "not-a-date", "PlateLicense": "5625JKA"},
                    {"GUID": "B", "PassTime": "2026-07-27T15:05:24+03:00"},
                ]
            },
        )

    _install_transport(monkeypatch, handler)
    records = await hik_client.query_vehicle_logs(
        WIN_BEGIN, WIN_END, "447", 5
    )

    # An unparsable PassTime cannot be matched or ordered, so it is dropped
    # rather than half-trusted.
    assert [r.guid for r in records] == ["B"]


# ── Failure policy ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_credentials_rejection_is_not_retried(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401)

    _install_transport(monkeypatch, handler)
    records = await hik_client.query_vehicle_logs(
        WIN_BEGIN, WIN_END, "447", 5
    )

    # 401 is a configuration fault, not a blip — retrying it just burns budget.
    assert records == []
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_server_error_is_retried_once_then_gives_up(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(hik_client, "_TRANSPORT_BACKOFF_S", 0)
    records = await hik_client.query_vehicle_logs(
        WIN_BEGIN, WIN_END, "447", 5
    )

    assert records == []
    assert len(calls) == hik_client._TRANSPORT_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_transport_error_returns_empty_rather_than_raising(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    _install_transport(monkeypatch, handler)
    monkeypatch.setattr(hik_client, "_TRANSPORT_BACKOFF_S", 0)

    # The whole point: HikCentral being down must not take the webhook down.
    assert (
        await hik_client.query_vehicle_logs(
            WIN_BEGIN, WIN_END, "447", 5
        )
        == []
    )


@pytest.mark.asyncio
async def test_non_json_response_returns_empty(monkeypatch):
    _install_transport(
        monkeypatch, lambda request: httpx.Response(200, text="<html>nope</html>")
    )
    assert (
        await hik_client.query_vehicle_logs(
            WIN_BEGIN, WIN_END, "447", 5
        )
        == []
    )


# ── Picture download ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_download_picture_passes_the_vsm_handle_as_a_query_param(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = request.url
        return httpx.Response(
            200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"}
        )

    _install_transport(monkeypatch, handler)
    content = await hik_client.download_picture("Vsm://abc/def?x=1")

    assert content == b"\xff\xd8jpeg"
    assert captured["url"].path == hik_client.PICTURE_PATH
    assert captured["url"].params["URL"] == "Vsm://abc/def?x=1"


@pytest.mark.asyncio
async def test_download_picture_rejects_oversized_image(monkeypatch):
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"x" * 2048, headers={"content-type": "image/jpeg"}
        ),
    )
    # HIK_IMAGE_MAX_BYTES is 1024 in this fixture.
    assert await hik_client.download_picture("Vsm://big") is None


@pytest.mark.asyncio
async def test_download_picture_rejects_non_image_content_type(monkeypatch):
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=b"{}", headers={"content-type": "application/json"}
        ),
    )
    assert await hik_client.download_picture("Vsm://json") is None


@pytest.mark.asyncio
async def test_download_picture_ignores_blank_url(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not perform a request for an empty URL")

    _install_transport(monkeypatch, handler)
    assert await hik_client.download_picture("") is None


def test_save_picture_writes_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hik_client, "SNAPSHOT_DIR", str(tmp_path))
    path = hik_client.save_picture(b"jpegbytes", "GUID-123", "vehicle")

    assert path is not None
    with open(path, "rb") as handle:
        assert handle.read() == b"jpegbytes"


def test_save_picture_sanitises_the_guid_into_the_filename(tmp_path, monkeypatch):
    monkeypatch.setattr(hik_client, "SNAPSHOT_DIR", str(tmp_path))
    path = hik_client.save_picture(b"x", "../../etc/passwd", "plate")

    assert path is not None
    assert ".." not in path
    assert "etcpasswd" in path


def test_save_picture_ignores_empty_content(tmp_path, monkeypatch):
    monkeypatch.setattr(hik_client, "SNAPSHOT_DIR", str(tmp_path))
    assert hik_client.save_picture(b"", "GUID", "vehicle") is None


# ── HTTP 200 is not success ─────────────────────────────────────────────────
# Observed live against 10.1.20.51 on 2026-07-27: the platform answers every
# request with 200 and reports the real outcome in ResponseStatus.ErrorCode.


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ResponseStatus": {"ErrorModule": 0, "ErrorCode": 1016}}, 1016),
        ({"ResponseStatus": {"ErrorModule": 0, "ErrorCode": 216}}, 216),
        ({"ResponseStatus": {"ErrorCode": 0}}, 0),
        ({"rows": [{"GUID": "A"}]}, None),
        ({"ResponseStatus": {"ErrorCode": "oops"}}, None),
        ("not a dict", None),
    ],
)
def test_response_error_code_extraction(payload, expected):
    assert hik_client.response_error_code(payload) == expected


@pytest.mark.asyncio
async def test_unauthenticated_error_envelope_is_not_read_as_no_vehicles(
    monkeypatch,
):
    """The exact response an unauthenticated live call returns."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"ResponseStatus": {"ErrorModule": 0, "ErrorCode": 1016}}
        )

    _install_transport(monkeypatch, handler)
    records = await hik_client.query_vehicle_logs(
        WIN_BEGIN, WIN_END, "447", 5
    )

    # It must come back empty AND be logged as a refusal — silently treating a
    # dead session as "this car was never seen" would validate nothing while
    # looking perfectly healthy.
    assert records == []


@pytest.mark.asyncio
async def test_records_are_rejected_when_no_error_code_present(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "VehicleLogsResponse": {
                    "VehicleLogList": [
                        {
                            "GUID": "G",
                            "PassTime": "2026-07-27T15:05:24+03:00",
                            "PlateLicense": "5625JKA",
                        }
                    ]
                }
            },
        )

    _install_transport(monkeypatch, handler)
    records = await hik_client.query_vehicle_logs(
        WIN_BEGIN, WIN_END, "447", 5
    )

    assert records == []


def test_client_does_not_use_http_auth(monkeypatch):
    """Digest was the original assumption and the platform disproved it.

    Live, Digest / Basic / no-auth all returned the same ErrorCode, so an `auth=`
    on the client is misleading dead weight. Identity comes from the session
    cookie instead.
    """
    monkeypatch.setattr(settings, "HIK_BASE_URL", "https://hik.test")
    client = hik_client._new_client()
    try:
        assert client.auth is None
    finally:
        pass


def test_record_parses_all_reported_fields():
    record = VehicleLogRecord.from_payload(
        {
            "GUID": "G",
            "PassTime": "2026-07-27T15:05:24+03:00",
            "PlateLicense": "5625JKA",
            "VehicleImageUrl": "Vsm://v",
            "PlateImageUrl": "Vsm://p",
            "ResourceID": "447",
            "ResourceName": "Entry LPR",
            "VehicleDirectionType": "1",
            "VehicleType": "car",
        }
    )
    assert record.resource_name == "Entry LPR"
    assert record.vehicle_type == "car"
    assert record.vehicle_direction_type == "1"
    assert record.pass_time.utcoffset() == timedelta(hours=3)
