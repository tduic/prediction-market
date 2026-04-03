"""
Tests for execution/state.py — PositionStateManager.

Covers: Position dataclass (update_price, PnL calculation),
        track_fill, update_pnl, close_position (BUY/SELL/missing),
        load_positions_from_db, get_net_exposure,
        get_total_unrealized_pnl, get_market_exposure.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.state import Position, PositionStateManager  # noqa: E402

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


@pytest_asyncio.fixture
async def state(db):
    return PositionStateManager(db, flush_interval_s=999)


async def _seed_position_in_db(
    db,
    pid,
    market_id,
    side="BUY",
    entry_price=0.50,
    entry_size=100.0,
    strategy="arb",
    status="open",
    current_price=None,
    unrealized_pnl=None,
):
    """Insert position using the real schema."""
    await db.execute(
        """INSERT INTO positions (id, market_id, side, entry_price, entry_size,
                                  signal_id, strategy, status, opened_at, updated_at,
                                  current_price, unrealized_pnl)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pid,
            market_id,
            side,
            entry_price,
            entry_size,
            f"sig_{pid}",
            strategy,
            status,
            NOW,
            NOW,
            current_price,
            unrealized_pnl,
        ),
    )
    await db.commit()


# ── Position dataclass ──────────────────────────────────────────────────


class TestPositionDataclass:

    def test_buy_pnl_profit(self):
        pos = Position("p1", "m1", "poly", "BUY", 100.0, 0.40, 0.0)
        pos.update_price(0.60)
        assert abs(pos.unrealized_pnl - 20.0) < 0.01

    def test_buy_pnl_loss(self):
        pos = Position("p1", "m1", "poly", "BUY", 100.0, 0.60, 0.0)
        pos.update_price(0.40)
        assert abs(pos.unrealized_pnl - (-20.0)) < 0.01

    def test_sell_pnl_profit(self):
        pos = Position("p1", "m1", "poly", "SELL", 100.0, 0.70, 0.0)
        pos.update_price(0.50)
        assert abs(pos.unrealized_pnl - 20.0) < 0.01

    def test_sell_pnl_loss(self):
        pos = Position("p1", "m1", "poly", "SELL", 100.0, 0.50, 0.0)
        pos.update_price(0.70)
        assert abs(pos.unrealized_pnl - (-20.0)) < 0.01

    def test_update_price_sets_current(self):
        pos = Position("p1", "m1", "poly", "BUY", 10.0, 0.50, 0.0)
        assert pos.current_price is None
        pos.update_price(0.55)
        assert pos.current_price == 0.55


# ── track_fill ──────────────────────────────────────────────────────────


class TestTrackFill:

    @pytest.mark.asyncio
    async def test_creates_in_memory_position(self, state):
        pid = await state.track_fill("ord1", "mkt1", "polymarket", "BUY", 50.0, 0.45)
        assert pid in state.positions
        pos = state.positions[pid]
        assert pos.market_id == "mkt1"
        assert pos.side == "BUY"
        assert pos.quantity == 50.0
        assert pos.entry_price == 0.45

    @pytest.mark.asyncio
    async def test_adds_to_pending_writes(self, state):
        await state.track_fill("ord1", "mkt1", "polymarket", "BUY", 50.0, 0.45)
        assert len(state.pending_writes) == 1

    @pytest.mark.asyncio
    async def test_multiple_fills_tracked(self, state):
        await state.track_fill("o1", "mkt1", "poly", "BUY", 10, 0.50)
        await state.track_fill("o2", "mkt2", "poly", "SELL", 20, 0.60)
        assert len(state.positions) == 2


# ── update_pnl ──────────────────────────────────────────────────────────


class TestUpdatePnl:

    @pytest.mark.asyncio
    async def test_updates_matching_positions(self, state):
        await state.track_fill("o1", "mkt_A", "poly", "BUY", 100, 0.50)
        await state.track_fill("o2", "mkt_B", "poly", "BUY", 100, 0.50)

        updated = await state.update_pnl("mkt_A", 0.60)
        assert len(updated) == 1
        for pnl in updated.values():
            assert abs(pnl - 10.0) < 0.01

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self, state):
        updated = await state.update_pnl("nonexistent", 0.55)
        assert updated == {}


# ── close_position ──────────────────────────────────────────────────────


