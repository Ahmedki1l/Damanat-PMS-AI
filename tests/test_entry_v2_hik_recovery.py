"""Entry V2 stage S7 — dropped-ANPR recovery, and the gate in front of it.

The correlation being tested: a VCA event on a ramp camera with NO ANPR
crossRecord near it is a dropped gate read. The event says something crossed
the line; the absent crossRecord says the gate never named it.

The gate in front of it matters as much as the correlation. An unknown
indexCode answers HTTP 200 / code=0 / empty, which is byte-for-byte what an
idle camera returns — so "no events" is never allowed to be reported as a
confident zero when we never actually asked.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import entry_v2_hik_recovery as recovery


NOW = datetime(2026, 8, 29, 12, 0, 0)


def _event(code, seconds_offset, image="pic://x", src="510"):
    return SimpleNamespace(
        event_index_code=code,
        src_index=src,
        start_time=NOW + timedelta(seconds=seconds_offset),
        event_pic_uri=image,
        event_type="131585",
    )


def _anpr(seconds_offset):
    return SimpleNamespace(
        guid=f"g{seconds_offset}",
        plate="ABC-1234",
        pass_time=NOW + timedelta(seconds=seconds_offset),
        vehicle_image_url="pic://a",
    )


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(recovery.settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(recovery.settings, "HIK_RAMP_RESOURCE_IDS", "510,511")


async def _run(events, anpr_records, **kwargs):
    with patch.object(recovery.hikcentral, "is_enabled", return_value=True), \
         patch.object(
             recovery.hik_client, "list_camera_events",
             AsyncMock(return_value=events),
         ), \
         patch.object(
             recovery.hikcentral, "list_entry_candidates",
             AsyncMock(return_value=anpr_records),
         ):
        return await recovery.find_dropped_gate_reads(observed_at=NOW, **kwargs)


# --------------------------------------------------------------------------- #
# The gate: never report a confident zero we did not earn
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_unconfigured_ramp_codes_report_not_configured_not_zero_events(
    monkeypatch,
):
    """The 453 incident, in one assertion.

    An unknown indexCode returns 200/code=0/empty, exactly like an idle camera.
    So when no codes are configured this must say so, rather than answering
    "no events" as though it had looked.
    """
    monkeypatch.setattr(recovery.settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(recovery.settings, "HIK_RAMP_RESOURCE_IDS", "")
    with patch.object(recovery.hikcentral, "is_enabled", return_value=True):
        block = await recovery.find_dropped_gate_reads(observed_at=NOW)

    assert block["configured"] is False
    assert block["queried"] is False
    assert block["api_error"] == "hik_ramp_resource_ids_unset"
    assert block["events"] == 0
    assert block["dropped"] == []


@pytest.mark.asyncio
async def test_the_ramp_codes_default_to_empty():
    from app.config import Settings

    # Must stay empty until the probe confirms real codes. A guessed value
    # fails silently and looks healthy.
    assert Settings().HIK_RAMP_RESOURCE_IDS == ""


@pytest.mark.asyncio
async def test_mode_off_does_not_query(monkeypatch):
    monkeypatch.setattr(recovery.settings, "ENTRY_V2_MODE", "off")
    monkeypatch.setattr(recovery.settings, "HIK_RAMP_RESOURCE_IDS", "510")
    block = await recovery.find_dropped_gate_reads(observed_at=NOW)
    assert block["queried"] is False


# --------------------------------------------------------------------------- #
# The correlation
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_event_with_no_gate_read_near_it_is_a_dropped_read(configured):
    block = await _run([_event("e1", 0)], [])
    assert block["queried"] is True
    assert block["dropped"] == ["e1"]
    assert block["explained"] == []


@pytest.mark.asyncio
async def test_an_event_the_gate_did_read_is_not_a_dropped_read(configured):
    """The gate was not silent, so this is not the dropped-read case. WHICH car
    that read belongs to is not decided here — only that a read exists."""
    block = await _run([_event("e1", 0)], [_anpr(5)])
    assert block["dropped"] == []
    assert block["explained"] == ["e1"]


@pytest.mark.asyncio
async def test_a_distant_gate_read_does_not_explain_the_crossing(configured):
    block = await _run(
        [_event("e1", 0)], [_anpr(600)], anpr_presence_seconds=90
    )
    assert block["dropped"] == ["e1"]


@pytest.mark.asyncio
async def test_several_cars_in_the_window_are_each_assessed(configured):
    """Cars entering one after another. Two were named at the gate, one was
    not — and picking which is which is a presence test, never an identity
    one."""
    events = [_event("e1", -40), _event("e2", 0), _event("e3", 40)]
    block = await _run(events, [_anpr(-38), _anpr(42)], anpr_presence_seconds=10)

    assert block["dropped"] == ["e2"]
    assert sorted(block["explained"]) == ["e1", "e3"]


@pytest.mark.asyncio
async def test_an_event_without_an_image_is_recorded_as_unactionable(configured):
    """A dropped read we cannot act on: with no image there is nothing for
    Re-ID to associate, so it can never become a witness. Worth recording — it
    is evidence the recovery path is being starved."""
    block = await _run([_event("e1", 0, image=None)], [])
    assert block["no_image"] == ["e1"]
    assert block["dropped"] == []


@pytest.mark.asyncio
async def test_an_event_without_a_time_cannot_be_called_dropped(configured):
    timeless = SimpleNamespace(
        event_index_code="e1",
        src_index="510",
        start_time=None,
        event_pic_uri="pic://x",
        event_type="131585",
    )
    block = await _run([timeless], [])
    assert block["explained"] == ["e1"]
    assert block["dropped"] == []


# --------------------------------------------------------------------------- #
# Failure and consumption
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_api_failure_is_a_degraded_query(configured):
    with patch.object(recovery.hikcentral, "is_enabled", return_value=True), \
         patch.object(
             recovery.hik_client, "list_camera_events",
             AsyncMock(side_effect=RuntimeError("platform down")),
         ):
        block = await recovery.find_dropped_gate_reads(observed_at=NOW)
    assert "platform down" in block["api_error"]
    assert block["dropped"] == []


@pytest.mark.asyncio
async def test_recovery_consumes_no_guid_and_takes_no_db_session():
    import inspect

    signature = inspect.signature(recovery.find_dropped_gate_reads)
    assert "db" not in signature.parameters

    body = inspect.getsource(recovery).split('"""', 2)[2]
    for forbidden in (
        "record_hik_validation(",
        "consume_already_logged(",
        "consume_refused_entry(",
        "HikValidation(",
        "db.commit",
    ):
        assert forbidden not in body


