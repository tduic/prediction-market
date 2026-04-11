"""
Tests for detect_single_platform_opportunities and _p2_title_root.

Covers:
  - P3 (calibration bias): extreme price triggers, side direction
  - P4 (liquidity timing): price in transition zones, side direction
  - P5 (information latency): wide spread + extreme price
  - P2 (structured event): same-platform series over-sum detection
  - Quota allocation: per-strategy slot caps
  - _p2_title_root: suffix stripping for series grouping
"""

import ast
import sys
import textwrap
from datetime import datetime, timezone
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
import re as _re

_STRIP_SUFFIX = _re.compile(
    r"\s+(?:(january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
    r"|20\d{2}|q[1-4]|h[1-2]|"
    r"\$?[\d,]+\.?\d*[km%]?(?:\s*[-\u2013to]+\s*\$?[\d,]+\.?\d*[km%]?)?)"
    r"\s*$",
    _re.IGNORECASE,
)
_ns = {"_STRIP_SUFFIX": _STRIP_SUFFIX, "re": _re}
exec(compile(_root_src, "<_p2_title_root>", "exec"), _ns)
_p2_title_root = _ns["_p2_title_root"]

from scripts.paper_trading_session import detect_single_platform_opportunities  # noqa: E402


async def _seed_market(db, market_id, platform, title, yes_price, spread=0.02, liquidity=10000.0):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (market_id, platform, market_id, title, now, now),
    )
    await db.execute(
        "INSERT INTO market_prices (market_id, yes_price, no_price, spread, liquidity, polled_at) VALUES (?, ?, ?, ?, ?, ?)",
        (market_id, yes_price, round(1 - yes_price, 4), spread, liquidity, now),
    )
    await db.commit()


class TestP2TitleRoot:
    def test_strips_month(self):
        assert _p2_title_root("Will GDP grow in March") == "will gdp grow in"

    def test_strips_full_month(self):
        assert _p2_title_root("Will Fed cut rates in February") == "will fed cut rates in"

    def test_strips_year(self):
        assert _p2_title_root("Will Bitcoin hit $100k in 2025") == "will bitcoin hit $100k in"

    def test_strips_quarter(self):
        assert _p2_title_root("Will GDP grow in Q1") == "will gdp grow in"

    def test_strips_half(self):
        assert _p2_title_root("Will GDP grow in H2") == "will gdp grow in"

    def test_strips_dollar_amount(self):
        assert _p2_title_root("Will S&P close above $5000") == "will s&p close above"

    def test_strips_multiple_suffixes(self):
        assert _p2_title_root("Will Fed raise rates March 2025") == "will fed raise rates"

    def test_preserves_core_title(self):
        assert _p2_title_root("Will Bitcoin be adopted globally") == "will bitcoin be adopted globally"

    def test_strips_trailing_punctuation(self):
        assert _p2_title_root("Will the Fed cut rates?") == "will the fed cut rates"


@pytest.mark.asyncio
class TestP3CalibrationBias:
    async def test_low_price_triggers_buy(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.25)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) >= 1
        assert p3[0]["side"] == "BUY"

    async def test_high_price_triggers_sell(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.75)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) >= 1
        assert p3[0]["side"] == "SELL"

    async def test_price_near_center_no_p3(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.55)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) == 0

    async def test_edge_boundary_exclusive(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.70)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) == 0


@pytest.mark.asyncio
class TestP4LiquidityTiming:
    async def test_lower_zone_buy(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.20)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) >= 1
        assert p4[0]["side"] == "BUY"

    async def test_upper_zone_sell(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.75)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) >= 1
        assert p4[0]["side"] == "SELL"

    async def test_center_no_p4(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.50)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) == 0

    async def test_too_extreme_no_p4(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.10)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) == 0


@pytest.mark.asyncio
class TestP5InformationLatency:
    async def test_wide_spread_low_price_buy(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.18, spread=0.10)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) >= 1
        assert p5[0]["side"] == "BUY"

    async def test_wide_spread_high_price_sell(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.82, spread=0.12)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) >= 1
        assert p5[0]["side"] == "SELL"

    async def test_narrow_spread_no_p5(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.15, spread=0.02)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) == 0

    async def test_wide_spread_center_price_no_p5(self, db):
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.40, spread=0.15)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) == 0

    async def test_edge_is_30_pct_of_spread(self, db):
        spread = 0.12
        await _seed_market(db, "m1", "polymarket", "Test market", yes_price=0.18, spread=spread)
        trades = await detect_single_platform_opportunities(db, max_trades=10)
        p5 = [t for t in trades if t["strategy"] == "P5_information_latency"]
        assert len(p5) >= 1
        assert abs(p5[0]["edge"] - round(spread * 0.30, 4)) < 0.001