class TestClosePosition:

    @pytest.mark.asyncio
    async def test_close_buy_position(self, db, state):
        # Seed in DB and in-memory
        await _seed_position_in_db(db, "pos1", "mkt1", "BUY", 0.40, 100)
        state.positions["pos1"] = Position(
            "pos1",
            "mkt1",
            "poly",
            "BUY",
            100,
            0.40,
            0,
        )

        result = await state.close_position("pos1", exit_price=0.70)
        assert result is not None
        assert abs(result["realized_pnl"] - 30.0) < 0.01
        assert "pos1" not in state.positions

    @pytest.mark.asyncio
    async def test_close_sell_position(self, db, state):
        await _seed_position_in_db(db, "pos2", "mkt2", "SELL", 0.70, 100)
        state.positions["pos2"] = Position(
            "pos2",
            "mkt2",
            "poly",
            "SELL",
            100,
            0.70,
            0,
        )

        result = await state.close_position("pos2", exit_price=0.40)
        assert abs(result["realized_pnl"] - 30.0) < 0.01

    @pytest.mark.asyncio
    async def test_close_nonexistent_returns_none(self, state):
        result = await state.close_position("nonexistent", exit_price=0.50)
        assert result is None

    @pytest.mark.asyncio
    async def test_close_writes_trade_outcome(self, db, state):
        await _seed_position_in_db(db, "pos3", "mkt3", "BUY", 0.50, 10)
        state.positions["pos3"] = Position(
            "pos3",
            "mkt3",
            "poly",
            "BUY",
            10,
            0.50,
            0,
        )

        await state.close_position("pos3", exit_price=0.80)
        cursor = await db.execute(
            "SELECT actual_pnl FROM trade_outcomes WHERE id LIKE 'outcome-%'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row[0] - 3.0) < 0.01  # (0.80 - 0.50) * 10


# ── load_positions_from_db ──────────────────────────────────────────────


class TestLoadPositionsFromDb:

    @pytest.mark.asyncio
    async def test_loads_open_positions(self, db, state):
        await _seed_position_in_db(db, "pos1", "mkt1", "BUY", 0.50, 100)
        await _seed_position_in_db(db, "pos2", "mkt2", "SELL", 0.70, 50)
        await _seed_position_in_db(
            db, "pos3", "mkt3", "BUY", 0.50, 100, status="closed"
        )

        count = await state.load_positions_from_db()
        assert count == 2
        assert "pos1" in state.positions
        assert "pos2" in state.positions
        assert "pos3" not in state.positions

    @pytest.mark.asyncio
    async def test_empty_db_loads_zero(self, state):
        count = await state.load_positions_from_db()
        assert count == 0

    @pytest.mark.asyncio
    async def test_loaded_position_has_correct_fields(self, db, state):
        await _seed_position_in_db(
            db,
            "posX",
            "mktX",
            "BUY",
            0.40,
            200,
            strategy="polymarket_arb",
            current_price=0.55,
            unrealized_pnl=30.0,
        )

        await state.load_positions_from_db()
        pos = state.positions["posX"]
        assert pos.side == "BUY"
        assert pos.entry_price == 0.40
        assert pos.quantity == 200
        assert pos.current_price == 0.55
        assert abs(pos.unrealized_pnl - 30.0) < 0.01


# ── Exposure helpers ────────────────────────────────────────────────────


class TestExposureHelpers:

    @pytest.mark.asyncio
    async def test_net_exposure_buy_only(self, state):
        await state.track_fill("o1", "mkt1", "poly", "BUY", 100, 0.50)
        exposure = state.get_net_exposure("mkt1")
        assert abs(exposure - 100.0) < 0.01

    @pytest.mark.asyncio
    async def test_net_exposure_mixed(self, state):
        state.positions["buy1"] = Position("buy1", "mkt1", "poly", "BUY", 100, 0.50, 0)
        state.positions["sell1"] = Position(
            "sell1", "mkt1", "poly", "SELL", 40, 0.55, 0
        )
        # Net = 100 (BUY) - 40 (SELL) = 60
        exposure = state.get_net_exposure("mkt1")
        assert abs(exposure - 60.0) < 0.01

    @pytest.mark.asyncio
    async def test_total_unrealized_pnl(self, state):
        await state.track_fill("o1", "mkt1", "poly", "BUY", 100, 0.50)
        await state.track_fill("o2", "mkt2", "poly", "SELL", 100, 0.70)

        await state.update_pnl("mkt1", 0.60)  # +10
        await state.update_pnl("mkt2", 0.50)  # +20

        total = state.get_total_unrealized_pnl()
        assert abs(total - 30.0) < 0.01

    @pytest.mark.asyncio
    async def test_market_exposure_map(self, state):
        state.positions["buy_a"] = Position(
            "buy_a", "mkt_A", "poly", "BUY", 100, 0.50, 0
        )
        state.positions["sell_a"] = Position(
            "sell_a", "mkt_A", "poly", "SELL", 30, 0.55, 0
        )
        state.positions["buy_b"] = Position(
            "buy_b", "mkt_B", "poly", "BUY", 50, 0.40, 0
        )

        exposure_map = state.get_market_exposure()
        assert abs(exposure_map["mkt_A"] - 70.0) < 0.01
        assert abs(exposure_map["mkt_B"] - 50.0) < 0.01

    @pytest.mark.asyncio
    async def test_get_open_positions(self, state):
        await state.track_fill("o1", "mkt1", "poly", "BUY", 10, 0.50)
        await state.track_fill("o2", "mkt2", "poly", "SELL", 20, 0.60)
        assert len(state.get_open_positions()) == 2

    @pytest.mark.asyncio
    async def test_get_position_by_id(self, state):
        pid = await state.track_fill("o1", "mkt1", "poly", "BUY", 10, 0.50)
        assert state.get_position(pid) is not None
        assert state.get_position("nonexistent") is None
