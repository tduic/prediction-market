"""
Tests for execution/resolution.py — ResolutionMonitor.

Covers: check_resolutions (auto-close on market resolution, BUY/SELL PnL),
        force_close_position (manual exit, guards against non-open),
        get_stale_positions (age-based detection),
        _write_trade_outcome (DB persistence).
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.resolution import ResolutionMonitor
from execution.state import Position, PositionStateManager

MIGRATIONS_DIR = PROJECT_ROOT / "core" / "storage" / "migrations"


async def _apply_migrations(db: aiosqlite.Connection) -> None:
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        sql = sql_file.read_text()
        clean_lines = []
        for line in sql.split("\n"):
            stripped = line.strip()
            if stripped.upper().startswith("ALTER TABLE") and "ADD COLUMN" in stripped.upper():
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


@pytest_asyncio.fixture
async def state(db):
    return PositionStateManager(db)


@pytest_asyncio.fixture
async def monitor(db, state):
    return ResolutionMonitor(db, state)


NOW = datetime.now(timezone.utc).isoformat()


async def _seed_market(db, market_id, status="open", outcome=None, outcome_value=None):
    await db.execute(
        """INSERT INTO markets (id, platform, platform_id, title, status,
                                outcome, outcome_value, created_at, updated_at)
           VALUES (?, 'polymarket', ?, 'Test market', ?, ?, ?, ?, ?)""",
        (market_id, market_id, status, outcome, outcome_value, NOW, NOW),
    )
    await db.commit()


async def _seed_position(db, position_id, market_id, side="BUY", entry_price=0.50,
                          entry_size=100.0, strategy="P1_cross_market_arb",
                          opened_at=None):
    if opened_at is None:
        opened_at = NOW
    await db.execute(
        """INSERT INTO positions (id, market_id, side, entry_price, entry_size,
                                  signal_id, strategy, status, opened_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (position_id, market_id, side, entry_price, entry_size,
         f"sig-{position_id}", strategy, opened_at, NOW),
    )
    await db.commit()


def _make_position(pid, mkt, side, qty, entry_price):
    return Position(
        position_id=pid, market_id=mkt, platform="polymarket",
        side=side, quantity=qty, entry_price=entry_price, entry_timestamp=0,
    )


# ── check_resolutions ───────────────────────────────────────────────────


class TestCheckResolutions:

    @pytest.mark.asyncio
    async def test_no_open_positions(self, monitor):
        summary = await monitor.check_resolutions()
        assert summary["checked"] == 0
        assert summary["closed"] == 0

    @pytest.mark.asyncio
    async def test_close_buy_on_yes_resolution(self, db, state, monitor):
        """BUY at 0.40, resolves YES (1.0) → PnL = +60."""
        await _seed_market(db, "mkt1", status="resolved", outcome="yes", outcome_value=1.0)
        await _seed_position(db, "pos1", "mkt1", side="BUY", entry_price=0.40, entry_size=100.0)
        state.positions["pos1"] = _make_position("pos1", "mkt1", "BUY", 100.0, 0.40)

        summary = await monitor.check_resolutions()
        assert summary["closed"] == 1
        assert abs(summary["total_realized_pnl"] - 60.0) < 0.01

        cursor = await db.execute("SELECT status, exit_price, realized_pnl FROM positions WHERE id='pos1'")
        row = await cursor.fetchone()
        assert row[0] == "closed"
        assert abs(row[1] - 1.0) < 0.01

    @pytest.mark.asyncio
    async def test_close_sell_on_no_resolution(self, db, state, monitor):
        """SELL at 0.70, resolves NO (0.0) → PnL = +70."""
        await _seed_market(db, "mkt2", status="resolved", outcome="no", outcome_value=0.0)
        await _seed_position(db, "pos2", "mkt2", side="SELL", entry_price=0.70, entry_size=100.0)
        state.positions["pos2"] = _make_position("pos2", "mkt2", "SELL", 100.0, 0.70)

        summary = await monitor.check_resolutions()
        assert summary["closed"] == 1
        assert abs(summary["total_realized_pnl"] - 70.0) < 0.01

    @pytest.mark.asyncio
    async def test_buy_losing_trade(self, db, state, monitor):
        """BUY at 0.80, resolves NO (0.0) → PnL = -80."""
        await _seed_market(db, "mkt3", status="resolved", outcome="no", outcome_value=0.0)
        await _seed_position(db, "pos3", "mkt3", side="BUY", entry_price=0.80, entry_size=100.0)
        state.positions["pos3"] = _make_position("pos3", "mkt3", "BUY", 100.0, 0.80)

        summary = await monitor.check_resolutions()
        assert summary["closed"] == 1
        assert abs(summary["total_realized_pnl"] - (-80.0)) < 0.01

    @pytest.mark.asyncio
    async def test_open_market_not_closed(self, db, state, monitor):
        await _seed_market(db, "mkt4", status="open")
        await _seed_position(db, "pos4", "mkt4")

        summary = await monitor.check_resolutions()
        assert summary["checked"] == 1
        assert summary["closed"] == 0

    @pytest.mark.asyncio
    async def test_trade_outcome_written(self, db, state, monitor):
        await _seed_market(db, "mkt5", status="closed", outcome="yes", outcome_value=1.0)
        await _seed_position(db, "pos5", "mkt5", side="BUY", entry_price=0.50, entry_size=10.0)
        state.positions["pos5"] = _make_position("pos5", "mkt5", "BUY", 10.0, 0.50)

        await monitor.check_resolutions()

        cursor = await db.execute("SELECT actual_pnl FROM trade_outcomes WHERE id='outcome-pos5'")
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row[0] - 5.0) < 0.01

    @pytest.mark.asyncio
    async def test_missing_market_skipped(self, db, state, monitor):
        await _seed_position(db, "pos6", "nonexistent_market")
        summary = await monitor.check_resolutions()
        assert summary["checked"] == 1
        assert summary["closed"] == 0


