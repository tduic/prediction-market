"""
Unit tests for the ArbitrageEngine.

Tests event-driven price updates, spread detection, position dedup,
trade execution, and batch commit behavior.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.paper_trading_session import ArbitrageEngine  # noqa: E402


def _make_match(poly_id, kalshi_id, poly_price, kalshi_price, similarity=0.85):
    return {
        "poly_id": poly_id,
        "kalshi_id": kalshi_id,
        "poly_title": f"Poly {poly_id}",
        "kalshi_title": f"Kalshi {kalshi_id}",
        "poly_price": poly_price,
        "kalshi_price": kalshi_price,
        "similarity": similarity,
    }


@pytest.fixture
def matches():
    return [
        _make_match("poly_A", "kal_A", 0.50, 0.55),
        _make_match("poly_B", "kal_B", 0.40, 0.42),
        _make_match("poly_C", "kal_C", 0.60, 0.65),
    ]


async def _seed_markets_for_engine(db, matches):
    """Insert market + price records so the paper client can look up prices."""
    now = datetime.now(timezone.utc).isoformat()
    for m in matches:
        for mid, platform, plat_id, price in [
            (m["poly_id"], "polymarket", m["poly_id"], m["poly_price"]),
            (m["kalshi_id"], "kalshi", m["kalshi_id"], m["kalshi_price"]),
        ]:
            await db.execute(
                """INSERT OR IGNORE INTO markets
                   (id, platform, platform_id, title, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'open', ?, ?)""",
                (mid, platform, plat_id, f"Title {mid}", now, now),
            )
            await db.execute(
                """INSERT INTO market_prices
                   (market_id, yes_price, no_price, spread, liquidity, polled_at)
                   VALUES (?, ?, ?, 0.02, 10000, ?)""",
                (mid, price, round(1 - price, 4), now),
            )
    await db.commit()


async def _simulate_price_update(engine, db, market_id: str, new_price: float):
    """Simulate a websocket price update: write to DB + call engine.

    In production the websocket handler writes the new price to market_prices
    before (or concurrently with) calling engine.on_price_update. The paper
    client reads from the DB, so both must be consistent.
    """
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO market_prices
           (market_id, yes_price, no_price, spread, liquidity, polled_at)
           VALUES (?, ?, ?, 0.02, 10000, ?)""",
        (market_id, new_price, round(1 - new_price, 4), now),
    )
    await db.commit()
    await engine.on_price_update(market_id, new_price)


