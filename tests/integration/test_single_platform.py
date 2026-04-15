"""
Tests for core/strategies/single_platform.py

Covers:
  - P2 (structured event): same-platform series over-sum detection
  - P3 (calibration bias): extreme price triggers, side direction
  - P4 (liquidity timing): price in transition zones, side direction
  - P5 (information latency): wide spread + extreme price
  - Quota allocation: per-strategy slot caps
  - _p2_title_root: suffix stripping for series grouping
  - Fill model: positions open as 'open', no synthetic exit at open time
  - Mark-to-market: expired positions closed with realized_pnl
  - Slippage model in PaperExecutionClient
  - Fee rates configurable via RiskControlConfig
  - pnl_model column
  - ScheduledStrategyRunner.run_one_cycle() calls mark-to-market pass
  - Cross-strategy market dedup
  - Consecutive-cycle dedup
  - Signal strength normalization
  - Per-strategy enable flags
  - Per-strategy kill-switch based on rolling realistic PnL
"""

import ast
import re as _re
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_SRC = (PROJECT_ROOT / "scripts" / "paper_trading_session.py").read_text()
_TREE = ast.parse(_SRC)


def _extract_func(name):
    node = next(
        n
        for n in ast.walk(_TREE)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    )
    lines = _SRC.splitlines()[node.lineno - 1 : node.end_lineno]
    return textwrap.dedent("\n".join(lines))


_root_src = _extract_func("_p2_title_root")

_STRIP_SUFFIX = _re.compile(
    r"\s+(?:(january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
    r"|20\d{2}|q[1-4]|h[1-2]|"
    r"\$?[\d,]+\.?\d*[km%]?(?:\s*[-\u2013to]+\s*\$?[\d,]+\.?\d*[km%]?)?)"
    r"\s*$",
    _re.IGNORECASE,
)
_ns = {"_STRIP_SUFFIX": _STRIP_SUFFIX, "re": _re}
_compiled = compile(_root_src, "<_p2_title_root>", "exec")
exec(_compiled, _ns)  # noqa: S102
_p2_title_root = _ns["_p2_title_root"]

from core.config import RiskControlConfig  # noqa: E402
from core.engine import ScheduledStrategyRunner  # noqa: E402
from core.strategies.single_platform import (  # noqa: E402
    _cross_strategy_dedup,
    _get_strategy_rolling_pnl,
    _normalize_signal_strengths,
    detect_single_platform_opportunities,
    mark_and_close_positions,
)
from execution.clients.paper import PaperExecutionClient  # noqa: E402
from execution.models import OrderLeg  # noqa: E402

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


async def _seed_market(
    db,
    market_id,
    platform="polymarket",
    title=None,
    yes_price=0.25,
    spread=0.02,
    liquidity=10000.0,
):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO markets "
        "(id, platform, platform_id, title, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (market_id, platform, market_id, title or f"Title {market_id}", now, now),
    )
    await db.execute(
        "INSERT INTO market_prices "
        "(market_id, yes_price, no_price, spread, liquidity, polled_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (market_id, yes_price, round(1 - yes_price, 4), spread, liquidity, now),
    )
    await db.commit()


async def _seed_p3_market(db):
    """Insert a single market that triggers P3_calibration_bias (price 0.20 from center)."""
    await _seed_market(db, "mkt_p3", yes_price=0.25)


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
    if opened_at is None:
        opened_at = datetime.now(timezone.utc).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    sig_id = f"sig_{pos_id}"
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


async def _seed_closed_position(
    db,
    pos_id,
    market_id,
    strategy,
    realized_pnl,
    closed_at=None,
    entry_price_override=None,
):
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


# ── _p2_title_root ─────────────────────────────────────────────────────────────


