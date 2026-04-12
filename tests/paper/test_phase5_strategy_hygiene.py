"""
Phase 5 tests: strategy hygiene and signal quality.

TDD: these tests define the contracts. Run before implementing to confirm
they fail, then implement until they all pass.

Covers:
  5.1  Cross-strategy market dedup — same market, keep highest signal_strength
  5.2  Consecutive-cycle dedup — skip recently-traded markets without price move
  5.3  Signal strength normalization within each strategy bucket
  5.4  Per-strategy enable flags in RiskControlConfig
  5.6  Per-strategy kill-switch based on rolling realistic PnL
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import RiskControlConfig  # noqa: E402
from scripts.paper_trading_session import (  # noqa: E402
    _cross_strategy_dedup,
    _get_strategy_rolling_pnl,
    _normalize_signal_strengths,
    detect_single_platform_opportunities,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _risk_config(**overrides):
    defaults = dict(
        starting_capital=10000.0,
        max_position_pct=0.05,
        max_daily_loss_pct=0.02,
        max_portfolio_exposure_pct=0.20,
        kelly_fraction=0.25,
        duplicate_signal_window_s=300,
        min_edge=0.005,
        consecutive_failure_limit=5,
        arb_cooldown_s=60.0,
        arb_rearm_hysteresis=0.005,
        slippage_bps=0.0,
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


def _make_opp(market_id, strategy, signal_strength, price=0.25):
    return {
        "market": {
            "id": market_id,
            "platform": "polymarket",
            "title": f"Market {market_id}",
            "yes_price": price,
            "no_price": round(1 - price, 4),
            "spread": 0.02,
            "volume_24h": 1000,
            "liquidity": 5000,
        },
        "strategy": strategy,
        "side": "BUY",
        "edge": 0.02,
        "signal_strength": signal_strength,
    }


async def _seed_market(db, market_id, price, platform="polymarket"):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO markets "
        "(id, platform, platform_id, title, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (market_id, platform, market_id, f"Title {market_id}", now, now),
    )
    await db.execute(
        "INSERT INTO market_prices "
        "(market_id, yes_price, no_price, spread, liquidity, polled_at) "
        "VALUES (?, ?, ?, 0.02, 5000, ?)",
        (market_id, price, round(1 - price, 4), now),
    )
    await db.commit()


async def _seed_closed_position(
    db,
    pos_id,
    market_id,
    strategy,
    realized_pnl,
    closed_at=None,
    entry_price_override=None,
):
    """Insert a closed realistic position for kill-switch/dedup tests."""
    now = datetime.now(timezone.utc).isoformat()
    if closed_at is None:
        closed_at = now
    entry_price = entry_price_override if entry_price_override is not None else 0.40
    exit_price = round(entry_price + 0.05, 4)
    sig_id = f"sig_{pos_id}"
    await db.execute(
        "INSERT OR IGNORE INTO signals "
        "(id, strategy, signal_type, market_id_a, model_edge, kelly_fraction, "
        "position_size_a, position_size_b, total_capital_at_risk, status, fired_at, updated_at) "
        "VALUES (?, ?, 'single_market', ?, 0.05, 0.25, 10, 10, 20, 'fired', ?, ?)",
        (sig_id, strategy, market_id, now, now),
    )
    await db.execute(
        "INSERT INTO positions "
        "(id, signal_id, market_id, strategy, side, entry_price, entry_size, "
        "exit_price, exit_size, realized_pnl, fees_paid, pnl_model, status, "
        "opened_at, closed_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'BUY', ?, 10, ?, 10, ?, 0.05, 'realistic', 'closed', ?, ?, ?)",
        (
            pos_id,
            sig_id,
            market_id,
            strategy,
            entry_price,
            exit_price,
            realized_pnl,
            now,
            closed_at,
            now,
        ),
    )
    await db.commit()


async def _seed_open_position(db, pos_id, market_id, strategy, entry_price):
    """Insert an open realistic position for consecutive-cycle dedup tests."""
    now = datetime.now(timezone.utc).isoformat()
    sig_id = f"sig_{pos_id}"
    await db.execute(
        "INSERT OR IGNORE INTO signals "
        "(id, strategy, signal_type, market_id_a, model_edge, kelly_fraction, "
        "position_size_a, position_size_b, total_capital_at_risk, status, fired_at, updated_at) "
        "VALUES (?, ?, 'single_market', ?, 0.05, 0.25, 10, 10, 20, 'fired', ?, ?)",
        (sig_id, strategy, market_id, now, now),
    )
    await db.execute(
        "INSERT INTO positions "
        "(id, signal_id, market_id, strategy, side, entry_price, entry_size, "
        "fees_paid, pnl_model, status, opened_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'BUY', ?, 10, 0.05, 'realistic', 'open', ?, ?)",
        (pos_id, sig_id, market_id, strategy, entry_price, now, now),
    )
    await db.commit()


# ── 5.1 Cross-strategy market dedup ──────────────────────────────────────────


class TestCrossStrategyDedup:
    def test_same_market_keeps_highest_signal_strength(self):
        """When same market_id appears in two strategies, keep the stronger signal."""
        opps = [
            _make_opp("mkt_A", "P3_calibration_bias", signal_strength=0.30),
            _make_opp("mkt_A", "P4_liquidity_timing", signal_strength=0.25),
        ]
        result = _cross_strategy_dedup(opps)
        assert len(result) == 1
        assert result[0]["strategy"] == "P3_calibration_bias"
        assert result[0]["signal_strength"] == 0.30

    def test_same_market_lower_strength_wins_if_higher(self):
        """The P4 entry wins if its signal_strength is higher."""
        opps = [
            _make_opp("mkt_B", "P3_calibration_bias", signal_strength=0.20),
            _make_opp("mkt_B", "P4_liquidity_timing", signal_strength=0.35),
        ]
        result = _cross_strategy_dedup(opps)
        assert len(result) == 1
        assert result[0]["strategy"] == "P4_liquidity_timing"

    def test_unique_markets_all_kept(self):
        """Markets appearing in only one strategy are unaffected."""
        opps = [
            _make_opp("mkt_C", "P3_calibration_bias", 0.30),
            _make_opp("mkt_D", "P4_liquidity_timing", 0.25),
        ]
        result = _cross_strategy_dedup(opps)
        assert len(result) == 2

    def test_empty_list_returns_empty(self):
        assert _cross_strategy_dedup([]) == []

    def test_three_strategies_same_market(self):
        """Three strategies on same market → only highest kept."""
        opps = [
            _make_opp("mkt_E", "P3_calibration_bias", 0.20),
            _make_opp("mkt_E", "P4_liquidity_timing", 0.40),
            _make_opp("mkt_E", "P5_information_latency", 0.30),
        ]
        result = _cross_strategy_dedup(opps)
        assert len(result) == 1
        assert result[0]["signal_strength"] == 0.40


# ── 5.3 Signal strength normalization ────────────────────────────────────────


class TestNormalizeSignalStrengths:
    def test_z_scores_within_strategy(self):
        """Within-strategy signal strengths are z-score normalized."""
        opps = [
            _make_opp("mkt_1", "P3_calibration_bias", 0.20),
            _make_opp("mkt_2", "P3_calibration_bias", 0.30),
            _make_opp("mkt_3", "P3_calibration_bias", 0.40),
        ]
        result = _normalize_signal_strengths(opps)
        zscores = [
            o["signal_strength_normalized"]
            for o in result
            if o["strategy"] == "P3_calibration_bias"
        ]
        assert len(zscores) == 3
        # Highest raw → highest z-score
        assert zscores[2] > zscores[1] > zscores[0]

    def test_single_item_normalized_to_zero(self):
        """Single-item strategy bucket gets signal_strength_normalized=0.0."""
        opps = [_make_opp("mkt_1", "P3_calibration_bias", 0.30)]
        result = _normalize_signal_strengths(opps)
        assert result[0]["signal_strength_normalized"] == 0.0

    def test_normalization_does_not_cross_strategies(self):
        """Normalization is computed independently per strategy."""
        opps = [
            _make_opp("mkt_1", "P3_calibration_bias", 0.30),
            _make_opp("mkt_2", "P3_calibration_bias", 0.70),
            _make_opp("mkt_3", "P5_information_latency", 0.10),
            _make_opp("mkt_4", "P5_information_latency", 0.90),
        ]
        result = _normalize_signal_strengths(opps)
        p3_z = [
            o["signal_strength_normalized"]
            for o in result
            if o["strategy"] == "P3_calibration_bias"
        ]
        p5_z = [
            o["signal_strength_normalized"]
            for o in result
            if o["strategy"] == "P5_information_latency"
        ]
        # Both groups span the same z-score range independently
        assert abs(p3_z[0] - p5_z[0]) < 0.001  # same z within each pair

    def test_original_signal_strength_preserved(self):
        """The original signal_strength field is NOT modified."""
        opps = [
            _make_opp("mkt_1", "P3_calibration_bias", 0.30),
            _make_opp("mkt_2", "P3_calibration_bias", 0.50),
        ]
        result = _normalize_signal_strengths(opps)
        assert result[0]["signal_strength"] == 0.30
        assert result[1]["signal_strength"] == 0.50

    def test_empty_list_returns_empty(self):
        assert _normalize_signal_strengths([]) == []


# ── 5.4 Strategy enable flags ─────────────────────────────────────────────────


class TestRiskConfigStrategyFlags:
    def test_strategy_flags_exist_with_defaults(self):
        """RiskControlConfig has per-strategy enable flags defaulting to True."""
        cfg = RiskControlConfig()
        assert hasattr(cfg, "strategy_p2_enabled")
        assert hasattr(cfg, "strategy_p3_enabled")
        assert hasattr(cfg, "strategy_p4_enabled")
        assert hasattr(cfg, "strategy_p5_enabled")
        assert cfg.strategy_p2_enabled is True
        assert cfg.strategy_p3_enabled is True
        assert cfg.strategy_p4_enabled is True
        assert cfg.strategy_p5_enabled is True

    def test_replay_cooldown_exists(self):
        """RiskControlConfig has strategy_replay_cooldown_s."""
        cfg = RiskControlConfig()
        assert hasattr(cfg, "strategy_replay_cooldown_s")
        assert cfg.strategy_replay_cooldown_s > 0

    def test_replay_min_move_exists(self):
        """RiskControlConfig has strategy_replay_min_move."""
        cfg = RiskControlConfig()
        assert hasattr(cfg, "strategy_replay_min_move")
        assert 0 < cfg.strategy_replay_min_move < 1.0

    def test_killswitch_fields_exist(self):
        """RiskControlConfig has kill-switch window and min-trades fields."""
        cfg = RiskControlConfig()
        assert hasattr(cfg, "strategy_killswitch_window_s")
        assert hasattr(cfg, "strategy_killswitch_min_trades")
        assert cfg.strategy_killswitch_window_s > 0
        assert cfg.strategy_killswitch_min_trades > 0


@pytest.mark.asyncio
class TestStrategyEnableFlags:
    async def test_disabled_p3_produces_no_p3_trades(self, db):
        """With strategy_p3_enabled=False, no P3_calibration_bias positions open."""
        # price=0.25 → distance_from_center=0.25 > 0.20 → P3 signal
        await _seed_market(db, "mkt_p3", 0.25)
        cfg = _risk_config(strategy_p3_enabled=False)
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy='P3_calibration_bias'"
        )
        row = await cursor.fetchone()
        assert row[0] == 0, f"Expected 0 P3 positions, got {row[0]}"

    async def test_enabled_p3_produces_trades(self, db):
        """With strategy_p3_enabled=True, P3 positions can open normally."""
        await _seed_market(db, "mkt_p3", 0.25)
        cfg = _risk_config(strategy_p3_enabled=True)
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy='P3_calibration_bias'"
        )
        row = await cursor.fetchone()
        assert row[0] >= 1

    async def test_disabled_p4_produces_no_p4_trades(self, db):
        """With strategy_p4_enabled=False, no P4_liquidity_timing positions open."""
        # price=0.25 → in (0.15, 0.35) → P4 signal
        await _seed_market(db, "mkt_p4", 0.25)
        cfg = _risk_config(strategy_p4_enabled=False)
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy='P4_liquidity_timing'"
        )
        row = await cursor.fetchone()
        assert row[0] == 0, f"Expected 0 P4 positions, got {row[0]}"


# ── 5.2 Consecutive-cycle dedup ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestConsecutiveCycleDedup:
    async def test_market_with_open_position_is_skipped(self, db):
        """A market already holding an open position is not traded again."""
        await _seed_market(db, "mkt_open", 0.25)
        await _seed_open_position(
            db, "pos_open", "mkt_open", "P3_calibration_bias", 0.25
        )

        cfg = _risk_config(
            strategy_replay_cooldown_s=300, strategy_replay_min_move=0.01
        )
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        # Only the one pre-existing open position, no new one added
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id='mkt_open'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1, f"Expected 1 position (the seeded one), got {row[0]}"

    async def test_recently_closed_market_without_price_move_skipped(self, db):
        """Market closed recently with no significant price move → not re-traded."""
        entry_price = 0.25
        await _seed_market(db, "mkt_recent", entry_price)  # same price as entry
        closed_at = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        await _seed_closed_position(
            db,
            "pos_recent",
            "mkt_recent",
            "P3_calibration_bias",
            realized_pnl=0.05,
            closed_at=closed_at,
            entry_price_override=entry_price,  # price hasn't moved
        )

        cfg = _risk_config(
            strategy_replay_cooldown_s=300,
            strategy_replay_min_move=0.01,  # 1% move required
        )
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        # Only the 1 closed position; no new open position
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id='mkt_recent'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1, f"Expected 1 (seeded), got {row[0]}"

    async def test_recently_closed_with_price_move_allowed(self, db):
        """Market closed recently BUT price moved enough → re-entry allowed."""
        entry_price = 0.25
        # Current price = 0.20, which is 20% move from 0.25 → > 1% min_move
        await _seed_market(db, "mkt_moved", 0.20)
        closed_at = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        await _seed_closed_position(
            db,
            "pos_moved",
            "mkt_moved",
            "P3_calibration_bias",
            realized_pnl=0.05,
            closed_at=closed_at,
            entry_price_override=entry_price,
        )

        cfg = _risk_config(
            strategy_replay_cooldown_s=300,
            strategy_replay_min_move=0.01,
        )
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        # New open position should have been created
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id='mkt_moved' AND status='open'"
        )
        row = await cursor.fetchone()
        assert row[0] >= 1, "Expected re-entry after sufficient price move"

    async def test_market_outside_cooldown_window_allowed(self, db):
        """Market traded longer ago than cooldown window can re-enter freely."""
        await _seed_market(db, "mkt_old", 0.25)
        # Closed 600 seconds ago, well outside 300s cooldown
        closed_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
        await _seed_closed_position(
            db,
            "pos_old",
            "mkt_old",
            "P3_calibration_bias",
            realized_pnl=0.05,
            closed_at=closed_at,
        )

        cfg = _risk_config(
            strategy_replay_cooldown_s=300, strategy_replay_min_move=0.01
        )
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        # A new open position should appear
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id='mkt_old' AND status='open'"
        )
        row = await cursor.fetchone()
        assert row[0] >= 1, "Expected re-entry for market outside cooldown window"


# ── 5.6 Per-strategy kill-switch ──────────────────────────────────────────────


@pytest.mark.asyncio
class TestStrategyKillSwitch:
    async def test_get_strategy_rolling_pnl_empty(self, db):
        """With no positions, rolling PnL is (0, 0.0)."""
        count, pnl = await _get_strategy_rolling_pnl(db, "P3_calibration_bias", 604800)
        assert count == 0
        assert pnl == 0.0

    async def test_get_strategy_rolling_pnl_sums_correctly(self, db):
        """Rolling PnL sums realistic closed positions within window."""
        await _seed_market(db, "mkt_pnl1", 0.40)
        await _seed_market(db, "mkt_pnl2", 0.45)
        await _seed_closed_position(
            db, "ks_pos1", "mkt_pnl1", "P3_calibration_bias", -0.50
        )
        await _seed_closed_position(
            db, "ks_pos2", "mkt_pnl2", "P3_calibration_bias", -0.30
        )

        count, pnl = await _get_strategy_rolling_pnl(db, "P3_calibration_bias", 604800)
        assert count == 2
        assert abs(pnl - (-0.80)) < 0.001

    async def test_negative_pnl_kills_strategy_when_min_trades_met(self, db):
        """Strategy with negative rolling PnL and ≥ min_trades is disabled for cycle."""
        for i in range(5):
            await _seed_market(db, f"mkt_kill_{i}", 0.40)
            await _seed_closed_position(
                db,
                f"ks_kill_{i}",
                f"mkt_kill_{i}",
                "P3_calibration_bias",
                -0.50,
            )

        # P3 trigger market — would normally fire
        await _seed_market(db, "mkt_trigger", 0.25)

        cfg = _risk_config(
            strategy_p3_enabled=True,  # explicitly enabled
            strategy_killswitch_window_s=604800,
            strategy_killswitch_min_trades=5,  # exactly 5 trades triggers kill
        )
        await detect_single_platform_opportunities(db, max_trades=10, risk_config=cfg)

        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions "
            "WHERE strategy='P3_calibration_bias' AND market_id='mkt_trigger'"
        )
        row = await cursor.fetchone()
        assert (
            row[0] == 0
        ), f"Kill-switch should have blocked P3 trade, got {row[0]} positions"

    async def test_insufficient_trades_does_not_trigger_killswitch(self, db):
        """Strategy with negative PnL but fewer than min_trades is NOT killed."""
        # Only 2 negative trades, min_trades=5 → kill-switch should NOT fire
        for i in range(2):
            await _seed_market(db, f"mkt_few_{i}", 0.40)
            await _seed_closed_position(
                db,
                f"ks_few_{i}",
                f"mkt_few_{i}",
                "P3_calibration_bias",
                -0.50,
            )

        await _seed_market(db, "mkt_few_trigger", 0.25)

        cfg = _risk_config(
            strategy_killswitch_min_trades=5,
        )
        await detect_single_platform_opportunities(db, max_trades=10, risk_config=cfg)

        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions "
            "WHERE strategy='P3_calibration_bias' AND market_id='mkt_few_trigger'"
        )
        row = await cursor.fetchone()
        assert row[0] >= 1, "Expected trade when min_trades threshold not reached"

    async def test_positive_rolling_pnl_does_not_kill(self, db):
        """Strategy with positive rolling PnL is never killed."""
        for i in range(5):
            await _seed_market(db, f"mkt_pos_{i}", 0.40)
            await _seed_closed_position(
                db,
                f"ks_pos_{i}",
                f"mkt_pos_{i}",
                "P3_calibration_bias",
                +0.50,
            )

        await _seed_market(db, "mkt_pos_trigger", 0.25)

        cfg = _risk_config(strategy_killswitch_min_trades=5)
        await detect_single_platform_opportunities(db, max_trades=10, risk_config=cfg)

        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions "
            "WHERE strategy='P3_calibration_bias' AND market_id='mkt_pos_trigger'"
        )
        row = await cursor.fetchone()
        assert row[0] >= 1, "Positive PnL strategy should still trade"