@pytest.mark.asyncio
class TestArbitrageEngineInit:
    async def test_initializes_pair_indexes(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        assert len(engine._pairs) == 3
        assert "poly_A" in engine._poly_to_pairs
        assert "kal_A" in engine._kalshi_to_pairs

    async def test_seeds_prices_from_matches(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        assert engine.prices["poly_A"] == 0.50
        assert engine.prices["kal_A"] == 0.55

    async def test_empty_matches(self, db):
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert len(engine._pairs) == 0
        assert engine.prices == {}


@pytest.mark.asyncio
class TestOnPriceUpdate:
    async def test_no_trade_below_threshold(self, db, matches):
        """Spread below min_spread does not trigger a trade."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.10)

        # poly_A=0.50, kal_A=0.55 → spread=0.05 < 0.10 threshold
        await engine.on_price_update("poly_A", 0.50)
        assert len(engine.trades) == 0

    async def test_trade_above_threshold(self, db, matches):
        """Spread above min_spread triggers a trade."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        # Set poly_A to 0.45, kal_A is seeded at 0.55 → spread = 0.10
        await _simulate_price_update(engine, db, "poly_A", 0.45)
        assert len(engine.trades) == 1
        assert engine.trades[0]["strategy"] == "P1_cross_market_arb"

    async def test_position_dedup(self, db, matches):
        """Same pair is not traded twice."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        await _simulate_price_update(engine, db, "poly_A", 0.45)
        assert len(engine.trades) == 1

        # Same pair again — should NOT produce a second trade
        await _simulate_price_update(engine, db, "poly_A", 0.44)
        assert len(engine.trades) == 1

        pair_id = "poly_A_kal_A"
        assert pair_id in engine.recently_fired

    async def test_different_pairs_independent(self, db, matches):
        """Trading one pair does not block trading another."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        # poly_A: spread = 0.10 → trade
        await _simulate_price_update(engine, db, "poly_A", 0.45)
        # poly_C now 0.55 vs kal_C 0.65 = 0.10 spread
        await _simulate_price_update(engine, db, "poly_C", 0.55)
        assert len(engine.trades) == 2

    async def test_tiny_price_change_ignored(self, db, matches):
        """Price change < 0.001 is ignored (dedup noise)."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.10)

        # Seed a baseline price
        await engine.on_price_update("poly_A", 0.500)
        # Tiny change — should return early
        await engine.on_price_update("poly_A", 0.5005)
        # No trades should fire (spread is 0.05, below 0.10 threshold anyway)
        assert len(engine.trades) == 0

    async def test_unknown_market_id_ignored(self, db, matches):
        """Price update for unknown market_id does nothing."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        await engine.on_price_update("unknown_market_xyz", 0.50)
        assert len(engine.trades) == 0

    async def test_missing_counterpart_price_no_trade(self, db):
        """If one side has no price yet, no trade fires."""
        match = _make_match("poly_X", "kal_X", None, None)
        # No prices seeded — engine won't have counterpart prices
        engine = ArbitrageEngine(db, [match], min_spread=0.03)

        # Only poly side updates — kal side has no price
        await engine.on_price_update("poly_X", 0.50)
        assert len(engine.trades) == 0


@pytest.mark.asyncio
class TestArbTradeExecution:
    async def test_trade_records_pnl(self, db, matches):
        """Executed trade has actual_pnl in the result."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        await _simulate_price_update(engine, db, "poly_A", 0.45)
        assert len(engine.trades) == 1
        trade = engine.trades[0]
        assert "actual_pnl" in trade
        assert "fees" in trade
        assert "spread" in trade
        assert trade["spread"] >= 0.03

    async def test_trade_writes_to_db(self, db, matches):
        """Trade execution writes orders to the DB."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        await _simulate_price_update(engine, db, "poly_A", 0.45)
        await engine.flush()

        cursor = await db.execute("SELECT COUNT(*) FROM orders")
        row = await cursor.fetchone()
        assert row[0] >= 2  # Buy leg + sell leg

    async def test_trade_writes_signal_and_violation(self, db, matches):
        """Trade creates market_pair, violation, and signal records."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        await _simulate_price_update(engine, db, "poly_A", 0.45)
        await engine.flush()

        for table in ["market_pairs", "violations", "signals"]:
            cursor = await db.execute(f"SELECT COUNT(*) FROM {table}")
            row = await cursor.fetchone()
            assert row[0] >= 1, f"Expected at least 1 row in {table}"


@pytest.mark.asyncio
class TestFlushAndStats:
    async def test_flush_commits(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        engine._pending_commit = 5
        await engine.flush()
        assert engine._pending_commit == 0

    async def test_stats(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        stats = engine.stats()
        assert "pairs_monitored" in stats
        assert "pairs_eligible_now" in stats
        assert "recently_fired" in stats
        assert "total_pnl" in stats
        assert "prices_tracked" in stats
        assert stats["prices_tracked"] > 0


@pytest.mark.asyncio
class TestInitialSweep:
    async def test_sweep_trades_pairs_above_threshold(self, db, matches):
        """initial_sweep() fires trades for pairs already above min_spread at startup."""
        # poly_A=0.50, kal_A=0.55 → spread=0.05 > 0.03 threshold
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await engine.initial_sweep()
        # poly_A/kal_A (0.05) and poly_C/kal_C (0.05) exceed 0.03 threshold
        assert len(engine.trades) >= 1

    async def test_sweep_skips_pairs_below_threshold(self, db, matches):
        """initial_sweep() does not fire trades when all spreads are below threshold."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.10)
        await engine.initial_sweep()
        # poly_A=0.05, poly_B=0.02, poly_C=0.05 — all below 0.10
        assert len(engine.trades) == 0

    async def test_sweep_runs_only_once(self, db, matches):
        """Calling initial_sweep() a second time is a no-op (idempotent)."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await engine.initial_sweep()
        count_after_first = len(engine.trades)
        await engine.initial_sweep()
        assert len(engine.trades) == count_after_first

    async def test_sweep_marks_positions_open(self, db, matches):
        """Pairs traded during sweep are added to open_positions (prevents double-trade)."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await engine.initial_sweep()
        assert len(engine.recently_fired) > 0

    async def test_on_price_update_skips_swept_pairs(self, db, matches):
        """After sweep, price update on already-traded pair does not generate another trade."""
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await engine.initial_sweep()
        trades_after_sweep = len(engine.trades)

        # Trigger price update on poly_A — should be blocked by open_positions
        await _simulate_price_update(engine, db, "poly_A", 0.40)
        assert len(engine.trades) == trades_after_sweep

    async def test_sweep_skips_missing_prices(self, db):
        """Pairs with missing prices are skipped gracefully during sweep."""
        match = _make_match("poly_X", "kal_X", None, None)
        engine = ArbitrageEngine(db, [match], min_spread=0.03)
        await engine.initial_sweep()
        assert len(engine.trades) == 0
