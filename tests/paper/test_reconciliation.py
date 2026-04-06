"""
Tests for execution/reconciliation.py — ReconciliationEngine.

Covers: reconcile_balances (OK/WARNING/UNREACHABLE/ERROR paths),
        reconcile_positions (counting open positions per platform),
        run_reconciliation_check (halt logic),
        _compute_local_balance, _estimate_portfolio_value,
        _log_reconciliation, _log_system_event.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.reconciliation import ReconciliationEngine  # noqa: E402

MIGRATIONS_DIR = PROJECT_ROOT / "core" / "storage" / "migrations"
NOW = datetime.now(timezone.utc).isoformat()


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = sql_file.read_text()
        clean_lines = []
        for line in sql.split("\n"):
            stripped = line.strip()
            if (
                stripped.upper().startswith("ALTER TABLE")
                and "ADD COLUMN" in stripped.upper()
            ):
                try:
                    await db.execute(stripped)
                except Exception as e:
                    if "duplicate column" not in str(e).lower():
                        raise
                clean_lines.append(f"-- (applied separately) {stripped}")
            else:
                clean_lines.append(line)
        await db.executescript("\n".join(clean_lines))
    await db.execute("PRAGMA foreign_keys = OFF")
    await db.commit()


@pytest_asyncio.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await _apply_migrations(conn)
    yield conn
    await conn.close()


def _make_mock_client(balance=1000.0):
    client = AsyncMock()
    client.get_balance = AsyncMock(return_value=balance)
    return client


@pytest_asyncio.fixture
async def engine(db):
    poly = _make_mock_client(balance=5000.0)
    kalshi = _make_mock_client(balance=5000.0)
    return ReconciliationEngine(
        db_connection=db,
        polymarket_client=poly,
        kalshi_client=kalshi,
        halt_threshold_pct=0.05,
        check_interval_s=60,
        starting_capital=10000.0,
    )


async def _insert_filled_order(
    db, order_id, platform, side, filled_size, filled_price, fee_paid=0.0
):
    """Insert a filled order using the real schema."""
    await db.execute(
        """INSERT INTO orders (id, signal_id, platform, market_id, side, order_type,
                               requested_price, requested_size, filled_price, filled_size,
                               fee_paid, status, submitted_at, updated_at)
           VALUES (?, 'sig1', ?, 'mkt1', ?, 'limit', ?, ?, ?, ?, ?, 'filled', ?, ?)""",
        (
            order_id,
            platform,
            side,
            filled_price,
            filled_size,
            filled_price,
            filled_size,
            fee_paid,
            NOW,
            NOW,
        ),
    )
    await db.commit()


async def _insert_position(
    db, pid, strategy, status="open", platform: str | None = None
):
    """Insert a position and a matching order so the platform join resolves correctly."""
    signal_id = f"sig_{pid}"
    await db.execute(
        """INSERT INTO positions (id, market_id, side, entry_price, entry_size,
                                  signal_id, strategy, status, opened_at, updated_at)
           VALUES (?, 'mkt1', 'BUY', 0.5, 10, ?, ?, ?, ?, ?)""",
        (pid, signal_id, strategy, status, NOW, NOW),
    )
    # Insert a matching order so _count_open_positions can resolve platform via the join.
    # Derive platform from the strategy name when not explicitly provided.
    if platform is None:
        if "polymarket" in strategy.lower():
            platform = "polymarket"
        elif "kalshi" in strategy.lower():
            platform = "kalshi"
        else:
            platform = "unknown"
    await db.execute(
        """INSERT INTO orders (id, signal_id, platform, market_id, side, order_type,
                               requested_price, requested_size, status, submitted_at, updated_at)
           VALUES (?, ?, ?, 'mkt1', 'buy', 'limit', 0.5, 10, 'filled', ?, ?)""",
        (f"ord_{pid}", signal_id, platform, NOW, NOW),
    )
    await db.commit()


# ── reconcile_balances ──────────────────────────────────────────────────


class TestReconcileBalances:
    @pytest.mark.asyncio
    async def test_zero_local_balance_ok(self, engine):
        report = await engine.reconcile_balances()
        assert "polymarket" in report
        assert report["polymarket"]["local"] == 0.0
        assert report["polymarket"]["exchange"] == 5000.0

    @pytest.mark.asyncio
    async def test_local_balance_from_buy_orders(self, db, engine):
        await _insert_filled_order(
            db, "ord1", "polymarket", "buy", 10, 0.50, fee_paid=0.10
        )
        report = await engine.reconcile_balances()
        # BUY 10 @ 0.50 = -5.0, fee -0.10 → -5.10
        assert abs(report["polymarket"]["local"] - (-5.10)) < 0.01

    @pytest.mark.asyncio
    async def test_sell_increases_local_balance(self, db, engine):
        await _insert_filled_order(
            db, "ord2", "kalshi", "sell", 20, 0.60, fee_paid=0.20
        )
        report = await engine.reconcile_balances()
        # SELL 20 @ 0.60 = +12.0, fee -0.20 → +11.80
        assert abs(report["kalshi"]["local"] - 11.80) < 0.01

    @pytest.mark.asyncio
    async def test_unreachable_exchange(self, engine):
        engine.polymarket_client.get_balance = AsyncMock(return_value=None)
        report = await engine.reconcile_balances()
        assert report["polymarket"]["status"] == "UNREACHABLE"
        assert report["polymarket"]["discrepancy"] is None

    @pytest.mark.asyncio
    async def test_exchange_error(self, engine):
        engine.kalshi_client.get_balance = AsyncMock(side_effect=Exception("API down"))
        report = await engine.reconcile_balances()
        assert report["kalshi"]["status"] == "ERROR"

    @pytest.mark.asyncio
    async def test_reconciliation_logged_to_db(self, db, engine):
        await engine.reconcile_balances()
        cursor = await db.execute("SELECT COUNT(*) FROM reconciliation_log")
        count = (await cursor.fetchone())[0]
        assert count == 2  # one per platform


# ── reconcile_positions ─────────────────────────────────────────────────


class TestReconcilePositions:
    @pytest.mark.asyncio
    async def test_zero_positions(self, engine):
        report = await engine.reconcile_positions()
        assert report["polymarket"]["local_count"] == 0
        assert report["kalshi"]["local_count"] == 0

    @pytest.mark.asyncio
    async def test_counts_open_positions(self, db, engine):
        for i in range(3):
            await _insert_position(db, f"pos_poly_{i}", "polymarket_arb")
        for i in range(2):
            await _insert_position(db, f"pos_kal_{i}", "kalshi_arb")
        # Closed: should not count
        await _insert_position(db, "pos_closed", "polymarket_arb", status="closed")

        report = await engine.reconcile_positions()
        assert report["polymarket"]["local_count"] == 3
        assert report["kalshi"]["local_count"] == 2


# ── run_reconciliation_check (halt logic) ───────────────────────────────


class TestRunReconciliationCheck:
    @pytest.mark.asyncio
    async def test_no_halt_when_balanced(self, engine):
        engine.polymarket_client.get_balance = AsyncMock(return_value=0.0)
        engine.kalshi_client.get_balance = AsyncMock(return_value=0.0)
        should_continue, _ = await engine.run_reconciliation_check()
        assert should_continue is True
        assert engine.trading_halted is False

    @pytest.mark.asyncio
    async def test_halt_on_large_discrepancy(self, engine):
        # Exchange says 5000, local 0 → disc 50% >> 5%
        should_continue, _ = await engine.run_reconciliation_check()
        assert should_continue is False
        assert engine.trading_halted is True

    @pytest.mark.asyncio
    async def test_halt_logged_to_system_events(self, db, engine):
        await engine.run_reconciliation_check()
        cursor = await db.execute(
            "SELECT event_type, severity, component FROM system_events WHERE event_type='RECONCILIATION_HALT'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[1] == "critical"
        assert row[2] == "reconciliation"

    @pytest.mark.asyncio
    async def test_unreachable_does_not_halt(self, engine):
        engine.polymarket_client.get_balance = AsyncMock(return_value=None)
        engine.kalshi_client.get_balance = AsyncMock(return_value=None)
        should_continue, _ = await engine.run_reconciliation_check()
        assert should_continue is True

    @pytest.mark.asyncio
    async def test_halt_flag_resets_on_ok(self, engine):
        # Cause halt
        engine.polymarket_client.get_balance = AsyncMock(return_value=9999.0)
        await engine.run_reconciliation_check()
        assert engine.trading_halted is True

        # Now balanced
        engine.polymarket_client.get_balance = AsyncMock(return_value=0.0)
        engine.kalshi_client.get_balance = AsyncMock(return_value=0.0)
        should_continue, _ = await engine.run_reconciliation_check()
        assert should_continue is True
        assert engine.trading_halted is False


# ── _estimate_portfolio_value ───────────────────────────────────────────


class TestEstimatePortfolioValue:
    @pytest.mark.asyncio
    async def test_default_is_starting_capital(self, engine):
        value = await engine._estimate_portfolio_value()
        assert abs(value - 10000.0) < 0.01

    @pytest.mark.asyncio
    async def test_accounts_for_realized_pnl(self, db, engine):
        await db.execute(
            """INSERT INTO positions (id, market_id, side, entry_price, entry_size,
                                      signal_id, strategy, status, realized_pnl,
                                      opened_at, closed_at, updated_at)
               VALUES ('pos_cl', 'mkt_1', 'BUY', 0.5, 100, 'sig_1', 'arb', 'closed',
                        50.0, ?, ?, ?)""",
            (NOW, NOW, NOW),
        )
        await db.commit()
        value = await engine._estimate_portfolio_value()
        assert abs(value - 10050.0) < 0.01

    @pytest.mark.asyncio
    async def test_floor_at_100(self, engine):
        engine.starting_capital = 0.0
        value = await engine._estimate_portfolio_value()
        assert value >= 100.0
