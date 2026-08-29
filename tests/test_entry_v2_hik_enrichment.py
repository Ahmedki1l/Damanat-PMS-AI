"""Entry V2 stage S2 — WE query HikCentral after an ANPR read.

The direction of communication is the thing under test. HikCentral pushes
nothing into this pipeline and never triggers it: our service holds a gate
event, calls the API, and decides what the answer means.

Two properties matter more than the rest and each has its own test:

  * Nothing is CONSUMED. `HikValidation.guid` doubles as the reconciliation
    watermark, so spending a GUID here would advance it and make the legacy
    authoritative reconciler skip real records.
  * Nothing is SELECTED by timestamp. Every record in the window is a
    candidate; Re-ID over the images decides which one is this car.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import entry_v2_hik_enrichment as enrichment


NOW = datetime(2026, 8, 29, 12, 0, 0)


def _record(guid, seconds_offset, plate="ABC-1234", image="pic://x"):
    return SimpleNamespace(
        guid=guid,
        plate=plate,
        pass_time=NOW + timedelta(seconds=seconds_offset),
        vehicle_image_url=image,
    )


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(enrichment.settings, "ENTRY_V2_MODE", "shadow")
    monkeypatch.setattr(enrichment.settings, "PMS_API_URL", "http://va:8000")
    monkeypatch.setattr(enrichment.settings, "ENTRY_V2_SERVICE_KEY", "k" * 8)


def _ack(status=201, attempt_id="", mode="shadow"):
    return SimpleNamespace(
        status_code=status,
        json=lambda: {
            "mode": mode,
            "attempt_id": attempt_id,
            "status": "accepted",
            "duplicate": False,
        },
    )


async def _run(records, *, images=b"jpeg-bytes", post=None, max_candidates=5):
    with patch.object(enrichment.hikcentral, "is_enabled", return_value=True), \
         patch.object(
             enrichment.hikcentral, "list_entry_candidates",
             AsyncMock(return_value=records),
         ), \
         patch.object(
             enrichment.hik_client, "download_picture",
             AsyncMock(return_value=images),
         ), \
         patch.object(enrichment, "_post_entry_v2", post or AsyncMock()) as posted:
        block = await enrichment.enrich_entry_from_hikcentral(
            plate="ABC-1234",
            event_time=NOW,
            camera_id="CAM-ENTRY",
            max_candidates=max_candidates,
        )
    return block, posted


# --------------------------------------------------------------------------- #
# The direction: our event, our query
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_trigger_is_our_own_event(enabled):
    block, _ = await _run([_record("g1", 2)], post=AsyncMock(return_value=_ack()))
    assert block["trigger"] == "anpr_identity"
    assert block["queried"] is True
    # The plate is the anchor we queried AROUND, not a filter we queried WITH.
    assert block["anchor_plate"] == "ABC-1234"


@pytest.mark.asyncio
async def test_not_calling_is_recorded_as_our_choice(monkeypatch):
    monkeypatch.setattr(enrichment.settings, "ENTRY_V2_MODE", "off")
    block = await enrichment.enrich_entry_from_hikcentral(
        plate="ABC-1234", event_time=NOW, camera_id="CAM-ENTRY"
    )
    # queried:false means WE did not call, never that HikCentral stayed silent.
    assert block["queried"] is False
    assert block["records"] == 0


# --------------------------------------------------------------------------- #
# Every record is a candidate; none is selected by time
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_every_record_in_the_window_is_forwarded_as_a_candidate(enabled):
    """Three cars passed in the window. All three go to VA, because deciding
    which is ours is Re-ID's job — not the job of whichever PassTime happens to
    sit closest to the gate read."""
    records = [_record("g1", 1), _record("g2", 4), _record("g3", 9)]
    block, posted = await _run(records, post=AsyncMock(return_value=_ack()))

    assert block["records"] == 3
    assert block["images"] == 3
    assert posted.await_count == 3


@pytest.mark.asyncio
async def test_the_candidate_cap_bounds_work_without_choosing_between_cars(enabled):
    records = [_record(f"g{i}", i) for i in range(1, 8)]
    block, posted = await _run(
        records, post=AsyncMock(return_value=_ack()), max_candidates=2
    )
    assert block["records"] == 7          # all of them were considered
    assert posted.await_count == 2        # only the work was bounded


@pytest.mark.asyncio
async def test_a_forwarded_candidate_is_marked_as_hikcentral_sourced(enabled):
    post = AsyncMock(return_value=_ack())
    await _run([_record("g1", 2)], post=post)

    data = post.await_args.kwargs["data"]
    metadata = data["metadata_json"]
    # The marker is what stops HikCentral's reading being counted as the gate's.
    assert '"evidence_source":"hikcentral"' in metadata
    assert '"hik_guid":"g1"' in metadata
    assert data["reported_plate"] == "ABC-1234"


# --------------------------------------------------------------------------- #
# Consumption: none
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_nothing_is_consumed_and_no_db_session_is_touched(enabled):
    """The watermark leak, closed by construction.

    HikValidation.guid doubles as the reconciliation cursor. This path takes no
    db session at all, so it cannot write one and cannot move that cursor.
    """
    import inspect

    signature = inspect.signature(enrichment.enrich_entry_from_hikcentral)
    assert "db" not in signature.parameters

    source = inspect.getsource(enrichment)
    body = source.split('"""', 2)[2]  # skip the module docstring, which
                                      # explains the leak and so names it
    for forbidden in (
        "record_hik_validation(",
        "consume_already_logged(",
        "consume_refused_entry(",
        "HikValidation(",
        "db.commit",
    ):
        assert forbidden not in body


