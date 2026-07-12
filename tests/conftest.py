"""Global test guards.

Tests must never touch live infrastructure. Two leaks made this necessary:

  * `test_parking_stats` drives the real exit path, which calls the real
    `notify_pms_anpr`. With `PMS_API_URL` set (it is, in .env), that POSTs to the
    production VA/PMS API and — when it can't reach it — spools the payload to the
    REAL ./pms_forward_spool directory. Those files then sit in the live queue and
    the backend's drain loop POSTs the fake plates (TEST-123, NO-ENTRY) to
    production on its next start. This actually happened on 2026-07-12.

  * It also made the suite wait on real 10s HTTP timeouts.

The fixture is autouse, so it protects every test — including ones added later
that don't know the spool exists. Tests that need forwarding behaviour (e.g.
test_pms_forward_reliability) patch their own settings object and are unaffected.
"""

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def isolate_external_side_effects(tmp_path, monkeypatch):
    """Point the forward spool at a temp dir and disable outbound pushes."""
    monkeypatch.setattr(
        settings, "PMS_FORWARD_SPOOL_DIR", str(tmp_path / "pms_forward_spool")
    )
    monkeypatch.setattr(settings, "PMS_API_URL", "", raising=False)
    monkeypatch.setattr(settings, "NODEBACK_URL", "", raising=False)
