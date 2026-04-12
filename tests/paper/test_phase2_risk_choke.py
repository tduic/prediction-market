"""
Phase 2 tests: risk choke point.

TDD: these tests define the contracts. Run before implementing to confirm
they fail, then implement until they all pass.

Covers:
  2.1  ArbitrageEngine accepts risk_config and circuit_breaker
  2.2  Circuit breaker halt blocks all arb trades
  2.3  Risk checks block oversized / low-edge trades
  2.4  Pair NOT added to recently_fired when risk checks fail
  2.5  Position size computed from Kelly+bankroll, not hardcoded $10
  2.6  ScheduledStrategyRunner accepts risk_config and circuit_breaker
  2.7  detect_single_platform_opportunities respects circuit breaker
"""

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import RiskControlConfig  # noqa: E402
from scripts.paper_trading_session import (  # noqa: E402
    ArbitrageEngine,
    ScheduledStrategyRunner,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


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


async def _seed_markets(db, matches):
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


async def _simulate_price_update(engine, db, market_id: str, new_price: float):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO market_prices "
        "(market_id, yes_price, no_price, spread, liquidity, polled_at) "
        "VALUES (?, ?, ?, 0.02, 10000, ?)",
        (market_id, new_price, round(1 - new_price, 4), now),
    )
    await db.commit()
    await engine.on_price_update(market_id, new_price)


def _risk_config(**overrides):
    """Returns a RiskControlConfig with given overrides."""
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


# Prices designed to produce a 0.07 spread (below PnL sanity cap):
# kal_A seeded at 0.58, poly_A triggered to 0.51 → spread = 0.07
# size = round(min(10, 100*0.07), 1) = 7.0
# pnl ≈ 0.07*7 - fees ≈ 0.49 - 0.15 = 0.34  (< cap=0.70)
_POLY_SEED = 0.50  # initial seeded price (engine caches this)
_KAL_SEED = 0.58  # produces spread = 0.07 after poly_A updates to 0.51
_POLY_TRIGGER = 0.51  # delta = 0.01 > 0.001 → passes tiny-change guard


# ── 2.1 ArbitrageEngine accepts risk_config and circuit_breaker ───────────────


@pytest.mark.asyncio
class TestArbEngineAcceptsRiskConfig:
    async def test_engine_accepts_risk_config(self, db):
        """ArbitrageEngine.__init__ accepts a risk_config parameter."""
        cfg = _risk_config()
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)
        assert engine._risk_config is cfg

    async def test_engine_accepts_circuit_breaker(self, db):
        """ArbitrageEngine.__init__ accepts a circuit_breaker parameter."""
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=False)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, circuit_breaker=cb)
        assert engine._circuit_breaker is cb

    async def test_engine_defaults_risk_config_when_none(self, db):
        """Without risk_config, ArbitrageEngine creates a default one."""
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert engine._risk_config is not None
        assert hasattr(engine._risk_config, "starting_capital")


# ── 2.2 Circuit breaker halt blocks trades ────────────────────────────────────


@pytest.mark.asyncio
class TestCircuitBreakerHalt:
    async def test_halted_circuit_breaker_prevents_trade(self, db):
        """When circuit_breaker.should_halt() is True, no trade fires."""
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=True)

        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, circuit_breaker=cb)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 0

    async def test_halted_pair_not_added_to_recently_fired(self, db):
        """When circuit breaker halts, pair is NOT added to recently_fired."""
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=True)

        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, circuit_breaker=cb)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" not in engine.recently_fired

    async def test_active_circuit_breaker_allows_trade(self, db):
        """When circuit_breaker.should_halt() is False, trade fires normally."""
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=False)

        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, circuit_breaker=cb, risk_config=_risk_config()
        )

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1

    async def test_no_circuit_breaker_trades_normally(self, db):
        """Without circuit_breaker, trade fires normally (backward compat)."""
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1


# ── 2.3 Risk checks block trades ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestRiskChecksBlockTrades:
    async def test_below_min_edge_blocked(self, db):
        """Risk check: trade with edge < min_edge is blocked."""
        # spread = 0.07, but min_edge = 0.20 → block
        cfg = _risk_config(min_edge=0.20)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 0

    async def test_above_min_edge_allowed(self, db):
        """Risk check: trade with edge above min_edge fires."""
        cfg = _risk_config(min_edge=0.02)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1


# ── 2.4 Pair NOT added to recently_fired when checks fail ────────────────────