class TestP2TitleRoot:
    def test_strips_month(self):
        assert _p2_title_root("Will GDP grow in March") == "will gdp grow in"

    def test_strips_full_month(self):
        assert (
            _p2_title_root("Will Fed cut rates in February") == "will fed cut rates in"
        )

    def test_strips_year(self):
        assert (
            _p2_title_root("Will Bitcoin hit $100k in 2025")
            == "will bitcoin hit $100k in"
        )

    def test_strips_quarter(self):
        assert _p2_title_root("Will GDP grow in Q1") == "will gdp grow in"

    def test_strips_half(self):
        assert _p2_title_root("Will GDP grow in H2") == "will gdp grow in"

    def test_strips_dollar_amount(self):
        assert _p2_title_root("Will S&P close above $5000") == "will s&p close above"

    def test_strips_multiple_suffixes(self):
        assert (
            _p2_title_root("Will Fed raise rates March 2025") == "will fed raise rates"
        )

    def test_preserves_core_title(self):
        assert (
            _p2_title_root("Will Bitcoin be adopted globally")
            == "will bitcoin be adopted globally"
        )

    def test_strips_trailing_punctuation(self):
        assert _p2_title_root("Will the Fed cut rates?") == "will the fed cut rates"


# ── P3 calibration bias ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestP3CalibrationBias:
    async def test_low_price_triggers_buy(self, db):
        await _seed_market(db, "m1", yes_price=0.25)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) >= 1
        assert p3[0]["side"] == "BUY"

    async def test_high_price_triggers_sell(self, db):
        await _seed_market(db, "m1", yes_price=0.75)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) >= 1
        assert p3[0]["side"] == "SELL"

    async def test_price_near_center_no_p3(self, db):
        await _seed_market(db, "m1", yes_price=0.55)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) == 0

    async def test_edge_boundary_exclusive(self, db):
        await _seed_market(db, "m1", yes_price=0.70)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) == 0


# ── P4 liquidity timing ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestP4LiquidityTiming:
    async def test_lower_zone_buy(self, db):
        await _seed_market(db, "m1", yes_price=0.32)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) >= 1
        assert p4[0]["side"] == "BUY"

    async def test_upper_zone_sell(self, db):
        await _seed_market(db, "m1", yes_price=0.68)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) >= 1
        assert p4[0]["side"] == "SELL"

    async def test_center_no_p4(self, db):
        await _seed_market(db, "m1", yes_price=0.50)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) == 0

    async def test_too_extreme_no_p4(self, db):
        await _seed_market(db, "m1", yes_price=0.10)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) == 0


# ── P5 information latency ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestP5InformationLatency:
    async def test_wide_spread_low_price_buy(self, db):
        await _seed_market(db, "m1", yes_price=0.18, spread=0.10)
        cfg = RiskControlConfig(strategy_p3_enabled=False, strategy_p4_enabled=False)
        trades = await detect_single_platform_opportunities(
            db, max_trades=10, risk_config=cfg
        )
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) >= 1
        assert p5[0]["side"] == "BUY"

    async def test_wide_spread_high_price_sell(self, db):
        await _seed_market(db, "m1", yes_price=0.82, spread=0.12)
        cfg = RiskControlConfig(strategy_p3_enabled=False, strategy_p4_enabled=False)
        trades = await detect_single_platform_opportunities(
            db, max_trades=10, risk_config=cfg
        )
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) >= 1
        assert p5[0]["side"] == "SELL"

    async def test_narrow_spread_no_p5(self, db):
        await _seed_market(db, "m1", yes_price=0.15, spread=0.02)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) == 0

    async def test_wide_spread_center_price_no_p5(self, db):
        await _seed_market(db, "m1", yes_price=0.40, spread=0.15)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) == 0

    async def test_edge_is_30_pct_of_spread(self, db):
        spread = 0.12
        await _seed_market(db, "m1", yes_price=0.18, spread=spread)
        cfg = RiskControlConfig(strategy_p3_enabled=False, strategy_p4_enabled=False)
        trades = await detect_single_platform_opportunities(
            db, max_trades=10, risk_config=cfg
        )
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) >= 1
        assert abs(p5[0]["edge"] - round(spread * 0.30, 4)) < 0.001


