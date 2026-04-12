"""
Phase 4 tests: realistic paper fill model.

TDD: these tests define the contracts. Run before implementing to confirm
they fail, then implement until they all pass.

Covers:
  4.1  Single-platform positions open as status='open' (no synthetic exit)
  4.2  No exit_price or realized_pnl written at position open
  4.3  mark_and_close_positions updates current_price and unrealized_pnl
  4.4  Time-based exit closes expired positions with realized_pnl
  4.5  Slippage model in paper execution client
  4.6  Fee rates configurable via RiskControlConfig
  4.7  pnl_model column exists; new positions default to 'realistic'
  4.8  ScheduledStrategyRunner.run_one_cycle() calls mark-to-market pass
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.config import RiskControlConfig  # noqa: E402
from execution.clients.paper import PaperExecutionClient  # noqa: E402
from execution.models import OrderLeg  # noqa: E402
from scripts.paper_trading_session import (  # noqa: E402
    ScheduledStrategyRunner,
    detect_single_platform_opportunities,
    mark_and_close_positions,
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


async def _seed_p3_market(db):
    """Insert a single market that triggers P3_calibration_bias (price 0.20 from center)."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
        "VALUES ('mkt_p3', 'polymarket', 'p3', 'P3 market', 'open', ?, ?)",
        (now, now),
    )
    # price=0.25 → distance_from_center=0.25 > 0.20 → triggers P3
    await db.execute(
        "INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at) "
        "VALUES ('mkt_p3', 0.25, 0.75, 0.02, 5000, ?)",
        (now,),
    )
    await db.commit()


async def _insert_open_position(
    db,
    pos_id,
    market_id,
    side="BUY",
    entry_price=0.40,
    size=10.0,
    opened_at=None,
    pnl_model="realistic",
):
    """Insert an open position into the positions table."""
    if opened_at is None:
        opened_at = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    sig_id = f"sig_{pos_id}"
    # signals row required by FK constraint
    await db.execute(
        """INSERT OR IGNORE INTO signals
           (id, strategy, signal_type, market_id_a, model_edge, kelly_fraction,
            position_size_a, position_size_b, total_capital_at_risk, status,
            fired_at, updated_at)
           VALUES (?, 'P3_calibration_bias', 'single_market', ?, 0.05, 0.25,
                   ?, ?, ?, 'fired', ?, ?)""",
        (sig_id, market_id, size, size, size * 2, now, now),
    )
    await db.execute(
        """INSERT INTO positions
           (id, signal_id, market_id, strategy, side, entry_price, entry_size,
            fees_paid, pnl_model, status, opened_at, updated_at)
           VALUES (?, ?, ?, 'P3_calibration_bias', ?, ?, ?, 0.02, ?, 'open', ?, ?)""",
        (pos_id, sig_id, market_id, side, entry_price, size, pnl_model, opened_at, now),
    )
    await db.commit()


# ── 4.1/4.2 Positions open as 'open', no synthetic exit ───────────────────────


@pytest.mark.asyncio
class TestPositionOpensAsOpen:
    async def test_single_platform_writes_open_status(self, db):
        """detect_single_platform_opportunities writes status='open', not 'closed'."""
        await _seed_p3_market(db)
        cfg = _risk_config()
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        cursor = await db.execute("SELECT status FROM positions LIMIT 1")
        row = await cursor.fetchone()
        assert row is not None, "No position written"
        assert row[0] == "open", f"Expected status='open', got '{row[0]}'"

    async def test_single_platform_no_exit_price_at_open(self, db):
        """Positions opened by detect_single_platform_opportunities have exit_price=NULL."""
        await _seed_p3_market(db)
        cfg = _risk_config()
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        cursor = await db.execute("SELECT exit_price FROM positions LIMIT 1")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None, f"Expected exit_price=NULL, got {row[0]}"

    async def test_single_platform_no_realized_pnl_at_open(self, db):
        """Positions opened by detect_single_platform_opportunities have realized_pnl=NULL."""
        await _seed_p3_market(db)
        cfg = _risk_config()
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        cursor = await db.execute("SELECT realized_pnl FROM positions LIMIT 1")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None, f"Expected realized_pnl=NULL, got {row[0]}"

    async def test_single_platform_no_closed_at_at_open(self, db):
        """Positions opened by detect_single_platform_opportunities have closed_at=NULL."""
        await _seed_p3_market(db)
        cfg = _risk_config()
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        cursor = await db.execute("SELECT closed_at FROM positions LIMIT 1")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None, f"Expected closed_at=NULL, got {row[0]}"


