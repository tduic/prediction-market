"""
Phase 3 tests: re-arm state machine for ArbitrageEngine.

TDD: these tests define the contracts. Run before implementing to confirm
they fail, then implement until they all pass.

Covers:
  3.1  PairFireState dataclass with last_fired_at, armed, last_spread_seen_below
  3.2  arb_cooldown_s and arb_rearm_hysteresis added to RiskControlConfig
  3.3  ArbitrageEngine replaces recently_fired: set with fired_state: dict
  3.4  recently_fired property for backward compatibility
  3.5  Fire eligibility: armed=True AND cooldown elapsed
  3.6  Re-arm trigger: spread < min_spread - hysteresis → armed = True
  3.7  Exception safety: failed trade rolls back fired_state
  3.8  Periodic scan rescans all pairs independent of tick deltas
  3.9  Lock scope: IO (circuit breaker, risk checks, orders) outside the lock
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import RiskControlConfig  # noqa: E402
from scripts.paper_trading_session import (  # noqa: E402
    ArbitrageEngine,
    PairFireState,
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
    """RiskControlConfig with sensible test defaults."""
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
    )
    defaults.update(overrides)
    cfg = RiskControlConfig.__new__(RiskControlConfig)
    for k, v in defaults.items():
        object.__setattr__(cfg, k, v)
    return cfg


# Prices: kal_A at 0.58, trigger poly_A to 0.51 → spread=0.07 (under PnL cap)
_POLY_SEED = 0.50
_KAL_SEED = 0.58
_POLY_TRIGGER = 0.51  # delta=0.01 > 0.001 guard


# ── 3.1 PairFireState dataclass ───────────────────────────────────────────────


class TestPairFireState:
    def test_pairfirestate_exists(self):
        """PairFireState is importable from paper_trading_session."""
        assert PairFireState is not None

    def test_pairfirestate_fields(self):
        """PairFireState has last_fired_at, armed, and last_spread_seen_below."""
        state = PairFireState(last_fired_at=time.time(), armed=True)
        assert hasattr(state, "last_fired_at")
        assert hasattr(state, "armed")
        assert hasattr(state, "last_spread_seen_below")

    def test_pairfirestate_defaults(self):
        """last_spread_seen_below defaults to None."""
        state = PairFireState(last_fired_at=0.0, armed=True)
        assert state.last_spread_seen_below is None

    def test_pairfirestate_armed_false(self):
        """Can create a disarmed state (just fired)."""
        state = PairFireState(last_fired_at=time.time(), armed=False)
        assert state.armed is False


# ── 3.2 arb_cooldown_s and arb_rearm_hysteresis in RiskControlConfig ─────────


class TestRiskConfigNewFields:
    def test_arb_cooldown_s_has_default(self):
        """RiskControlConfig.arb_cooldown_s exists with a sensible default."""
        cfg = RiskControlConfig()
        assert hasattr(cfg, "arb_cooldown_s")
        assert cfg.arb_cooldown_s > 0

    def test_arb_rearm_hysteresis_has_default(self):
        """RiskControlConfig.arb_rearm_hysteresis exists with a sensible default."""
        cfg = RiskControlConfig()
        assert hasattr(cfg, "arb_rearm_hysteresis")
        assert 0 < cfg.arb_rearm_hysteresis < 0.05

    def test_arb_cooldown_s_default_is_60(self):
        """Default cooldown is 60 seconds."""
        import os

        # Temporarily unset env var if present
        old = os.environ.pop("ARB_COOLDOWN_S", None)
        try:
            cfg = RiskControlConfig()
            assert cfg.arb_cooldown_s == 60.0
        finally:
            if old is not None:
                os.environ["ARB_COOLDOWN_S"] = old

    def test_arb_rearm_hysteresis_default_is_0005(self):
        """Default hysteresis is 0.005."""
        import os

        old = os.environ.pop("ARB_REARM_HYSTERESIS", None)
        try:
            cfg = RiskControlConfig()
            assert cfg.arb_rearm_hysteresis == 0.005
        finally:
            if old is not None:
                os.environ["ARB_REARM_HYSTERESIS"] = old


# ── 3.3 ArbitrageEngine has fired_state dict ─────────────────────────────────


@pytest.mark.asyncio
class TestFiredStateAttribute:
    async def test_fired_state_is_dict(self, db):
        """ArbitrageEngine.fired_state is a dict."""
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert isinstance(engine.fired_state, dict)

    async def test_fired_state_empty_at_init(self, db):
        """fired_state starts empty."""
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert len(engine.fired_state) == 0

    async def test_fired_state_populated_after_trade(self, db):
        """After a trade fires, pair_id appears in fired_state."""
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)

        assert "poly_A_kal_A" in engine.fired_state
        state = engine.fired_state["poly_A_kal_A"]
        assert isinstance(state, PairFireState)
        assert state.last_fired_at > 0

    async def test_fired_state_armed_false_immediately_after_trade(self, db):
        """Immediately after firing, pair is NOT re-armed (armed=False)."""
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)

        assert engine.fired_state["poly_A_kal_A"].armed is False


# ── 3.4 recently_fired backward compatibility ─────────────────────────────────


@pytest.mark.asyncio
class TestRecentlyFiredBackwardCompat:
    async def test_recently_fired_is_set_like(self, db):
        """engine.recently_fired behaves like a set (supports 'in' operator)."""
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert "poly_A_kal_A" in engine.recently_fired

    async def test_recently_fired_empty_before_trade(self, db):
        """recently_fired is empty before any trades."""
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert len(engine.recently_fired) == 0

    async def test_recently_fired_len_after_trade(self, db):
        """len(recently_fired) reflects number of pairs that have fired."""
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.recently_fired) >= 1


# ── 3.5 Fire eligibility: cooldown + armed ────────────────────────────────────


@pytest.mark.asyncio
class TestFireEligibility:
    async def test_pair_not_eligible_during_cooldown(self, db):
        """After a trade, pair is NOT eligible again until cooldown expires."""
        # Use a long cooldown (999s) so no retry is possible in the test window
        cfg = _risk_config(arb_cooldown_s=999.0, arb_rearm_hysteresis=0.005)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        # Fire first trade
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1

        # Price update that would otherwise trigger another trade
        await _simulate_price_update(engine, db, "poly_A", 0.52)  # delta = 0.01
        assert len(engine.trades) == 1, "Second trade fired before cooldown expired"

    async def test_pair_eligible_after_cooldown_and_rearm(self, db):
        """After cooldown expires AND spread reverts, the pair fires again."""
        # Use zero cooldown and zero hysteresis so re-arm is immediate
        cfg = _risk_config(arb_cooldown_s=0.0, arb_rearm_hysteresis=0.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        # First trade
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1

        # Revert spread below min_spread (triggers re-arm)
        await _simulate_price_update(engine, db, "poly_A", _KAL_SEED - 0.01)

        # Spread reopens → should fire again
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 2, "Expected pair to re-fire after cooldown+rearm"

    async def test_initial_pair_has_no_cooldown(self, db):
        """Brand-new pairs (never fired) are immediately eligible."""
        cfg = _risk_config(arb_cooldown_s=60.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        # Should fire on first trigger (no cooldown for never-fired pairs)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1


# ── 3.6 Re-arm trigger ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRearmTrigger:
    async def test_spread_reversion_arms_pair(self, db):
        """When spread drops below min_spread - hysteresis, armed becomes True."""
        cfg = _risk_config(arb_cooldown_s=0.0, arb_rearm_hysteresis=0.005)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.05, risk_config=cfg)

        # Fire first trade (spread = _KAL_SEED - _POLY_TRIGGER = 0.07 > 0.05)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1
        assert engine.fired_state["poly_A_kal_A"].armed is False

        # Revert spread below min_spread(0.05) - hysteresis(0.005) = 0.045
        # If poly moves to 0.55, spread = 0.58-0.55 = 0.03 < 0.045 → re-arm
        await _simulate_price_update(engine, db, "poly_A", 0.55)
        assert engine.fired_state["poly_A_kal_A"].armed is True

    async def test_spread_above_threshold_does_not_arm(self, db):
        """When spread stays above min_spread - hysteresis, pair stays disarmed."""
        cfg = _risk_config(arb_cooldown_s=999.0, arb_rearm_hysteresis=0.005)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.05, risk_config=cfg)

        # Fire first trade
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1
        assert engine.fired_state["poly_A_kal_A"].armed is False

        # Spread is still 0.06 (0.58-0.52), which is > 0.05-0.005=0.045 → NOT re-armed
        await _simulate_price_update(engine, db, "poly_A", 0.52)
        assert engine.fired_state["poly_A_kal_A"].armed is False

    async def test_never_fired_pair_treated_as_armed(self, db):
        """A pair that has never fired is always treated as eligible."""
        cfg = _risk_config(arb_cooldown_s=999.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        # Never-fired pair should fire on first valid tick
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1


# ── 3.7 Exception safety ──────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestExceptionSafety:
    async def test_exception_rolls_back_fired_state(self, db):
        """If _execute_arb_trade raises an exception, fired_state is rolled back."""
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(
            db, matches, min_spread=0.03, risk_config=_risk_config()
        )

        # Monkey-patch _execute_arb_trade to raise
        original = engine._execute_arb_trade

        async def _failing_execute(*args, **kwargs):
            raise RuntimeError("Simulated execution failure")

        engine._execute_arb_trade = _failing_execute

        # Should not raise (exception is caught internally)
        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)

        # pair_id should NOT be in fired_state (state was rolled back)
        assert "poly_A_kal_A" not in engine.fired_state

        # Restore original and verify pair can trade again
        engine._execute_arb_trade = original
        await _simulate_price_update(engine, db, "poly_A", 0.52)
        assert len(engine.trades) >= 1

    async def test_failed_risk_check_does_not_enter_cooldown(self, db):
        """A risk-check rejection should NOT put pair into cooldown."""
        # Impossibly high min_edge → risk checks always fail
        cfg = _risk_config(min_edge=0.99, arb_cooldown_s=999.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)

        # No trades AND pair not in fired_state (risk rejection ≠ cooldown)
        assert len(engine.trades) == 0
        assert "poly_A_kal_A" not in engine.fired_state


# ── 3.8 stats() reflects current muted count ─────────────────────────────────


@pytest.mark.asyncio
class TestStatsWithFireState:
    async def test_stats_recently_fired_is_muted_count(self, db):
        """stats()['recently_fired'] returns count of currently-muted pairs."""
        cfg = _risk_config(arb_cooldown_s=999.0)  # long cooldown = stays muted
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        assert engine.stats()["recently_fired"] == 0

        await _simulate_price_update(engine, db, "poly_A", _POLY_TRIGGER)
        assert len(engine.trades) == 1
        # Immediately after trade: pair is in cooldown → muted count = 1
        assert engine.stats()["recently_fired"] == 1


# ── 3.9 Periodic drift scan ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPeriodicScan:
    async def test_periodic_scan_method_exists(self, db):
        """ArbitrageEngine has a periodic_scan() async method."""
        engine = ArbitrageEngine(db, [], min_spread=0.03)
        assert hasattr(engine, "periodic_scan")
        assert callable(engine.periodic_scan)

    async def test_periodic_scan_fires_eligible_pairs(self, db):
        """periodic_scan() trades pairs that are eligible regardless of tick order."""
        cfg = _risk_config()
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        # Set prices manually (as if WS delivered them without triggering on_price_update)
        engine.prices["poly_A"] = _POLY_TRIGGER
        engine.prices["kal_A"] = _KAL_SEED

        # No trade yet (on_price_update was never called)
        assert len(engine.trades) == 0

        # periodic_scan should detect and trade the pair
        await engine.periodic_scan()
        assert len(engine.trades) == 1

    async def test_periodic_scan_respects_cooldown(self, db):
        """periodic_scan() does not re-trade pairs still in cooldown."""
        cfg = _risk_config(arb_cooldown_s=999.0)
        matches = [_make_match("poly_A", "kal_A", _POLY_SEED, _KAL_SEED)]
        await _seed_markets(db, matches)
        engine = ArbitrageEngine(db, matches, min_spread=0.03, risk_config=cfg)

        # Set prices and run scan (first trade)
        engine.prices["poly_A"] = _POLY_TRIGGER
        engine.prices["kal_A"] = _KAL_SEED
        await engine.periodic_scan()
        assert len(engine.trades) == 1

        # Run scan again — should NOT trade (still in cooldown)
        await engine.periodic_scan()
        assert len(engine.trades) == 1