# ── P2 structured event ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestP2StructuredEvent:
    async def test_over_sum_triggers_sell(self, db):
        for i, (month, price) in enumerate(
            zip(["January", "March", "June"], [0.42, 0.40, 0.38])
        ):
            await _seed_market(
                db,
                f"kal_fed_{i}",
                platform="kalshi",
                title=f"Will the Fed raise rates in {month}",
                yes_price=price,
            )
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) >= 1
        assert p2[0]["side"] == "SELL"

    async def test_sum_below_threshold_no_p2(self, db):
        for i, (quarter, price) in enumerate(
            zip(["Q1", "Q2", "Q3"], [0.30, 0.28, 0.30])
        ):
            await _seed_market(
                db,
                f"poly_gdp_{i}",
                platform="polymarket",
                title=f"Will GDP grow in {quarter}",
                yes_price=price,
            )
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) == 0

    async def test_cross_platform_not_grouped(self, db):
        await _seed_market(
            db,
            "cpi_poly",
            platform="polymarket",
            title="Will CPI exceed 3% in January",
            yes_price=0.55,
        )
        await _seed_market(
            db,
            "cpi_kal",
            platform="kalshi",
            title="Will CPI exceed 3% in February",
            yes_price=0.55,
        )
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) == 0

    async def test_short_title_root_skipped(self, db):
        for i, month in enumerate(["January", "February", "March"]):
            await _seed_market(
                db,
                f"rain_{i}",
                platform="kalshi",
                title=f"Rain in {month}",
                yes_price=0.40,
            )
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) == 0

    async def test_most_overpriced_selected(self, db):
        prices = [0.55, 0.38, 0.32]
        mids = []
        for i, (month, price) in enumerate(zip(["January", "March", "June"], prices)):
            mid = f"kal_unemp_{i}"
            mids.append(mid)
            await _seed_market(
                db,
                mid,
                platform="kalshi",
                title=f"Will unemployment fall below 4% in {month}",
                yes_price=price,
            )
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) >= 1
        assert mids[0][:20] in p2[0]["market"]

    async def test_single_market_no_p2(self, db):
        await _seed_market(
            db,
            "solo",
            platform="kalshi",
            title="Will the Fed raise rates in January",
            yes_price=0.70,
        )
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) == 0


# ── Quota allocation ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestQuotaAllocation:
    async def test_total_capped_at_max_trades(self, db):
        max_trades = 10
        for i in range(30):
            await _seed_market(db, f"mkt_{i}", yes_price=0.20, spread=0.12)
        trades = await detect_single_platform_opportunities(db, max_trades=max_trades)
        assert len(trades) <= max_trades

    async def test_p3_capped(self, db):
        max_trades = 20
        for i in range(20):
            await _seed_market(db, f"p3_{i}", yes_price=0.20)
        trades = await detect_single_platform_opportunities(db, max_trades=max_trades)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) <= max(1, int(max_trades * 0.50))

    async def test_p4_capped(self, db):
        max_trades = 20
        for i in range(20):
            await _seed_market(db, f"p4_{i}", yes_price=0.25)
        trades = await detect_single_platform_opportunities(db, max_trades=max_trades)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) <= max(2, int(max_trades * 0.25))

    async def test_all_strategies_represented(self, db):
        await _seed_market(
            db, "p3_1", platform="kalshi", title="P3 market alpha", yes_price=0.10
        )
        await _seed_market(
            db, "p4_1", platform="kalshi", title="P4 market alpha", yes_price=0.32
        )
        for i, month in enumerate(["January", "March", "June"]):
            await _seed_market(
                db,
                f"p2_{i}",
                platform="kalshi",
                title=f"Will inflation exceed 3% in {month}",
                yes_price=0.42,
            )
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        strategies = {t["strategy"] for t in trades}
        assert "P3_calibration_bias" in strategies
        assert "P4_liquidity_timing" in strategies
        assert "P2_structured_event" in strategies
        await _seed_market(
            db,
            "p5_check",
            platform="kalshi",
            title="P5 market alpha",
            yes_price=0.15,
            spread=0.10,
        )
        trades_p5 = await detect_single_platform_opportunities(
            db,
            max_trades=20,
            risk_config=RiskControlConfig(
                strategy_p3_enabled=False, strategy_p4_enabled=False
            ),
        )
        assert "P5_information_latency" in {t["strategy"] for t in trades_p5}

    async def test_empty_db_no_trades(self, db):
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        assert trades == []


