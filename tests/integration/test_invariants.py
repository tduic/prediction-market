"""
Tests for core/invariants.py

Covers:
  - Individual invariant checks: PnL sanity, position duration, orphan positions,
    fee ratio, engine state consistency
  - check_all_invariants orchestrator: warn-only vs halt mode, Discord alert
    forwarding, DB persistence
  - ScheduledStrategyRunner.run_one_cycle alert_manager forwarding
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.invariants import (  # noqa: E402
    InvariantResult,
    InvariantViolation,
    check_all_invariants,
    check_engine_state,
    check_fee_ratio,
    check_orphan_positions,
    check_pnl_sanity,
    check_position_duration,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _now():
    return datetime.now(timezone.utc).isoformat()


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


async def _seed_signal(db, signal_id: str, market_id: str = "m1") -> None:
    await db.execute(
        """INSERT OR IGNORE INTO markets
           (id, platform, platform_id, title, status, created_at, updated_at)
           VALUES (?, 'polymarket', ?, 'Test market', 'open', ?, ?)""",
        (market_id, market_id, _now(), _now()),
    )
    await db.execute(
        """INSERT INTO signals
           (id, violation_id, strategy, signal_type, market_id_a,
            model_edge, kelly_fraction, position_size_a,
            total_capital_at_risk, status, fired_at, updated_at)
           VALUES (?, NULL, 'P3_calibration_bias', 'single', ?,
                   0.05, 0.25, 50.0, 50.0, 'fired', ?, ?)""",
        (signal_id, market_id, _now(), _now()),
    )


async def _seed_closed_position(
    db,
    *,
    signal_id: str,
    market_id: str = "m1",
    entry_price: float = 0.50,
    entry_size: float = 50.0,
    exit_price: float = 0.55,
    realized_pnl: float = 2.5,
    fees_paid: float = 1.0,
    held_seconds: float = 300.0,
    pnl_model: str = "realistic",
) -> str:
    pos_id = f"pos_{uuid.uuid4().hex[:8]}"
    opened = _ago(held_seconds)
    closed = _now()
    await db.execute(
        """INSERT INTO positions
           (id, signal_id, market_id, strategy, side, entry_price, entry_size,
            exit_price, realized_pnl, fees_paid, status,
            opened_at, closed_at, updated_at, pnl_model)
           VALUES (?, ?, ?, 'P3_calibration_bias', 'BUY', ?, ?, ?, ?, ?, 'closed',
                   ?, ?, ?, ?)""",
        (
            pos_id,
            signal_id,
            market_id,
            entry_price,
            entry_size,
            exit_price,
            realized_pnl,
            fees_paid,
            opened,
            closed,
            _now(),
            pnl_model,
        ),
    )
    await db.commit()
    return pos_id


# ── PnL sanity ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPnLSanity:
    async def test_empty_db_passes(self, db):
        result = await check_pnl_sanity(db)
        assert result.passed

    async def test_normal_pnl_passes(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(
            db, signal_id="sig1", realized_pnl=2.5, entry_size=50.0
        )
        result = await check_pnl_sanity(db)
        assert result.passed

    async def test_inflated_pnl_fails(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(
            db, signal_id="sig1", realized_pnl=999.0, entry_size=50.0
        )
        result = await check_pnl_sanity(db)
        assert not result.passed

    async def test_pnl_exactly_at_bound_passes(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(
            db, signal_id="sig1", realized_pnl=50.0, entry_size=50.0
        )
        result = await check_pnl_sanity(db)
        assert result.passed

    async def test_result_names_offending_position(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(
            db, signal_id="sig1", realized_pnl=500.0, entry_size=50.0
        )
        result = await check_pnl_sanity(db)
        assert not result.passed
        assert "realized_pnl" in result.message.lower() or result.name


# ── Position duration ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPositionDuration:
    async def test_empty_db_passes(self, db):
        result = await check_position_duration(db)
        assert result.passed

    async def test_adequate_duration_passes(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", held_seconds=60.0)
        result = await check_position_duration(db)
        assert result.passed

    async def test_zero_duration_fails(self, db):
        await _seed_signal(db, "sig1")
        now = _now()
        pos_id = f"pos_{uuid.uuid4().hex[:8]}"
        await db.execute(
            """INSERT INTO positions
               (id, signal_id, market_id, strategy, side, entry_price, entry_size,
                exit_price, realized_pnl, fees_paid, status,
                opened_at, closed_at, updated_at, pnl_model)
               VALUES (?, 'sig1', 'm1', 'P3_calibration_bias', 'BUY',
                       0.50, 50.0, 0.55, 2.5, 1.0, 'closed', ?, ?, ?, 'realistic')""",
            (pos_id, now, now, now),
        )
        await db.commit()
        result = await check_position_duration(db)
        assert not result.passed

    async def test_open_positions_excluded(self, db):
        await _seed_signal(db, "sig1")
        pos_id = f"pos_{uuid.uuid4().hex[:8]}"
        await db.execute(
            """INSERT INTO positions
               (id, signal_id, market_id, strategy, side, entry_price, entry_size,
                status, opened_at, updated_at, pnl_model)
               VALUES (?, 'sig1', 'm1', 'P3_calibration_bias', 'BUY',
                       0.50, 50.0, 'open', ?, ?, 'realistic')""",
            (pos_id, _now(), _now()),
        )
        await db.commit()
        result = await check_position_duration(db)
        assert result.passed


# ── Orphan positions ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestOrphanPositions:
    async def test_empty_db_passes(self, db):
        result = await check_orphan_positions(db)
        assert result.passed

    async def test_position_with_signal_passes(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1")
        result = await check_orphan_positions(db)
        assert result.passed

    async def test_orphan_reported_as_failure(self, db):
        """Orphan position (no matching signal) is caught by the soft check.

        Strategy: insert a valid position, then delete the signal with FK
        enforcement off so the cascade doesn't fire.
        """
        await _seed_signal(db, "sig_orphan_test")
        await _seed_closed_position(db, signal_id="sig_orphan_test")
        await db.execute("PRAGMA foreign_keys = OFF")
        await db.execute("DELETE FROM signals WHERE id = 'sig_orphan_test'")
        await db.commit()
        result = await check_orphan_positions(db)
        assert not result.passed


# ── Fee ratio ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestFeeRatio:
    async def test_empty_db_passes(self, db):
        result = await check_fee_ratio(db)
        assert result.passed

    async def test_normal_fees_pass(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(
            db, signal_id="sig1", fees_paid=1.0, entry_size=50.0, entry_price=0.50
        )
        result = await check_fee_ratio(db)
        assert result.passed

    async def test_zero_fees_pass(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", fees_paid=0.0)
        result = await check_fee_ratio(db)
        assert result.passed

    async def test_excessive_fees_fail(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(
            db, signal_id="sig1", fees_paid=20.0, entry_size=50.0, entry_price=0.50
        )
        result = await check_fee_ratio(db)
        assert not result.passed


# ── Engine state consistency ──────────────────────────────────────────────────


class TestEngineState:
    def test_no_engine_passes(self):
        result = check_engine_state(arb_engine=None)
        assert result.passed

    def test_empty_engine_passes(self):
        engine = MagicMock()
        engine.fired_state = {}
        engine._pairs = {}
        result = check_engine_state(arb_engine=engine)
        assert result.passed

    def test_fired_state_within_pairs_passes(self):
        engine = MagicMock()
        engine.fired_state = {"pair1": object(), "pair2": object()}
        engine._pairs = {"pair1": None, "pair2": None, "pair3": None}
        result = check_engine_state(arb_engine=engine)
        assert result.passed

    def test_fired_state_exceeds_pairs_fails(self):
        engine = MagicMock()
        engine.fired_state = {"pair1": object(), "ghost": object()}
        engine._pairs = {"pair1": None}
        result = check_engine_state(arb_engine=engine)
        assert not result.passed


# ── check_all_invariants orchestrator ────────────────────────────────────────


@pytest.mark.asyncio
class TestCheckAllInvariants:
    async def test_empty_db_all_pass(self, db):
        results = await check_all_invariants(db)
        assert all(r.passed for r in results)

    async def test_returns_list_of_invariant_results(self, db):
        results = await check_all_invariants(db)
        assert isinstance(results, list)
        assert all(isinstance(r, InvariantResult) for r in results)

    async def test_warn_mode_does_not_raise_on_violation(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", realized_pnl=9999.0)
        results = await check_all_invariants(db, mode="warn")
        assert any(not r.passed for r in results)

    async def test_halt_mode_raises_on_violation(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", realized_pnl=9999.0)
        with pytest.raises(InvariantViolation):
            await check_all_invariants(db, mode="halt")

    async def test_halt_mode_passes_on_clean_db(self, db):
        await check_all_invariants(db, mode="halt")

    async def test_violation_sends_discord_alert(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", realized_pnl=9999.0)
        mock_mgr = AsyncMock()
        results = await check_all_invariants(db, alert_manager=mock_mgr, mode="warn")
        assert any(not r.passed for r in results)
        mock_mgr.send.assert_called()

    async def test_no_alert_when_all_pass(self, db):
        mock_mgr = AsyncMock()
        await check_all_invariants(db, alert_manager=mock_mgr, mode="warn")
        mock_mgr.send.assert_not_called()

    async def test_violation_persisted_to_db(self, db):
        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", realized_pnl=9999.0)
        await check_all_invariants(db, mode="warn")
        cursor = await db.execute("SELECT COUNT(*) FROM invariant_violations")
        row = await cursor.fetchone()
        assert row[0] >= 1

    async def test_clean_run_writes_no_violations(self, db):
        await check_all_invariants(db, mode="warn")
        cursor = await db.execute("SELECT COUNT(*) FROM invariant_violations")
        row = await cursor.fetchone()
        assert row[0] == 0


# ── ScheduledStrategyRunner: alert_manager forwarding ────────────────────────


@pytest.mark.asyncio
class TestRunOneCycleAlertManager:
    async def test_alert_manager_forwarded_on_invariant_violation(self, db):
        """When runner has an alert_manager and an invariant fails, send() is called."""
        from core.engine import ScheduledStrategyRunner

        await _seed_signal(db, "sig1")
        await _seed_closed_position(db, signal_id="sig1", realized_pnl=9999.0)

        mock_mgr = AsyncMock()
        runner = ScheduledStrategyRunner(db, alert_manager=mock_mgr)
        await runner.run_one_cycle()

        mock_mgr.send.assert_called()

    async def test_no_error_when_alert_manager_is_none(self, db):
        """run_one_cycle works without alert_manager (backward compatibility)."""
        from core.engine import ScheduledStrategyRunner

        runner = ScheduledStrategyRunner(db)
        # Should not raise
        await runner.run_one_cycle()

    async def test_no_alert_on_clean_db(self, db):
        """alert_manager.send() not called when all invariants pass."""
        from core.engine import ScheduledStrategyRunner

        mock_mgr = AsyncMock()
        runner = ScheduledStrategyRunner(db, alert_manager=mock_mgr)
        await runner.run_one_cycle()

        mock_mgr.send.assert_not_called()