@pytest.mark.asyncio
async def test_the_legacy_closest_selection_is_not_used(enabled):
    import inspect

    assert "_closest" not in inspect.getsource(enrichment)
    assert "validate_entry_plate" not in inspect.getsource(
        enrichment.enrich_entry_from_hikcentral
    )


# --------------------------------------------------------------------------- #
# Failure modes 18A-18D
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_an_api_failure_is_a_degraded_query_not_a_lost_entry(enabled):
    with patch.object(enrichment.hikcentral, "is_enabled", return_value=True), \
         patch.object(
             enrichment.hikcentral, "list_entry_candidates",
             AsyncMock(side_effect=RuntimeError("platform down")),
         ):
        block = await enrichment.enrich_entry_from_hikcentral(
            plate="ABC-1234", event_time=NOW, camera_id="CAM-ENTRY"
        )
    # 18A: there was no event of theirs to lose. Recorded and carried on.
    assert "platform down" in block["api_error"]
    assert block["records"] == 0


@pytest.mark.asyncio
async def test_no_records_is_not_an_error(enabled):
    block, posted = await _run([])
    assert block["queried"] is True
    assert block["records"] == 0
    assert block["api_error"] is None
    assert posted.await_count == 0


@pytest.mark.asyncio
async def test_a_record_without_an_image_is_noted_and_never_forwarded(enabled):
    """18C: its plate is still a source, but with no image there is nothing for
    Re-ID to associate, so it can never become a witness."""
    block, posted = await _run(
        [_record("g1", 2, image="")], post=AsyncMock(return_value=_ack())
    )
    assert block["no_image"] == ["g1"]
    assert block["images"] == 0
    assert posted.await_count == 0


@pytest.mark.asyncio
async def test_a_failed_download_degrades_to_no_image(enabled):
    with patch.object(enrichment.hikcentral, "is_enabled", return_value=True), \
         patch.object(
             enrichment.hikcentral, "list_entry_candidates",
             AsyncMock(return_value=[_record("g1", 2)]),
         ), \
         patch.object(
             enrichment.hik_client, "download_picture",
             AsyncMock(return_value=None),
         ):
        block = await enrichment.enrich_entry_from_hikcentral(
            plate="ABC-1234", event_time=NOW, camera_id="CAM-ENTRY"
        )
    assert block["no_image"] == ["g1"]


@pytest.mark.asyncio
async def test_a_rejected_forward_is_recorded_not_raised(enabled):
    block, _ = await _run(
        [_record("g1", 2)], post=AsyncMock(return_value=_ack(status=422))
    )
    assert block["images"] == 1
    assert block["forwarded"] == []


@pytest.mark.asyncio
async def test_hikcentral_disabled_short_circuits(monkeypatch):
    monkeypatch.setattr(enrichment.settings, "ENTRY_V2_MODE", "shadow")
    with patch.object(enrichment.hikcentral, "is_enabled", return_value=False):
        block = await enrichment.enrich_entry_from_hikcentral(
            plate="ABC-1234", event_time=NOW, camera_id="CAM-ENTRY"
        )
    assert block["queried"] is False
    assert block["api_error"] == "hikcentral_disabled"