# ── Fill model: positions open as 'open', no synthetic exit ──────────────────


@pytest.mark.asyncio
class TestPositionOpensAsOpen:
    async def test_single_platform_writes_open_status(self, db):
        await _seed_p3_market(db)
        cfg = _risk_config()
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute("SELECT status FROM positions LIMIT 1")
        row = await cursor.fetchone()
        assert row is not None, "No position written"
        assert row[0] == "open", f"Expected status='open', got '{row[0]}'"

    async def test_single_platform_no_exit_price_at_open(self, db):
        await _seed_p3_market(db)
        cfg = _risk_config()
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute("SELECT exit_price FROM positions LIMIT 1")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None, f"Expected exit_price=NULL, got {row[0]}"

    async def test_single_platform_no_realized_pnl_at_open(self, db):
        await _seed_p3_market(db)
        cfg = _risk_config()
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute("SELECT realized_pnl FROM positions LIMIT 1")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None, f"Expected realized_pnl=NULL, got {row[0]}"

    async def test_single_platform_no_closed_at_at_open(self, db):
        await _seed_p3_market(db)
        cfg = _risk_config()
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute("SELECT closed_at FROM positions LIMIT 1")
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None, f"Expected closed_at=NULL, got {row[0]}"


# ── Mark-to-market ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestMarkToMarket:
    async def test_expired_position_gets_closed(self, db):
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
        assert (await cursor.fetchone())[0] == "closed"

    async def test_expired_position_has_realized_pnl(self, db):
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
        await _insert_open_position(db, "pos3", "mkt3")
        closed = await mark_and_close_positions(db, holding_period_s=300)
        assert closed == 0
        cursor = await db.execute("SELECT status FROM positions WHERE id='pos3'")
        assert (await cursor.fetchone())[0] == "open"

    async def test_no_price_data_skips_close(self, db):
        now_dt = datetime.now(timezone.utc)
        old_opened = (now_dt - timedelta(seconds=400)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES ('mkt4', 'polymarket', 'p4', 'T4', 'open', ?, ?)",
            (now, now),
        )
        await _insert_open_position(db, "pos4", "mkt4", opened_at=old_opened)
        closed = await mark_and_close_positions(db, holding_period_s=300)
        assert closed == 0
        cursor = await db.execute("SELECT status FROM positions WHERE id='pos4'")
        assert (await cursor.fetchone())[0] == "open"

    async def test_buy_realized_pnl_correct(self, db):
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
        await _insert_open_position(
            db,
            "pos5",
            "mkt5",
            side="BUY",
            entry_price=0.40,
            size=10.0,
            opened_at=old_opened,
        )
        await db.execute("UPDATE positions SET fees_paid=0.02 WHERE id='pos5'")
        await db.commit()
        await mark_and_close_positions(db, holding_period_s=300)
        cursor = await db.execute("SELECT realized_pnl FROM positions WHERE id='pos5'")
        row = await cursor.fetchone()
        assert abs(row[0] - 1.98) < 0.001, f"Expected ~1.98, got {row[0]}"


