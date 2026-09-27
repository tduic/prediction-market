"""Fee-aware profitability gate in ArbitrageEngine._execute_arb_trade."""

import pytest

from core.config import RiskControlConfig
from core.engine import ArbitrageEngine
from core.engine.fees import arb_economics, kalshi_params, polymarket_params
from execution.clients.paper import PaperExecutionClient
from execution.market_data import StaticMarketData, set_market_data
from tests.integration.test_arb_engine import _make_match, _seed_markets_for_engine

POLY_5PCT = polymarket_params(0.05, 1.0)


@pytest.fixture(autouse=True)
def _no_live_paper_prices(monkeypatch):
    async def _none(self, platform, platform_id):
        return None

    monkeypatch.setattr(PaperExecutionClient, "_fetch_live_price", _none)


def _venue_fees():
    set_market_data(
        StaticMarketData(fees={"polymarket": POLY_5PCT, "kalshi": kalshi_params()})
    )


async def _order_count(db) -> int:
    cursor = await db.execute("SELECT COUNT(*) FROM orders")
    return (await cursor.fetchone())[0]


async def test_spread_eaten_by_fees_places_no_orders(db):
    _venue_fees()
    matches = [_make_match("poly_A", "kal_A", 0.50, 0.52)]
    await _seed_markets_for_engine(db, matches)
    engine = ArbitrageEngine(db, matches, min_spread=0.02)
    await engine.initial_sweep()
    assert engine.trades == []
    assert await _order_count(db) == 0
    assert engine.stats()["skipped_unprofitable"] == 1


async def test_profitable_after_fees_trades_and_records_fee_estimates(db):
    _venue_fees()
    matches = [_make_match("poly_A", "kal_A", 0.40, 0.50)]
    await _seed_markets_for_engine(db, matches)
    engine = ArbitrageEngine(db, matches, min_spread=0.03)
    await engine.initial_sweep()
    assert len(engine.trades) == 1

    cursor = await db.execute(
        "SELECT net_spread, fee_estimate_a, fee_estimate_b FROM violations"
    )
    net_spread, fee_a, fee_b = await cursor.fetchone()
    cursor = await db.execute("SELECT position_size_a, model_edge FROM signals")
    size, model_edge = await cursor.fetchone()
    econ = arb_economics(0.40, 0.50, size, POLY_5PCT, kalshi_params())
    assert econ is not None
    assert fee_a == pytest.approx(econ.buy_fee)
    assert fee_b == pytest.approx(econ.sell_fee)
    assert net_spread == pytest.approx(econ.net_edge_per_contract)
    # Kelly sized on the net edge: 0.10 - 0.05*0.24 - 0.07*0.25
    assert model_edge == pytest.approx(0.0705)


async def test_unknown_fees_fail_closed(db):
    class _NoFees(StaticMarketData):
        async def fee_params(self, db, market_id):
            return None

    set_market_data(_NoFees())
    matches = [_make_match("poly_A", "kal_A", 0.40, 0.50)]
    await _seed_markets_for_engine(db, matches)
    engine = ArbitrageEngine(db, matches, min_spread=0.03)
    await engine.initial_sweep()
    assert engine.trades == []
    assert await _order_count(db) == 0
    assert engine.stats()["skipped_fee_unknown"] == 1


async def test_min_net_profit_floor(db):
    _venue_fees()
    matches = [_make_match("poly_A", "kal_A", 0.40, 0.50)]
    await _seed_markets_for_engine(db, matches)
    engine = ArbitrageEngine(
        db,
        matches,
        min_spread=0.03,
        risk_config=RiskControlConfig(arb_min_net_profit=1_000_000.0),
    )
    await engine.initial_sweep()
    assert engine.trades == []
    assert await _order_count(db) == 0


def test_min_net_profit_env(monkeypatch):
    monkeypatch.setenv("ARB_MIN_NET_PROFIT", "0.25")
    assert RiskControlConfig().arb_min_net_profit == 0.25
    monkeypatch.delenv("ARB_MIN_NET_PROFIT")
    assert RiskControlConfig().arb_min_net_profit == 0.0


async def test_slow_fee_lookup_times_out_without_trading(db):
    import asyncio
    import time

    class _Slow(StaticMarketData):
        async def fee_params(self, db, market_id):
            await asyncio.sleep(10)
            return kalshi_params()

    set_market_data(_Slow())
    matches = [_make_match("poly_A", "kal_A", 0.40, 0.50)]
    await _seed_markets_for_engine(db, matches)
    engine = ArbitrageEngine(
        db,
        matches,
        min_spread=0.03,
        risk_config=RiskControlConfig(arb_pretrade_lookup_timeout_s=0.05),
    )
    started = time.monotonic()
    await engine.initial_sweep()
    assert time.monotonic() - started < 1.0
    assert engine.trades == []
    assert engine.stats()["skipped_fee_unknown"] == 1


async def test_warm_fee_cache_prefetches_all_pair_markets(db):
    class _Recorder(StaticMarketData):
        def __init__(self):
            super().__init__()
            self.prefetched: list[str] = []

        async def prefetch_fees(self, db, market_ids):
            self.prefetched.extend(market_ids)

    md = _Recorder()
    set_market_data(md)
    matches = [_make_match("poly_A", "kal_A", 0.40, 0.50)]
    engine = ArbitrageEngine(db, matches, min_spread=0.03)
    await engine.warm_fee_cache()
    assert sorted(md.prefetched) == ["kal_A", "poly_A"]
