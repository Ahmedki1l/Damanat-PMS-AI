# tests/test_alert_notification_suppression.py
"""Notification suppression for noisy alert types (SUPPRESSED_ALERT_NOTIFICATION_TYPES).

Suppression is notification-only by design: `create_alert` still writes the row
and still logs, and GET /alerts still serves it — only the SSE publish is
skipped. These tests pin that asymmetry, because the obvious "fix" for a noisy
alert (stop raising it) would destroy the record too.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
import pytest
from unittest.mock import patch

from app.config import settings
from app.services.alert_service import broadcast_event


@pytest.fixture
def published():
    """Capture the alert_type of everything that reaches the SSE bus."""
    seen = []
    with patch(
        "app.services.alert_service.event_bus.publish",
        side_effect=lambda payload: seen.append(json.loads(payload)["alert_type"]),
    ):
        yield seen


async def _broadcast(alert_type, is_alert=True):
    await broadcast_event(
        is_alert=is_alert,
        severity="warning",
        event_type="linedetection",
        alert_type=alert_type,
        description=f"test {alert_type}",
        camera_id="CAM-23",
    )


@pytest.mark.asyncio
async def test_silent_entry_is_not_pushed_to_the_stream(published):
    await _broadcast("silent_entry")
    assert published == []


@pytest.mark.asyncio
async def test_other_alert_types_still_reach_the_stream(published):
    await _broadcast("vehicle_intrusion")
    await _broadcast("capacity_exceeded")
    assert published == ["vehicle_intrusion", "capacity_exceeded"]


@pytest.mark.asyncio
async def test_suppression_keys_on_event_type_when_alert_type_is_absent(published):
    # broadcast_event falls back to event_type for alert_type, so a caller that
    # omits alert_type must still be suppressible.
    await broadcast_event(
        is_alert=True,
        severity="warning",
        event_type="silent_entry",
        description="no explicit alert_type",
        camera_id="CAM-23",
    )
    assert published == []


@pytest.mark.asyncio
async def test_empty_setting_disables_suppression(published):
    with patch.object(settings, "SUPPRESSED_ALERT_NOTIFICATION_TYPES", ""):
        await _broadcast("silent_entry")
    assert published == ["silent_entry"]


@pytest.mark.asyncio
async def test_suppressed_alert_is_still_written_to_the_db(published):
    """The whole point: silenced on screen, still on the record."""
    from unittest.mock import MagicMock
    from app.services.alert_service import create_alert

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    await create_alert(
        db=db,
        alert_type="silent_entry",
        camera_id="CAM-23",
        zone_id="entry",
        event_type="linedetection",
        description="Vehicle crossed entry ramp with no plate read (ANPR miss)",
    )

    assert db.add.called, "suppressed alert must still be persisted"
    assert db.add.call_args[0][0].alert_type == "silent_entry"
    assert published == [], "…but must not be pushed to connected dashboards"


def test_setting_parses_into_a_trimmed_set():
    with patch.object(
        settings, "SUPPRESSED_ALERT_NOTIFICATION_TYPES", " silent_entry , foo ,, "
    ):
        assert settings.suppressed_alert_notification_types() == {"silent_entry", "foo"}


# ── Full disable (DISABLED_ALERT_TYPES): no DB row, no stream ────────────────


@pytest.mark.asyncio
async def test_disabled_alert_is_not_written_and_not_streamed(published):
    """The stronger switch: a disabled type is dropped before anything writes."""
    from unittest.mock import MagicMock
    from app.services.alert_service import create_alert

    db = MagicMock()
    with patch.object(settings, "DISABLED_ALERT_TYPES", "silent_entry"):
        result = await create_alert(
            db=db, alert_type="silent_entry", camera_id="CAM-23",
            zone_id="entry", event_type="linedetection", description="x",
        )

    assert result is None
    assert not db.add.called, "a disabled alert must NOT be persisted"
    assert published == []


@pytest.mark.asyncio
async def test_disable_list_only_affects_listed_types(published):
    """Disabling silent_entry must not affect other alert types."""
    from unittest.mock import MagicMock
    from app.services.alert_service import create_alert

    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    with patch.object(settings, "DISABLED_ALERT_TYPES", "silent_entry"):
        await create_alert(
            db=db, alert_type="vehicle_intrusion", camera_id="CAM-01",
            zone_id="restricted-vip", event_type="linedetection", description="x",
        )

    assert db.add.called, "a non-disabled alert is unaffected and still written"


def test_disabled_setting_parses_into_a_trimmed_set():
    with patch.object(settings, "DISABLED_ALERT_TYPES", " silent_entry , foo ,, "):
        assert settings.disabled_alert_types() == {"silent_entry", "foo"}