@pytest.mark.asyncio
class TestP2StructuredEvent:
    async def test_over_sum_triggers_sell(self, db):
        for i, (month, price) in enumerate(zip(["January", "March", "June"], [0.42, 0.40, 0.38])):
            await _seed_market(db, f"kal_fed_{i}", "kalshi", f"Will the Fed raise rates in {month}", yes_price=price)
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) >= 1
        assert p2[0]["side"] == "SELL"

    async def test_sum_below_threshold_no_p2(self, db):
        for i, (quarter, price) in enumerate(zip(["Q1", "Q2", "Q3"], [0.30, 0.28, 0.30])):
            await _seed_market(db, f"poly_gdp_{i}", "polymarket", f"Will GDP grow in {quarter}", yes_price=price)
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) == 0

    async def test_cross_platform_not_grouped(self, db):
        await _seed_market(db, "cpi_poly", "polymarket", "Will CPI exceed 3% in January", yes_price=0.55)
        await _seed_market(db, "cpi_kal", "kalshi", "Will CPI exceed 3% in February", yes_price=0.55)
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) == 0

    async def test_short_title_root_skipped(self, db):
        for i, month in enumerate(["January", "February", "March"]):
            await _seed_market(db, f"rain_{i}", "kalshi", f"Rain in {month}", yes_price=0.40)
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) == 0

    async def test_most_overpriced_selected(self, db):
        prices = [0.55, 0.38, 0.32]
        mids = []
        for i, (month, price) in enumerate(zip(["January", "March", "June"], prices)):
            mid = f"kal_unemp_{i}"
            mids.append(mid)
            await _seed_market(db, mid, "kalshi", f"Will unemployment fall below 4% in {month}", yes_price=price)
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) >= 1
        assert mids[0][:20] in p2[0]["market"]

    async def test_single_market_no_p2(self, db):
        await _seed_market(db, "solo", "kalshi", "Will the Fed raise rates in January", yes_price=0.70)
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        p2 = [t for t in trades if t["strategy"] == "P2_structured_event"]
        assert len(p2) == 0


@pytest.mark.asyncio
class TestQuotaAllocation:
    async def test_total_capped_at_max_trades(self, db):
        max_trades = 10
        for i in range(30):
            await _seed_market(db, f"mkt_{i}", "polymarket", f"Market {i}", yes_price=0.20, spread=0.12)
        trades = await detect_single_platform_opportunities(db, max_trades=max_trades)
        assert len(trades) <= max_trades

    async def test_p3_capped(self, db):
        max_trades = 20
        for i in range(20):
            await _seed_market(db, f"p3_{i}", "polymarket", f"P3 market {i}", yes_price=0.20)
        trades = await detect_single_platform_opportunities(db, max_trades=max_trades)
        p3 = [t for t in trades if t["strategy"] == "P3_calibration_bias"]
        assert len(p3) <= max(1, int(max_trades * 0.50))

    async def test_p4_capped(self, db):
        max_trades = 20
        for i in range(20):
            await _seed_market(db, f"p4_{i}", "polymarket", f"P4 market {i}", yes_price=0.25)
        trades = await detect_single_platform_opportunities(db, max_trades=max_trades)
        p4 = [t for t in trades if t["strategy"] == "P4_liquidity_timing"]
        assert len(p4) <= max(2, int(max_trades * 0.25))

    async def test_all_strategies_represented(self, db):
        await _seed_market(db, "p3_1", "kalshi", "P3 market alpha", yes_price=0.20)
        await _seed_market(db, "p4_1", "kalshi", "P4 market alpha", yes_price=0.25)
        await _seed_market(db, "p5_1", "kalshi", "P5 market alpha", yes_price=0.15, spread=0.10)
        for i, month in enumerate(["January", "March", "June"]):
            await _seed_market(db, f"p2_{i}", "kalshi", f"Will inflation exceed 3% in {month}", yes_price=0.42)
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        strategies = {t["strategy"] for t in trades}
        assert "P3_calibration_bias" in strategies
        assert "P4_liquidity_timing" in strategies
        assert "P5_information_latency" in strategies
        assert "P2_structured_event" in strategies

    async def test_empty_db_no_trades(self, db):
        trades = await detect_single_platform_opportunities(db, max_trades=20)
        assert trades == []