# ── Slippage model ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSlippageModel:
    async def _seed_market_price(self, db, market_id, price):
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT OR IGNORE INTO markets "
            "(id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES (?, 'polymarket', ?, 'T', 'open', ?, ?)",
            (market_id, market_id, now, now),
        )
        await db.execute(
            "INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at) "
            "VALUES (?, ?, ?, 0.02, 5000, ?)",
            (market_id, price, round(1 - price, 4), now),
        )
        await db.commit()

    async def test_buy_fills_at_or_above_market_price(self, db):
        await self._seed_market_price(db, "mkt_slip_buy", 0.50)
        client = PaperExecutionClient(
            db, platform_label="paper_polymarket", slippage_bps=100.0
        )
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
        assert result.filled_price >= 0.50

    async def test_sell_fills_at_or_below_market_price(self, db):
        await self._seed_market_price(db, "mkt_slip_sell", 0.50)
        client = PaperExecutionClient(
            db, platform_label="paper_polymarket", slippage_bps=100.0
        )
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
        assert result.filled_price <= 0.50

    async def test_zero_slippage_fills_exactly_at_market(self, db):
        await self._seed_market_price(db, "mkt_zero_slip", 0.50)
        client = PaperExecutionClient(
            db, platform_label="paper_polymarket", slippage_bps=0.0
        )
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
        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        assert client.slippage_bps == 0.0


# ── Fee rates ─────────────────────────────────────────────────────────────────


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
            "INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at) "
            "VALUES (?, ?, ?, 0.02, 5000, ?)",
            (market_id, price, round(1 - price, 4), now),
        )
        await db.commit()

    async def test_custom_fee_rate_applied(self, db):
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
        assert abs(result.fee_paid - 0.25) < 0.01


# ── pnl_model column ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPnlModelColumn:
    async def test_pnl_model_column_exists(self, db):
        cursor = await db.execute("PRAGMA table_info(positions)")
        cols = {row[1] for row in await cursor.fetchall()}
        assert "pnl_model" in cols, "positions.pnl_model column missing"

    async def test_new_realistic_position_default(self, db):
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
        cfg = RiskControlConfig()
        assert hasattr(cfg, "slippage_bps")
        assert cfg.slippage_bps >= 0

    async def test_risk_config_has_holding_period(self):
        cfg = RiskControlConfig()
        assert hasattr(cfg, "strategy_holding_period_s")
        assert cfg.strategy_holding_period_s > 0


# ── ScheduledStrategyRunner calls mark-to-market ─────────────────────────────


@pytest.mark.asyncio
class TestScheduledRunnerMarkToMarket:
    async def test_run_one_cycle_closes_expired_positions(self, db):
        now_dt = datetime.now(timezone.utc)
        old_opened = (now_dt - timedelta(seconds=400)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO markets (id, platform, platform_id, title, status, created_at, updated_at) "
            "VALUES ('mkt_sched', 'polymarket', 'ms', 'Sched Market', 'open', ?, ?)",
            (now, now),
        )
        await db.execute(
            "INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at) "
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
        ), f"Expected run_one_cycle to close expired position, got '{row[0]}'"

    async def test_run_one_cycle_returns_list(self, db):
        cfg = _risk_config()
        runner = ScheduledStrategyRunner(db, interval=120, risk_config=cfg)
        result = await runner.run_one_cycle()
        assert isinstance(result, list)


# ── Cross-strategy dedup ──────────────────────────────────────────────────────


class TestCrossStrategyDedup:
    def test_same_market_keeps_highest_signal_strength(self):
        opps = [
            _make_opp("mkt_A", "P3_calibration_bias", signal_strength=0.30),
            _make_opp("mkt_A", "P4_liquidity_timing", signal_strength=0.25),
        ]
        result = _cross_strategy_dedup(opps)
        assert len(result) == 1
        assert result[0]["strategy"] == "P3_calibration_bias"
        assert result[0]["signal_strength"] == 0.30

    def test_same_market_lower_strength_wins_if_higher(self):
        opps = [
            _make_opp("mkt_B", "P3_calibration_bias", signal_strength=0.20),
            _make_opp("mkt_B", "P4_liquidity_timing", signal_strength=0.35),
        ]
        result = _cross_strategy_dedup(opps)
        assert len(result) == 1
        assert result[0]["strategy"] == "P4_liquidity_timing"

    def test_unique_markets_all_kept(self):
        opps = [
            _make_opp("mkt_C", "P3_calibration_bias", 0.30),
            _make_opp("mkt_D", "P4_liquidity_timing", 0.25),
        ]
        result = _cross_strategy_dedup(opps)
        assert len(result) == 2

    def test_empty_list_returns_empty(self):
        assert _cross_strategy_dedup([]) == []

    def test_three_strategies_same_market(self):
        opps = [
            _make_opp("mkt_E", "P3_calibration_bias", 0.20),
            _make_opp("mkt_E", "P4_liquidity_timing", 0.40),
            _make_opp("mkt_E", "P5_information_latency", 0.30),
        ]
        result = _cross_strategy_dedup(opps)
        assert len(result) == 1
        assert result[0]["signal_strength"] == 0.40


