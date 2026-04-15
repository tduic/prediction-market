"""
Tests for execution.circuit_breaker.DailyLossCircuitBreaker.

Covers:
  - Clean state on a fresh DB
  - Trips when daily loss >= limit
  - Remains tripped (sticky) across subsequent should_halt() calls
  - Consecutive-failure path
  - Reset clears the tripped state
  - load_state restores the trip from a system_events row written today
  - Day rollover auto-resets the breaker
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # noqa: E402

from execution.circuit_breaker import DailyLossCircuitBreaker  # noqa: E402


async def _seed_loss(db, amount: float, days_ago: int = 0) -> None:
    """Insert a trade_outcomes row with a negative pnl."""
    # Use foreign_keys OFF to avoid needing parent markets/signals rows.
    await db.execute("PRAGMA foreign_keys = OFF")
    now = datetime.now(timezone.utc)
    if days_ago == 0:
        created_at = now.isoformat()
    else:
        # Back-date by N days — SQLite DATE() sees the date portion.
        created_at = now.replace(microsecond=0).isoformat()
        created_at = f"{(now.date().toordinal() - days_ago)}"  # unused, see below
        # Simpler: direct date math
        import datetime as _dt

        back = now - _dt.timedelta(days=days_ago)
        created_at = back.isoformat()

    await db.execute(
        """
        INSERT INTO trade_outcomes
        (id, signal_id, strategy, market_id_a, actual_pnl, fees_total,
         resolved_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"out_{created_at}_{amount}",
            "sig_test",
            "test_strategy",
            "mkt_a",
            -abs(amount),
            0.0,
            created_at,
            created_at,
        ),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_clean_state_does_not_halt(db):
    breaker = DailyLossCircuitBreaker(
        db=db, starting_capital=10_000, max_daily_loss_pct=0.02
    )
    assert await breaker.should_halt() is False
    state = await breaker.get_state()
    assert state.tripped is False
    assert state.daily_loss == 0.0
    assert state.daily_loss_limit == 200.0


@pytest.mark.asyncio
async def test_trips_when_daily_loss_exceeds_limit(db):
    # 2% of $10k = $200 limit
    breaker = DailyLossCircuitBreaker(
        db=db, starting_capital=10_000, max_daily_loss_pct=0.02
    )

    await _seed_loss(db, 250.0)  # exceeds $200 limit

    halted = await breaker.should_halt()
    assert halted is True

    state = await breaker.get_state()
    assert state.tripped is True
    assert state.daily_loss == 250.0
    assert "Daily loss" in (state.reason or "")
    assert state.tripped_at is not None


@pytest.mark.asyncio
async def test_loss_below_limit_does_not_trip(db):
    breaker = DailyLossCircuitBreaker(
        db=db, starting_capital=10_000, max_daily_loss_pct=0.02
    )
    await _seed_loss(db, 50.0)  # $50 < $200 limit
    assert await breaker.should_halt() is False


@pytest.mark.asyncio
async def test_trip_is_sticky(db):
    """Once tripped, subsequent calls return True even if loss decreases."""
    breaker = DailyLossCircuitBreaker(
        db=db, starting_capital=10_000, max_daily_loss_pct=0.02
    )
    await _seed_loss(db, 300.0)
    assert await breaker.should_halt() is True

    # Delete the loss row — breaker should still report halted.
    await db.execute("DELETE FROM trade_outcomes")
    await db.commit()

    assert await breaker.should_halt() is True


@pytest.mark.asyncio
async def test_reset_clears_trip(db):
    breaker = DailyLossCircuitBreaker(
        db=db, starting_capital=10_000, max_daily_loss_pct=0.02
    )
    await _seed_loss(db, 300.0)
    await breaker.should_halt()
    assert (await breaker.get_state()).tripped

    # Clear the underlying loss AND reset so should_halt doesn't re-trip.
    await db.execute("DELETE FROM trade_outcomes")
    await db.commit()
    await breaker.reset("operator cleared for test")

    assert await breaker.should_halt() is False
    assert (await breaker.get_state()).tripped is False


