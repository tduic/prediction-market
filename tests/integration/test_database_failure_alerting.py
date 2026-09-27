"""Material database failures alert without exposing database contents."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.config import RiskControlConfig
from core.engine import ScheduledStrategyRunner


async def _noop(*args, **kwargs):
    return []


async def _db_failure(*args, **kwargs):
    raise RuntimeError("database unavailable")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "component"),
    [
        ("core.engine.resolution.close_resolved_positions", "resolution"),
        ("core.strategies.single_platform.mark_and_close_positions", "position_lifecycle"),
        ("core.engine.reconciliation.reconcile_internal_state", "reconciliation"),
    ],
)
async def test_material_scheduler_database_failures_alert(target, component, db, monkeypatch):
    """Each scheduler-owned DB pass invokes the sanitized critical notifier."""
    monkeypatch.setattr("core.engine.resolution.close_resolved_positions", _noop)
    monkeypatch.setattr("core.strategies.single_platform.mark_and_close_positions", _noop)
    monkeypatch.setattr("core.engine.reconciliation.reconcile_internal_state", _noop)
    monkeypatch.setattr("core.invariants.check_all_invariants", _noop)
    monkeypatch.setattr(
        "core.strategies.single_platform.detect_single_platform_opportunities", _noop
    )
    monkeypatch.setattr(target, _db_failure)
    notifier = MagicMock()
    monkeypatch.setattr("core.alerting.notify_database_failure", notifier)

    runner = ScheduledStrategyRunner(
        db, risk_config=RiskControlConfig(reconcile_every=1)
    )
    assert await runner.run_one_cycle() == []
    notifier.assert_called_once()
    assert notifier.call_args.args[0] == component
    assert isinstance(notifier.call_args.args[1], RuntimeError)