# ── 4.3 mark_and_close_positions — mark-to-market update ──────────────────────


@pytest.mark.asyncio
class TestMarkToMarket:
    async def test_expired_position_gets_closed(self, db):
        """mark_and_close_positions closes positions past holding period."""
        now_dt = datetime.now(timezone.utc)
        old_opened = (now_dt - timedelta(seconds=400)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES ('mkt1', 'polymarket', 'p1', 'T1', 'open', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at) "
            "VALUES ('mkt1', 0.55, 0.45, 0.02, 5000, ?)",
            (now,),
        )
        await _insert_open_position(db, "pos1", "mkt1", opened_at=old_opened)

        closed = await mark_and_close_positions(db, holding_period_s=300)
        assert closed == 1

        cursor = await db.execute("SELECT status FROM positions WHERE id='pos1'")
        row = await cursor.fetchone()
        assert row[0] == "closed"

    async def test_expired_position_has_realized_pnl(self, db):
        """After closing, position has a non-NULL realized_pnl."""
        now_dt = datetime.now(timezone.utc)
        old_opened = (now_dt - timedelta(seconds=400)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES ('mkt2', 'polymarket', 'p2', 'T2', 'open', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at) "
            "VALUES ('mkt2', 0.55, 0.45, 0.02, 5000, ?)",
            (now,),
        )
        await _insert_open_position(
            db,
            "pos2",
            "mkt2",
            side="BUY",
            entry_price=0.40,
            size=10.0,
            opened_at=old_opened,
        )

        await mark_and_close_positions(db, holding_period_s=300)

        cursor = await db.execute(
            "SELECT realized_pnl, exit_price FROM positions WHERE id='pos2'"
        )
        row = await cursor.fetchone()
        assert row[0] is not None, "realized_pnl should be non-NULL after close"
        assert row[1] is not None, "exit_price should be non-NULL after close"

    async def test_recent_position_stays_open(self, db):
        """Positions within holding period are NOT closed."""
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES ('mkt3', 'polymarket', 'p3', 'T3', 'open', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at) "
            "VALUES ('mkt3', 0.55, 0.45, 0.02, 5000, ?)",
            (now,),
        )
        # opened_at = now → only 0 seconds old, well within 300s holding period
        await _insert_open_position(db, "pos3", "mkt3")

        closed = await mark_and_close_positions(db, holding_period_s=300)
        assert closed == 0

        cursor = await db.execute("SELECT status FROM positions WHERE id='pos3'")
        row = await cursor.fetchone()
        assert row[0] == "open"

    async def test_no_price_data_skips_close(self, db):
        """Positions with no current price data are left open (stale handling)."""
        now_dt = datetime.now(timezone.utc)
        old_opened = (now_dt - timedelta(seconds=400)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES ('mkt4', 'polymarket', 'p4', 'T4', 'open', ?, ?)",
            (now, now),
        )
        # No market_prices row for mkt4
        await _insert_open_position(db, "pos4", "mkt4", opened_at=old_opened)

        closed = await mark_and_close_positions(db, holding_period_s=300)
        assert closed == 0

        cursor = await db.execute("SELECT status FROM positions WHERE id='pos4'")
        row = await cursor.fetchone()
        assert row[0] == "open"

    async def test_buy_realized_pnl_correct(self, db):
        """BUY position: realized_pnl = (exit_price - entry_price) * size - fees."""
        now_dt = datetime.now(timezone.utc)
        old_opened = (now_dt - timedelta(seconds=400)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES ('mkt5', 'polymarket', 'p5', 'T5', 'open', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at) "
            "VALUES ('mkt5', 0.60, 0.40, 0.02, 5000, ?)",
            (now,),
        )
        # BUY at 0.40, current price 0.60, size 10, fees 0.02
        await _insert_open_position(
            db,
            "pos5",
            "mkt5",
            side="BUY",
            entry_price=0.40,
            size=10.0,
            opened_at=old_opened,
        )
        # Override fees_paid via direct update
        await db.execute("UPDATE positions SET fees_paid=0.02 WHERE id='pos5'")
        await db.commit()

        await mark_and_close_positions(db, holding_period_s=300)

        cursor = await db.execute("SELECT realized_pnl FROM positions WHERE id='pos5'")
        row = await cursor.fetchone()
        # (0.60 - 0.40) * 10 - 0.02 = 2.0 - 0.02 = 1.98
        assert abs(row[0] - 1.98) < 0.001, f"Expected ~1.98, got {row[0]}"