@pytest.mark.asyncio
async def test_consecutive_failures_trip(db):
    breaker = DailyLossCircuitBreaker(
        db=db,
        starting_capital=10_000,
        max_daily_loss_pct=0.02,
        consecutive_failure_limit=3,
    )

    await breaker.record_order_result(success=False)
    await breaker.record_order_result(success=False)
    assert (await breaker.get_state()).tripped is False
    assert (await breaker.get_state()).consecutive_failures == 2

    await breaker.record_order_result(success=False)  # 3rd failure

    state = await breaker.get_state()
    assert state.tripped is True
    assert "consecutive" in (state.reason or "").lower()


@pytest.mark.asyncio
async def test_success_resets_consecutive_failures(db):
    breaker = DailyLossCircuitBreaker(
        db=db,
        starting_capital=10_000,
        max_daily_loss_pct=0.02,
        consecutive_failure_limit=3,
    )
    await breaker.record_order_result(success=False)
    await breaker.record_order_result(success=False)
    await breaker.record_order_result(success=True)  # reset!

    state = await breaker.get_state()
    assert state.consecutive_failures == 0
    assert state.tripped is False


@pytest.mark.asyncio
async def test_load_state_restores_today_trip(db):
    """A trip logged earlier today should be restored on load_state()."""
    # Write a trip event directly to system_events with today's date.
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO system_events
        (event_type, severity, component, detail, context, occurred_at)
        VALUES ('CIRCUIT_BREAKER_TRIPPED', 'critical', 'circuit_breaker',
                'prior trip detail', NULL, ?)
        """,
        (now,),
    )
    await db.commit()

    breaker = DailyLossCircuitBreaker(
        db=db, starting_capital=10_000, max_daily_loss_pct=0.02
    )
    await breaker.load_state()

    state = await breaker.get_state()
    assert state.tripped is True
    assert state.reason == "prior trip detail"


@pytest.mark.asyncio
async def test_load_state_ignores_yesterday_trip(db):
    """A trip from a previous day should NOT re-trip the breaker today."""
    # 2 days ago
    from datetime import timedelta

    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    await db.execute(
        """
        INSERT INTO system_events
        (event_type, severity, component, detail, context, occurred_at)
        VALUES ('CIRCUIT_BREAKER_TRIPPED', 'critical', 'circuit_breaker',
                'old trip', NULL, ?)
        """,
        (old,),
    )
    await db.commit()

    breaker = DailyLossCircuitBreaker(
        db=db, starting_capital=10_000, max_daily_loss_pct=0.02
    )
    await breaker.load_state()

    assert (await breaker.get_state()).tripped is False


@pytest.mark.asyncio
async def test_day_rollover_auto_resets(db):
    breaker = DailyLossCircuitBreaker(
        db=db, starting_capital=10_000, max_daily_loss_pct=0.02
    )
    await _seed_loss(db, 300.0)
    assert await breaker.should_halt() is True

    # Simulate rollover by mutating the internal UTC day marker and removing
    # today's losses (the breaker's auto-rollover should then clear state).
    breaker._utc_day = "1999-01-01"
    await db.execute("DELETE FROM trade_outcomes")
    await db.commit()

    assert await breaker.should_halt() is False
    assert (await breaker.get_state()).tripped is False


@pytest.mark.asyncio
async def test_trip_logged_to_system_events(db):
    breaker = DailyLossCircuitBreaker(
        db=db, starting_capital=10_000, max_daily_loss_pct=0.02
    )
    await _seed_loss(db, 500.0)
    await breaker.should_halt()

    cursor = await db.execute(
        "SELECT event_type, severity, component FROM system_events "
        "WHERE event_type = 'CIRCUIT_BREAKER_TRIPPED'"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "CIRCUIT_BREAKER_TRIPPED"
    assert rows[0][1] == "critical"
    assert rows[0][2] == "circuit_breaker"
