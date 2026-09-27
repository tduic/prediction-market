"""Order-book depth caps P1 arb size; unknown or thin books skip the trade."""

import pytest

from core.config import RiskControlConfig
from core.engine import ArbitrageEngine
from execution.clients.paper import PaperExecutionClient
from execution.market_data import StaticMarketData, set_market_data
from tests.integration.test_arb_engine import _make_match, _seed_markets_for_engine


@pytest.fixture(autouse=True)
def _no_live_paper_prices(monkeypatch):
    async def _none(self, platform, platform_id):
        return None

    monkeypatch.setattr(PaperExecutionClient, "_fetch_live_price", _none)


class _PerMarketDepth(StaticMarketData):
    def __init__(self, depths: dict[str, float | None]):
        super().__init__()
        self.depths = depths

    async def executable_depth(self, db, leg):
        return self.depths[leg.market_id]


async def _run(db, **engine_kwargs) -> ArbitrageEngine:
    matches = [_make_match("poly_A", "kal_A", 0.40, 0.50)]
    await _seed_markets_for_engine(db, matches)
    engine = ArbitrageEngine(db, matches, min_spread=0.03, **engine_kwargs)
    await engine.initial_sweep()
    return engine


async def _order_sizes(db) -> list[float]:
    cursor = await db.execute("SELECT requested_size FROM orders ORDER BY submitted_at")
    return [row[0] for row in await cursor.fetchall()]


async def test_size_capped_by_thinner_book(db):
    set_market_data(_PerMarketDepth({"poly_A": 40.0, "kal_A": 12.7}))
    engine = await _run(db)
    assert len(engine.trades) == 1
    assert await _order_sizes(db) == [12.0, 12.0]  # whole contracts


async def test_unknown_depth_skips(db):
    set_market_data(_PerMarketDepth({"poly_A": 40.0, "kal_A": None}))
    engine = await _run(db)
    assert engine.trades == []
    assert await _order_sizes(db) == []
    assert engine.stats()["skipped_depth_unknown"] == 1


async def test_thin_book_skips(db):
    set_market_data(_PerMarketDepth({"poly_A": 40.0, "kal_A": 0.5}))
    engine = await _run(db)
    assert engine.trades == []
    assert engine.stats()["skipped_thin_book"] == 1


async def test_min_fill_size_floor(db):
    set_market_data(_PerMarketDepth({"poly_A": 40.0, "kal_A": 3.0}))
    engine = await _run(db, risk_config=RiskControlConfig(arb_min_fill_size=5.0))
    assert engine.trades == []
    assert engine.stats()["skipped_thin_book"] == 1


def test_min_fill_size_env(monkeypatch):
    monkeypatch.setenv("ARB_MIN_FILL_SIZE", "3")
    assert RiskControlConfig().arb_min_fill_size == 3.0
