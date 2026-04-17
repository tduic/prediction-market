"""
Tests for core/engine/arb_engine.py (ArbitrageEngine) and
core/engine/scheduler.py (ScheduledStrategyRunner).

Covers event-driven spread detection, trade execution, fired_state / re-arm
state machine, risk choke point, circuit breaker, Kelly sizing, and
execution_mode wiring.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import RiskControlConfig  # noqa: E402
from core.engine import ArbitrageEngine, ScheduledStrategyRunner  # noqa: E402


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


@pytest.mark.asyncio
class TestUpdatePairs:
    """Hot pair-list updates — used by the weekly refresh watcher."""

    async def test_add_new_pair(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        new_match = _make_match("poly_D", "kal_D", 0.30, 0.35)
        delta = engine.update_pairs([*matches, new_match])

        assert delta == {"added": 1, "removed": 0, "retained": 3}
        assert "poly_D_kal_D" in engine._pairs
        assert "poly_D" in engine._poly_to_pairs
        assert engine.prices["poly_D"] == 0.30
        assert engine._market_platform["poly_D"] == "polymarket"
        assert engine._market_platform["kal_D"] == "kalshi"

    async def test_remove_dropped_pair(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        delta = engine.update_pairs(matches[:2])  # drop poly_C/kal_C

        assert delta == {"added": 0, "removed": 1, "retained": 2}
        assert "poly_C_kal_C" not in engine._pairs
        assert "poly_C" not in engine._poly_to_pairs

    async def test_preserves_live_prices_for_retained_markets(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        # Simulate a live price coming in via the websocket feed.
        engine.prices["poly_A"] = 0.99

        # Refresh with stale match data — the seed price should NOT clobber live.
        stale = [_make_match("poly_A", "kal_A", 0.50, 0.55), *matches[1:]]
        engine.update_pairs(stale)

        assert engine.prices["poly_A"] == 0.99

    async def test_retains_fired_state_for_retained_pairs(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        # Put poly_A/kal_A into fired_state (simulate a prior trade).
        from core.engine.fire_state import PairFireState

        engine.fired_state["poly_A_kal_A"] = PairFireState(
            last_fired_at=time.time(), armed=False
        )

        # Refresh with the same set — fired_state must persist so cooldown holds.
        engine.update_pairs(matches)
        assert "poly_A_kal_A" in engine.fired_state

    async def test_drops_fired_state_for_removed_pairs(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        from core.engine.fire_state import PairFireState

        engine.fired_state["poly_C_kal_C"] = PairFireState(
            last_fired_at=time.time(), armed=False
        )

        engine.update_pairs(matches[:2])  # drops poly_C/kal_C
        assert "poly_C_kal_C" not in engine.fired_state

    async def test_sets_initial_sweep_flag_on_add(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await engine.initial_sweep()
        assert engine._needs_initial_sweep is False

        new_match = _make_match("poly_D", "kal_D", 0.30, 0.35)
        engine.update_pairs([*matches, new_match])
        assert engine._needs_initial_sweep is True

    async def test_no_sweep_flag_when_nothing_added(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)
        await engine.initial_sweep()
        assert engine._needs_initial_sweep is False

        engine.update_pairs(matches[:2])  # pure removal
        assert engine._needs_initial_sweep is False

    async def test_empty_refresh_removes_everything(self, db, matches):
        await _seed_markets_for_engine(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03)

        delta = engine.update_pairs([])
        assert delta == {"added": 0, "removed": 3, "retained": 0}
        assert engine._pairs == {}
        assert engine._poly_to_pairs == {}
        assert engine._kalshi_to_pairs == {}


# ── execution_mode wiring: ScheduledStrategyRunner ────────────────────────────


class TestScheduledStrategyRunnerExecutionMode:
    def test_live_execution_mode_uses_live_risk_config(self):
        runner = ScheduledStrategyRunner(MagicMock(), execution_mode="live")
        assert runner._risk_config.max_position_pct == pytest.approx(0.02)
        assert runner._risk_config.min_edge == pytest.approx(0.05)
        assert runner._risk_config.max_daily_loss_pct == pytest.approx(0.01)

    def test_paper_execution_mode_uses_default_risk_config(self):
        runner = ScheduledStrategyRunner(MagicMock(), execution_mode="paper")
        assert runner._risk_config.max_position_pct == pytest.approx(0.05)
        assert runner._risk_config.min_edge == pytest.approx(0.02)

    def test_shadow_execution_mode_uses_default_risk_config(self):
        runner = ScheduledStrategyRunner(MagicMock(), execution_mode="shadow")
        assert runner._risk_config.max_position_pct == pytest.approx(0.05)

    def test_explicit_risk_config_overrides_execution_mode(self):
        explicit = RiskControlConfig(max_position_pct=0.10)
        runner = ScheduledStrategyRunner(
            MagicMock(), execution_mode="live", risk_config=explicit
        )
        assert runner._risk_config.max_position_pct == pytest.approx(0.10)

    def test_no_execution_mode_backward_compat(self):
        """No execution_mode → standard defaults (unchanged behavior)."""
        runner = ScheduledStrategyRunner(MagicMock())
        assert runner._risk_config.max_position_pct == pytest.approx(0.05)


# ── execution_mode wiring: ArbitrageEngine ────────────────────────────────────


class TestArbitrageEngineExecutionMode:
    def test_live_execution_mode_uses_live_risk_config(self, matches):
        engine = ArbitrageEngine(MagicMock(), matches, execution_mode="live")
        assert engine._risk_config.max_position_pct == pytest.approx(0.02)
        assert engine._risk_config.min_edge == pytest.approx(0.05)

    def test_paper_execution_mode_uses_default_risk_config(self, matches):
        engine = ArbitrageEngine(MagicMock(), matches, execution_mode="paper")
        assert engine._risk_config.max_position_pct == pytest.approx(0.05)

    def test_explicit_risk_config_overrides_execution_mode(self, matches):
        explicit = RiskControlConfig(max_position_pct=0.10)
        engine = ArbitrageEngine(
            MagicMock(), matches, execution_mode="live", risk_config=explicit
        )
        assert engine._risk_config.max_position_pct == pytest.approx(0.10)

    def test_no_execution_mode_backward_compat(self, matches):
        engine = ArbitrageEngine(MagicMock(), matches)
        assert engine._risk_config.max_position_pct == pytest.approx(0.05)


# ── Helpers shared by risk-choke and re-arm tests ────────────────────────────
# Prices: kal_A seeded at 0.58, trigger poly_A to 0.51 → spread 0.07
_POLY_SEED = 0.50
_KAL_SEED = 0.58
_POLY_TRIGGER = 0.51  # delta=0.01 > 0.001 guard


def _risk_config(**overrides):
    """RiskControlConfig with sensible test defaults, all Phase 5 fields set."""
    defaults = dict(
        starting_capital=10000.0,
        max_position_pct=0.05,
        max_daily_loss_pct=0.02,
        max_portfolio_exposure_pct=0.20,
        kelly_fraction=0.25,
        duplicate_signal_window_s=300,
        min_edge=0.02,
        consecutive_failure_limit=5,
        arb_cooldown_s=60.0,
        arb_rearm_hysteresis=0.005,
        slippage_bps=10.0,
        strategy_holding_period_s=300,
        strategy_replay_cooldown_s=300,
        strategy_replay_min_move=0.01,
        strategy_p2_enabled=True,
        strategy_p3_enabled=True,
        strategy_p4_enabled=True,
        strategy_p5_enabled=True,
        strategy_killswitch_window_s=604800,
        strategy_killswitch_min_trades=5,
    )
    defaults.update(overrides)
    cfg = RiskControlConfig.__new__(RiskControlConfig)
    for k, v in defaults.items():
        object.__setattr__(cfg, k, v)
    return cfg


async def _seed_markets_p23(db, matches):
    """Seed markets + prices for risk-choke / re-arm tests."""
    now = datetime.now(timezone.utc).isoformat()
    for m in matches:
        for mid, platform, price in [
            (m["poly_id"], "polymarket", m["poly_price"]),
            (m["kalshi_id"], "kalshi", m["kalshi_price"]),
        ]:
            await db.execute(
                "INSERT OR IGNORE INTO markets "
                "(id, platform, platform_id, title, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'open', ?, ?)",
                (mid, platform, mid, f"Title {mid}", now, now),
            )
            if price is not None:
                await db.execute(
                    "INSERT INTO market_prices "
                    "(market_id, yes_price, no_price, spread, liquidity, polled_at) "
                    "VALUES (?, ?, ?, 0.02, 10000, ?)",
                    (mid, price, round(1 - price, 4), now),
                )
    await db.commit()


# ── Risk choke point (Phase 2) ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestArbEngineAcceptsRiskConfig:
    async def test_engine_accepts_risk_config(self, db):
        cfg = _risk_config()
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        assert engine._risk_config is cfg

    async def test_engine_accepts_circuit_breaker(self, db):
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=False)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, circuit_breaker=cb)
        assert engine._circuit_breaker is cb

    async def test_engine_defaults_risk_config_when_none(self, db):
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert engine._risk_config is not None
        assert hasattr(engine._risk_config, "starting_capital")


@pytest.mark.asyncio
class TestCircuitBreakerHalt:
    async def test_halted_circuit_breaker_prevents_trade(self, db):
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=True)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, circuit_breaker=cb)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 0

    async def test_halted_pair_not_added_to_recently_fired(self, db):
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=True)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, circuit_breaker=cb)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" not in engine.recently_fired

    async def test_active_circuit_breaker_allows_trade(self, db):
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=False)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, circuit_breaker=cb, risk_config=_risk_config()
        )
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1

    async def test_no_circuit_breaker_trades_normally(self, db):
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1


@pytest.mark.asyncio
class TestRiskChecksBlockTrades:
    async def test_below_min_edge_blocked(self, db):
        cfg = _risk_config(min_edge=0.20)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 0

    async def test_above_min_edge_allowed(self, db):
        cfg = _risk_config(min_edge=0.02)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1


@pytest.mark.asyncio
class TestRecentlyFiredNotUpdatedOnRejection:
    async def test_failed_risk_check_leaves_pair_retriable(self, db):
        cfg = _risk_config(min_edge=0.99)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" not in engine.recently_fired

    async def test_successful_trade_adds_to_recently_fired(self, db):
        cfg = _risk_config(min_edge=0.02)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" in engine.recently_fired


@pytest.mark.asyncio
class TestKellySizing:
    async def test_position_size_respects_max_position_pct(self, db):
        cfg = _risk_config(starting_capital=10000.0, max_position_pct=0.05)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1
        cursor = await db.execute(
            "SELECT entry_size FROM positions ORDER BY opened_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] <= 500.0

    async def test_small_bankroll_produces_small_position(self, db):
        cfg = _risk_config(starting_capital=100.0, max_position_pct=0.05)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) >= 1
        cursor = await db.execute(
            "SELECT entry_size FROM positions ORDER BY opened_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] <= 5.0

    async def test_large_bankroll_exceeds_old_hardcoded_cap(self, db):
        cfg = _risk_config(
            starting_capital=1_000_000.0,
            max_position_pct=0.05,
            kelly_fraction=0.25,
            min_edge=0.02,
        )
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1
        cursor = await db.execute(
            "SELECT entry_size FROM positions ORDER BY opened_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] > 10.0


class TestScheduledRunnerAcceptsRiskConfig:
    def test_runner_accepts_risk_config(self, db):
        cfg = _risk_config()
        runner = ScheduledStrategyRunner(db, interval=120, risk_config=cfg)
        assert runner._risk_config is cfg

    def test_runner_accepts_circuit_breaker(self, db):
        cb = MagicMock()
        runner = ScheduledStrategyRunner(db, interval=120, circuit_breaker=cb)
        assert runner._circuit_breaker is cb

    def test_runner_defaults_when_no_risk_config(self, db):
        runner = ScheduledStrategyRunner(db, interval=120)
        assert runner._risk_config is not None


@pytest.mark.asyncio
class TestScheduledCircuitBreaker:
    async def test_halted_runner_skips_cycle(self, db):
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=True)
        runner = ScheduledStrategyRunner(
            db, interval=120, risk_config=_risk_config(), circuit_breaker=cb
        )
        trades = await runner.run_one_cycle()
        assert len(trades) == 0

    async def test_active_runner_runs_normally(self, db):
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=False)
        runner = ScheduledStrategyRunner(
            db, interval=120, risk_config=_risk_config(), circuit_breaker=cb
        )
        trades = await runner.run_one_cycle()
        assert isinstance(trades, list)


# ── Re-arm state machine (Phase 3) ───────────────────────────────────────────


class TestPairFireState:
    def test_pairfirestate_exists(self):
        from core.engine import PairFireState as PFS

        assert PFS is not None

    def test_pairfirestate_fields(self):
        from core.engine import PairFireState as PFS

        state = PFS(last_fired_at=time.time(), armed=True)
        assert hasattr(state, "last_fired_at")
        assert hasattr(state, "armed")
        assert hasattr(state, "last_spread_seen_below")

    def test_pairfirestate_defaults(self):
        from core.engine import PairFireState as PFS

        state = PFS(last_fired_at=0.0, armed=True)
        assert state.last_spread_seen_below is None

    def test_pairfirestate_armed_false(self):
        from core.engine import PairFireState as PFS

        state = PFS(last_fired_at=time.time(), armed=False)
        assert state.armed is False


class TestRiskConfigNewFields:
    def test_arb_cooldown_s_has_default(self):
        cfg = RiskControlConfig()
        assert hasattr(cfg, "arb_cooldown_s")
        assert cfg.arb_cooldown_s > 0

    def test_arb_rearm_hysteresis_has_default(self):
        cfg = RiskControlConfig()
        assert hasattr(cfg, "arb_rearm_hysteresis")
        assert 0 < cfg.arb_rearm_hysteresis < 0.05

    def test_arb_cooldown_s_default_is_60(self):
        import os

        old = os.environ.pop("ARB_COOLDOWN_S", None)
        try:
            cfg = RiskControlConfig()
            assert cfg.arb_cooldown_s == 60.0
        finally:
            if old is not None:
                os.environ["ARB_COOLDOWN_S"] = old

    def test_arb_rearm_hysteresis_default_is_0005(self):
        import os

        old = os.environ.pop("ARB_REARM_HYSTERESIS", None)
        try:
            cfg = RiskControlConfig()
            assert cfg.arb_rearm_hysteresis == 0.005
        finally:
            if old is not None:
                os.environ["ARB_REARM_HYSTERESIS"] = old


@pytest.mark.asyncio
class TestFiredStateAttribute:
    async def test_fired_state_is_dict(self, db):
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert isinstance(engine.fired_state, dict)

    async def test_fired_state_empty_at_init(self, db):
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert len(engine.fired_state) == 0

    async def test_fired_state_populated_after_trade(self, db):
        from core.engine import PairFireState as PFS

        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" in engine.fired_state
        state = engine.fired_state["poly_A_kal_A"]
        assert isinstance(state, PFS)
        assert state.last_fired_at > 0

    async def test_fired_state_armed_false_immediately_after_trade(self, db):
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert engine.fired_state["poly_A_kal_A"].armed is False


@pytest.mark.asyncio
class TestRecentlyFiredBackwardCompat:
    async def test_recently_fired_is_set_like(self, db):
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" in engine.recently_fired

    async def test_recently_fired_empty_before_trade(self, db):
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert len(engine.recently_fired) == 0

    async def test_recently_fired_len_after_trade(self, db):
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.recently_fired) >= 1


@pytest.mark.asyncio
class TestFireEligibility:
    async def test_pair_not_eligible_during_cooldown(self, db):
        cfg = _risk_config(arb_cooldown_s=999.0, arb_rearm_hysteresis=0.005)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1
        await _simulate_price_update(engine, db, "poly_A", 0.52)
        assert len(engine.trades) == 1

    async def test_pair_eligible_after_cooldown_and_rearm(self, db):
        cfg = _risk_config(arb_cooldown_s=0.0, arb_rearm_hysteresis=0.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1
        await _simulate_price_update(engine, db, "poly_A", _KAL_SEED - 0.01)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 2

    async def test_initial_pair_has_no_cooldown(self, db):
        cfg = _risk_config(arb_cooldown_s=60.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1


@pytest.mark.asyncio
class TestRearmTrigger:
    async def test_spread_reversion_arms_pair(self, db):
        cfg = _risk_config(arb_cooldown_s=0.0, arb_rearm_hysteresis=0.005)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.05, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert engine.fired_state["poly_A_kal_A"].armed is False
        await _simulate_price_update(engine, db, "poly_A", 0.55)
        assert engine.fired_state["poly_A_kal_A"].armed is True

    async def test_spread_above_threshold_does_not_arm(self, db):
        cfg = _risk_config(arb_cooldown_s=999.0, arb_rearm_hysteresis=0.005)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.05, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert engine.fired_state["poly_A_kal_A"].armed is False
        await _simulate_price_update(engine, db, "poly_A", 0.52)
        assert engine.fired_state["poly_A_kal_A"].armed is False

    async def test_never_fired_pair_treated_as_armed(self, db):
        cfg = _risk_config(arb_cooldown_s=999.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1


@pytest.mark.asyncio
class TestExceptionSafety:
    async def test_exception_rolls_back_fired_state(self, db):
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )
        original = engine._execute_arb_trade

        async def _failing_execute(*args, **kwargs):
            raise RuntimeError("Simulated execution failure")

        engine._execute_arb_trade = _failing_execute
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" not in engine.fired_state
        engine._execute_arb_trade = original
        await _simulate_price_update(engine, db, "poly_A", 0.52)
        assert len(engine.trades) >= 1

    async def test_failed_risk_check_does_not_enter_cooldown(self, db):
        cfg = _risk_config(min_edge=0.99, arb_cooldown_s=999.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 0
        assert "poly_A_kal_A" not in engine.fired_state


@pytest.mark.asyncio
class TestStatsWithFireState:
    async def test_stats_recently_fired_is_muted_count(self, db):
        cfg = _risk_config(arb_cooldown_s=999.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        assert engine.stats()["recently_fired"] == 0
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1
        assert engine.stats()["recently_fired"] == 1


@pytest.mark.asyncio
class TestPeriodicScan:
    async def test_periodic_scan_method_exists(self, db):
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert hasattr(engine, "periodic_scan")
        assert callable(engine.periodic_scan)

    async def test_periodic_scan_fires_eligible_pairs(self, db):
        cfg = _risk_config()
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        engine.prices["poly_A"] = _POLY_TRIGGER
        engine.prices["kal_A"] = _KAL_SEED
        assert len(engine.trades) == 0
        await engine.periodic_scan()
        assert len(engine.trades) == 1

    async def test_periodic_scan_respects_cooldown(self, db):
        cfg = _risk_config(arb_cooldown_s=999.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets_p23(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        engine.prices["poly_A"] = _POLY_TRIGGER
        engine.prices["kal_A"] = _KAL_SEED
        await engine.periodic_scan()
        assert len(engine.trades) == 1
        await engine.periodic_scan()
        assert len(engine.trades) == 1