# ── Signal strength normalization ─────────────────────────────────────────────


class TestNormalizeSignalStrengths:
    def test_z_scores_within_strategy(self):
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
        assert zscores[2] > zscores[1] > zscores[0]

    def test_single_item_normalized_to_zero(self):
        opps = [_make_opp("mkt_1", "P3_calibration_bias", 0.30)]
        result = _normalize_signal_strengths(opps)
        assert result[0]["signal_strength_normalized"] == 0.0

    def test_normalization_does_not_cross_strategies(self):
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
        assert abs(p3_z[0] - p5_z[0]) < 0.001

    def test_original_signal_strength_preserved(self):
        opps = [
            _make_opp("mkt_1", "P3_calibration_bias", 0.30),
            _make_opp("mkt_2", "P3_calibration_bias", 0.50),
        ]
        result = _normalize_signal_strengths(opps)
        assert result[0]["signal_strength"] == 0.30
        assert result[1]["signal_strength"] == 0.50

    def test_empty_list_returns_empty(self):
        assert _normalize_signal_strengths([]) == []


# ── Per-strategy enable flags ─────────────────────────────────────────────────


class TestRiskConfigStrategyFlags:
    def test_strategy_flags_exist_with_defaults(self):
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
        cfg = RiskControlConfig()
        assert hasattr(cfg, "strategy_replay_cooldown_s")
        assert cfg.strategy_replay_cooldown_s > 0

    def test_replay_min_move_exists(self):
        cfg = RiskControlConfig()
        assert hasattr(cfg, "strategy_replay_min_move")
        assert 0 < cfg.strategy_replay_min_move < 1.0

    def test_killswitch_fields_exist(self):
        cfg = RiskControlConfig()
        assert hasattr(cfg, "strategy_killswitch_window_s")
        assert hasattr(cfg, "strategy_killswitch_min_trades")
        assert cfg.strategy_killswitch_window_s > 0
        assert cfg.strategy_killswitch_min_trades > 0


@pytest.mark.asyncio
class TestStrategyEnableFlags:
    async def test_disabled_p3_produces_no_p3_trades(self, db):
        await _seed_market(db, "mkt_p3", yes_price=0.25)
        cfg = _risk_config(strategy_p3_enabled=False)
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy='P3_calibration_bias'"
        )
        assert (await cursor.fetchone())[0] == 0

    async def test_enabled_p3_produces_trades(self, db):
        await _seed_market(db, "mkt_p3", yes_price=0.25)
        cfg = _risk_config(strategy_p3_enabled=True)
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy='P3_calibration_bias'"
        )
        assert (await cursor.fetchone())[0] >= 1

    async def test_disabled_p4_produces_no_p4_trades(self, db):
        await _seed_market(db, "mkt_p4", yes_price=0.25)
        cfg = _risk_config(strategy_p4_enabled=False)
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy='P4_liquidity_timing'"
        )
        assert (await cursor.fetchone())[0] == 0