@pytest.mark.asyncio
class TestRecentlyFiredNotUpdatedOnRejection:
    async def test_failed_risk_check_leaves_pair_retriable(self, db):
        """If risk checks fail, the pair stays eligible for retry on next tick."""
        cfg = _risk_config(min_edge=0.99)  # impossibly high → always blocked
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" not in engine.recently_fired

    async def test_successful_trade_adds_to_recently_fired(self, db):
        """If trade succeeds, pair IS added to recently_fired."""
        cfg = _risk_config(min_edge=0.02)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" in engine.recently_fired


# ── 2.5 Position size uses Kelly, not hardcoded $10 ──────────────────────────


@pytest.mark.asyncio
class TestKellySizing:
    async def test_position_size_respects_max_position_pct(self, db):
        """Position size must be <= starting_capital * max_position_pct."""
        # $10,000 capital × 5% = $500 max position
        cfg = _risk_config(starting_capital=10000.0, max_position_pct=0.05)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1

        cursor = await db.execute(
            "SELECT entry_size FROM positions ORDER BY opened_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        # Kelly(edge=0.07, odds=1.0, fraction=0.25) × $10,000 is well below $500
        assert row[0] <= 500.0, f"Position size {row[0]} exceeds max $500"

    async def test_small_bankroll_produces_small_position(self, db):
        """Tiny bankroll → tiny position, not the old hardcoded $10 cap."""
        # $100 bankroll × 5% = $5 max position
        cfg = _risk_config(starting_capital=100.0, max_position_pct=0.05)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) >= 1

        cursor = await db.execute(
            "SELECT entry_size FROM positions ORDER BY opened_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert (
            row[0] <= 5.0
        ), f"Position size {row[0]} exceeds bankroll-proportional max $5"

    async def test_old_hardcoded_cap_no_longer_applies(self, db):
        """With a large bankroll, size should scale above the old $10 hardcoded cap."""
        # $1,000,000 bankroll × 5% = $50,000 max. Kelly with small edge still > $10.
        cfg = _risk_config(
            starting_capital=1_000_000.0,
            max_position_pct=0.05,
            kelly_fraction=0.25,
            min_edge=0.02,
        )
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1

        cursor = await db.execute(
            "SELECT entry_size FROM positions ORDER BY opened_at DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None
        # With $1M bankroll and 7% edge, Kelly × bankroll >> $10
        assert (
            row[0] > 10.0
        ), f"Expected position > $10 for large bankroll, got {row[0]}"


# ── 2.6 ScheduledStrategyRunner accepts risk_config and circuit_breaker ───────


class TestScheduledRunnerAcceptsRiskConfig:
    def test_runner_accepts_risk_config(self, db):
        """ScheduledStrategyRunner.__init__ accepts risk_config parameter."""
        cfg = _risk_config()
        runner = ScheduledStrategyRunner(db, interval=120, risk_config=cfg)
        assert runner._risk_config is cfg

    def test_runner_accepts_circuit_breaker(self, db):
        """ScheduledStrategyRunner.__init__ accepts circuit_breaker parameter."""
        cb = MagicMock()
        runner = ScheduledStrategyRunner(db, interval=120, circuit_breaker=cb)
        assert runner._circuit_breaker is cb

    def test_runner_defaults_when_no_risk_config(self, db):
        """Without risk_config, ScheduledStrategyRunner creates a default."""
        runner = ScheduledStrategyRunner(db, interval=120)
        assert runner._risk_config is not None


# ── 2.7 ScheduledStrategyRunner circuit breaker integration ──────────────────


@pytest.mark.asyncio
class TestScheduledCircuitBreaker:
    async def test_halted_runner_skips_cycle(self, db):
        """When circuit breaker is halted, run_one_cycle returns no trades."""
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=True)
        cfg = _risk_config()

        runner = ScheduledStrategyRunner(
            db, interval=120, risk_config=cfg, circuit_breaker=cb
        )
        trades = await runner.run_one_cycle()
        assert len(trades) == 0

    async def test_active_runner_runs_normally(self, db):
        """When circuit breaker is not halted, run_one_cycle executes normally."""
        cb = MagicMock()
        cb.should_halt = AsyncMock(return_value=False)
        cfg = _risk_config()

        runner = ScheduledStrategyRunner(
            db, interval=120, risk_config=cfg, circuit_breaker=cb
        )
        # Just assert it runs without error (no positions in DB to trade)
        trades = await runner.run_one_cycle()
        assert isinstance(trades, list)