# ── 4.5 Slippage model ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSlippageModel:
    async def _make_client(self, db, slippage_bps=25.0, platform="polymarket"):
        return PaperExecutionClient(
            db, platform_label=f"paper_{platform}", slippage_bps=slippage_bps
        )

    async def _seed_market_price(self, db, market_id, price):
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO markets "
            "(id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES (?, 'polymarket', ?, 'T', 'open', ?, ?)",
            (market_id, market_id, now, now),
        )
        await db.execute(
            "INSERT INTO market_prices "
            "(market_id, yes_price, no_price, spread, liquidity, polled_at) "
            "VALUES (?, ?, ?, 0.02, 5000, ?)",
            (market_id, price, round(1 - price, 4), now),
        )
        await db.commit()

    async def test_buy_fills_at_or_above_market_price(self, db):
        """BUY with slippage fills at >= market_price (adverse movement)."""
        await self._seed_market_price(db, "mkt_slip_buy", 0.50)
        client = await self._make_client(db, slippage_bps=100.0)  # large slippage
        leg = OrderLeg(
            market_id="mkt_slip_buy",
            platform="polymarket",
            side="BUY",
            size=10.0,
            limit_price=0.60,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg)
        assert result.status == "filled"
        assert (
            result.filled_price >= 0.50
        ), f"BUY slippage should fill >= market price, got {result.filled_price}"

    async def test_sell_fills_at_or_below_market_price(self, db):
        """SELL with slippage fills at <= market_price (adverse movement)."""
        await self._seed_market_price(db, "mkt_slip_sell", 0.50)
        client = await self._make_client(db, slippage_bps=100.0)  # large slippage
        leg = OrderLeg(
            market_id="mkt_slip_sell",
            platform="polymarket",
            side="SELL",
            size=10.0,
            limit_price=0.40,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg)
        assert result.status == "filled"
        assert (
            result.filled_price <= 0.50
        ), f"SELL slippage should fill <= market price, got {result.filled_price}"

    async def test_zero_slippage_fills_exactly_at_market(self, db):
        """With slippage_bps=0, fill price equals market price exactly."""
        await self._seed_market_price(db, "mkt_zero_slip", 0.50)
        client = await self._make_client(db, slippage_bps=0.0)
        leg = OrderLeg(
            market_id="mkt_zero_slip",
            platform="polymarket",
            side="BUY",
            size=10.0,
            limit_price=0.60,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg)
        assert result.status == "filled"
        assert result.filled_price == pytest.approx(0.50, abs=1e-6)

    async def test_default_client_has_slippage_zero(self, db):
        """PaperExecutionClient created without slippage_bps defaults to 0 (backward compat)."""
        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        assert client.slippage_bps == 0.0


# ── 4.6 Fee rates configurable ───────────────────────────────────────────────