# ── Consecutive-cycle dedup ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestConsecutiveCycleDedup:
    async def test_market_with_open_position_is_skipped(self, db):
        await _seed_market(db, "mkt_open", yes_price=0.25)
        await _seed_open_position(
            db, "pos_open", "mkt_open", "P3_calibration_bias", 0.25
        )
        cfg = _risk_config(
            strategy_replay_cooldown_s=300, strategy_replay_min_move=0.01
        )
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id='mkt_open'"
        )
        assert (await cursor.fetchone())[0] == 1

    async def test_recently_closed_market_without_price_move_skipped(self, db):
        entry_price = 0.25
        await _seed_market(db, "mkt_recent", yes_price=entry_price)
        closed_at = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        await _seed_closed_position(
            db,
            "pos_recent",
            "mkt_recent",
            "P3_calibration_bias",
            realized_pnl=0.05,
            closed_at=closed_at,
            entry_price_override=entry_price,
        )
        cfg = _risk_config(
            strategy_replay_cooldown_s=300, strategy_replay_min_move=0.01
        )
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id='mkt_recent'"
        )
        assert (await cursor.fetchone())[0] == 1

    async def test_recently_closed_with_price_move_allowed(self, db):
        entry_price = 0.25
        await _seed_market(db, "mkt_moved", yes_price=0.20)
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
            strategy_replay_cooldown_s=300, strategy_replay_min_move=0.01
        )
        await detect_single_platform_opportunities(db, max_trades=5, risk_config=cfg)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id='mkt_moved' AND status='open'"
        )
        assert (await cursor.fetchone())[0] >= 1

    async def test_market_outside_cooldown_window_allowed(self, db):
        await _seed_market(db, "mkt_old", yes_price=0.25)
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
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE market_id='mkt_old' AND status='open'"
        )
        assert (await cursor.fetchone())[0] >= 1


# ── Per-strategy kill-switch ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestStrategyKillSwitch:
    async def test_get_strategy_rolling_pnl_empty(self, db):
        count, pnl = await _get_strategy_rolling_pnl(db, "P3_calibration_bias", 604800)
        assert count == 0
        assert pnl == 0.0

    async def test_get_strategy_rolling_pnl_sums_correctly(self, db):
        await _seed_market(db, "mkt_pnl1", yes_price=0.40)
        await _seed_market(db, "mkt_pnl2", yes_price=0.45)
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
        for i in range(5):
            await _seed_market(db, f"mkt_kill_{i}", yes_price=0.40)
            await _seed_closed_position(
                db, f"ks_kill_{i}", f"mkt_kill_{i}", "P3_calibration_bias", -0.50
            )
        await _seed_market(db, "mkt_trigger", yes_price=0.25)
        cfg = _risk_config(
            strategy_p3_enabled=True,
            strategy_killswitch_window_s=604800,
            strategy_killswitch_min_trades=5,
        )
        await detect_single_platform_opportunities(db, max_trades=10, risk_config=cfg)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy='P3_calibration_bias' AND market_id='mkt_trigger'"
        )
        assert (await cursor.fetchone())[0] == 0

    async def test_insufficient_trades_does_not_trigger_killswitch(self, db):
        for i in range(2):
            await _seed_market(db, f"mkt_few_{i}", yes_price=0.40)
            await _seed_closed_position(
                db, f"ks_few_{i}", f"mkt_few_{i}", "P3_calibration_bias", -0.50
            )
        await _seed_market(db, "mkt_few_trigger", yes_price=0.25)
        cfg = _risk_config(strategy_killswitch_min_trades=5)
        await detect_single_platform_opportunities(db, max_trades=10, risk_config=cfg)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy='P3_calibration_bias' AND market_id='mkt_few_trigger'"
        )
        assert (await cursor.fetchone())[0] >= 1

    async def test_positive_rolling_pnl_does_not_kill(self, db):
        for i in range(5):
            await _seed_market(db, f"mkt_pos_{i}", yes_price=0.40)
            await _seed_closed_position(
                db, f"ks_pos_{i}", f"mkt_pos_{i}", "P3_calibration_bias", +0.50
            )
        await _seed_market(db, "mkt_pos_trigger", yes_price=0.25)
        cfg = _risk_config(strategy_killswitch_min_trades=5)
        await detect_single_platform_opportunities(db, max_trades=10, risk_config=cfg)
        cursor = await db.execute(
            "SELECT COUNT(*) FROM positions WHERE strategy='P3_calibration_bias' AND market_id='mkt_pos_trigger'"
        )
        assert (await cursor.fetchone())[0] >= 1