@pytest.mark.asyncio
async def test_only_the_named_events_have_their_images_fetched(configured):
    events = [_event("e1", 0), _event("e2", 10)]
    with patch.object(
        recovery.hik_client, "download_picture", AsyncMock(return_value=b"jpg")
    ) as download:
        images = await recovery.recover_images_for(["e1"], events)

    assert set(images) == {"e1"}
    assert download.await_count == 1


# --------------------------------------------------------------------------- #
# Keeping the refusal tombstone honest
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_tombstone_scope_flag_defaults_to_todays_behaviour():
    """The fix must not switch itself on.

    Scoping the tombstone changes the LEGACY AUTHORITATIVE path, and nothing in
    this work may alter production behaviour before the shadow review passes.
    Turning it on tombstones strictly less, which is the safe direction.
    """
    from app.config import Settings

    assert Settings().HIK_TOMBSTONE_REQUIRE_PLATE_MATCH is False


@pytest.mark.asyncio
async def test_scoped_tombstone_refuses_to_consume_another_cars_pass(monkeypatch):
    """A refusal of plate X is not a licence to suppress plate Y.

    The gate refused ABC-1234 and HikCentral holds no record for it — only a
    pass by XYZ-9999, a different car that really did enter. Consuming that
    GUID would mean the reconciler could never recover it: a lost entry caused
    by suppressing the wrong record.
    """
    from app.services.hikcentral import validation

    monkeypatch.setattr(
        validation.settings, "HIK_TOMBSTONE_REQUIRE_PLATE_MATCH", True
    )
    other_car = SimpleNamespace(
        guid="g-other",
        plate_license="XYZ-9999",
        canonical_plate="XYZ-9999",
        pass_time=NOW + timedelta(seconds=2),
        resource_id="447",
        resource_name="ANPR",
    )
    db = SimpleNamespace(add=lambda row: pytest.fail("must not tombstone"))

    with patch.object(validation, "_enabled", return_value=True), \
         patch.object(validation, "_lookup", AsyncMock(return_value=[other_car])), \
         patch.object(validation, "_within_skew", return_value=True), \
         patch.object(validation, "guid_already_used", return_value=False):
        result = await validation.consume_refused_entry(db, "ABC-1234", NOW)

    assert result is None


@pytest.mark.asyncio
async def test_scoped_tombstone_still_consumes_the_refused_cars_own_pass(monkeypatch):
    """The tombstone's real job is untouched: the pass the gate actually
    refused is still consumed, so the reconciler cannot re-open it."""
    from app.services.hikcentral import validation

    monkeypatch.setattr(
        validation.settings, "HIK_TOMBSTONE_REQUIRE_PLATE_MATCH", True
    )
    refused = SimpleNamespace(
        guid="g-refused",
        plate_license="ABC-1234",
        canonical_plate="ABC-1234",
        pass_time=NOW + timedelta(seconds=2),
        resource_id="447",
        resource_name="ANPR",
    )
    added = []
    db = SimpleNamespace(add=added.append)

    # Patch on the CLASS: Settings is a pydantic model and will not accept a
    # new attribute on the instance.
    monkeypatch.setattr(
        type(validation.settings),
        "hik_entry_resource_ids",
        lambda self: "447",
        raising=False,
    )
    with patch.object(validation, "_enabled", return_value=True), \
         patch.object(validation, "_lookup", AsyncMock(return_value=[refused])), \
         patch.object(validation, "_within_skew", return_value=True), \
         patch.object(validation, "guid_already_used", return_value=False):
        result = await validation.consume_refused_entry(db, "ABC-1234", NOW)

    assert result == "g-refused"
    assert len(added) == 1
