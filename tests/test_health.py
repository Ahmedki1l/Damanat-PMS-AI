"""Health endpoint diagnostics for process-local Entry V2 shadow delivery."""

from unittest.mock import MagicMock, patch

from app.routers.health import health_check
from app.schemas.responses import HealthResponse


def test_health_exposes_entry_v2_shadow_delivery_status():
    shadow_status = {
        "mode": "shadow",
        "accepting": True,
        "worker_alive": True,
        "queue_depth": 2,
        "queue_capacity": 8,
        "inflight": 1,
        "enqueued": 10,
        "completed": 6,
        "failed": 1,
        "dropped": 1,
    }
    db = MagicMock()

    with patch(
        "app.routers.health.entry_v2_shadow_status",
        return_value=shadow_status,
    ):
        result = health_check(db)

    assert result["entry_v2_shadow"] == shadow_status
    validated = HealthResponse.model_validate(result)
    assert validated.entry_v2_shadow.queue_depth == 2
    assert validated.entry_v2_shadow.dropped == 1