# ── force_close_position ────────────────────────────────────────────────


class TestForceClosePosition:

    @pytest.mark.asyncio
    async def test_force_close_success(self, db, state, monitor):
        await _seed_market(db, "mkt_fc", status="open")
        await _seed_position(db, "pos_fc", "mkt_fc", side="BUY", entry_price=0.40, entry_size=50.0)
        state.positions["pos_fc"] = _make_position("pos_fc", "mkt_fc", "BUY", 50.0, 0.40)

        result = await monitor.force_close_position("pos_fc", exit_price=0.60, reason="stop_loss")

        assert result is not None
        assert result["position_id"] == "pos_fc"
        assert abs(result["realized_pnl"] - 10.0) < 0.01
        assert result["reason"] == "stop_loss"

        cursor = await db.execute("SELECT status FROM positions WHERE id='pos_fc'")
        assert (await cursor.fetchone())[0] == "closed"

    @pytest.mark.asyncio
    async def test_force_close_nonexistent(self, monitor):
        result = await monitor.force_close_position("nonexistent", exit_price=0.50)
        assert result is None

    @pytest.mark.asyncio
    async def test_force_close_already_closed(self, db, state, monitor):
        await _seed_market(db, "mkt_cl", status="open")
        await _seed_position(db, "pos_cl", "mkt_cl")
        await db.execute("UPDATE positions SET status='closed' WHERE id='pos_cl'")
        await db.commit()

        result = await monitor.force_close_position("pos_cl", exit_price=0.50)
        assert result is None


# ── get_stale_positions ─────────────────────────────────────────────────


class TestStalePositions:

    @pytest.mark.asyncio
    async def test_no_stale_positions(self, db, monitor):
        now = datetime.now(timezone.utc).isoformat()
        await _seed_market(db, "mkt_fresh")
        await _seed_position(db, "pos_fresh", "mkt_fresh", opened_at=now)

        stale = await monitor.get_stale_positions(threshold_hours=72.0)
        assert len(stale) == 0

    @pytest.mark.asyncio
    async def test_stale_position_detected(self, db, monitor):
        old_time = (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat()
        await _seed_market(db, "mkt_old")
        await _seed_position(db, "pos_old", "mkt_old", opened_at=old_time)

        stale = await monitor.get_stale_positions(threshold_hours=72.0)
        assert len(stale) == 1
        assert stale[0]["position_id"] == "pos_old"
        assert stale[0]["age_hours"] > 72.0