@pytest.mark.asyncio
class TestFeeRates:
    async def _seed_market_price(self, db, market_id, price):
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO markets "
            "(id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES (?, 'polymarket', ?, 'T', 'open', ?, ?)",
            (market_id, market_id, now, now),
        )
        await db.execute(
            "INSERT INTO market_prices "
            "(market_id, yes_price, no_price, spread, liquidity, polled_at) "
            "VALUES (?, ?, ?, 0.02, 5000, ?)",
            (market_id, price, round(1 - price, 4), now),
        )
        await db.commit()

    async def test_custom_fee_rate_applied(self, db):
        """PaperExecutionClient respects a custom fee_rate parameter."""
        await self._seed_market_price(db, "mkt_fee", 0.50)
        client = PaperExecutionClient(
            db, platform_label="paper_polymarket", fee_rate=0.05
        )
        leg = OrderLeg(
            market_id="mkt_fee",
            platform="polymarket",
            side="BUY",
            size=10.0,
            limit_price=0.60,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg)
        assert result.status == "filled"
        # fee = size * price * rate = 10 * 0.50 * 0.05 = 0.25
        assert (
            abs(result.fee_paid - 0.25) < 0.01
        ), f"Expected fee ~0.25 at 5% rate, got {result.fee_paid}"


# ── 4.7 pnl_model column ─────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPnlModelColumn:
    async def test_pnl_model_column_exists(self, db):
        """positions table has a pnl_model column after migration."""
        cursor = await db.execute("PRAGMA table_info(positions)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "pnl_model" in cols, "positions.pnl_model column missing"

    async def test_new_realistic_position_default(self, db):
        """Positions written by detect_single_platform have pnl_model='realistic'."""
        await _seed_p3_market(db)
        cfg = _risk_config()
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)

        cursor = await db.execute(
            "SELECT pnl_model FROM positions WHERE pnl_model IS NOT NULL LIMIT 1"
        )
        row = await cursor.fetchone()
        assert row is not None, "No position with pnl_model written"
        assert row[0] == "realistic", f"Expected 'realistic', got '{row[0]}'"

    async def test_risk_config_has_slippage_bps(self):
        """RiskControlConfig has slippage_bps field."""
        cfg = RiskControlConfig()
        assert hasattr(cfg, "slippage_bps")
        assert cfg.slippage_bps >= 0

    async def test_risk_config_has_holding_period(self):
        """RiskControlConfig has strategy_holding_period_s field."""
        cfg = RiskControlConfig()
        assert hasattr(cfg, "strategy_holding_period_s")
        assert cfg.strategy_holding_period_s > 0


# ── 4.8 ScheduledStrategyRunner calls mark-to-market ─────────────────────────


@pytest.mark.asyncio
class TestScheduledRunnerMarkToMarket:
    async def test_run_one_cycle_closes_expired_positions(self, db):
        """run_one_cycle() triggers mark_and_close_positions for expired open positions."""
        now_dt = datetime.now(timezone.utc)
        old_opened = (now_dt - timedelta(seconds=400)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES ('mkt_sched', 'polymarket', 'ms', 'Sched Market', 'open', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO market_prices "
            "(market_id, yes_price, no_price, spread, liquidity, polled_at) "
            "VALUES ('mkt_sched', 0.55, 0.45, 0.02, 5000, ?)",
            (now,),
        )
        await _insert_open_position(db, "pos_sched", "mkt_sched", opened_at=old_opened)

        cfg = _risk_config(strategy_holding_period_s=300)
        runner = ScheduledStrategyRunner(db, interval=120, risk_config=cfg)
        await runner.run_one_cycle()

        cursor = await db.execute("SELECT status FROM positions WHERE id='pos_sched'")
        row = await cursor.fetchone()
        assert (
            row[0] == "closed"
        ), f"Expected run_one_cycle to close expired position, got status='{row[0]}'"

    async def test_run_one_cycle_returns_list(self, db):
        """run_one_cycle() still returns a list (backward compat)."""
        cfg = _risk_config()
        runner = ScheduledStrategyRunner(db, interval=120, risk_config=cfg)
        result = await runner.run_one_cycle()
        assert isinstance(result, list)
