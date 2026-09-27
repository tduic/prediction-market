"""Halt enforcement and fill handling in ArbitrageEngine._execute_arb_trade.

Legs are sequenced: the hedge (sell) leg is sent only after the buy leg
fills, sized to the buy's actual fill. Any unknown fill state (``pending``)
or incomplete hedge trips the persistent execution halt.
"""

from unittest.mock import MagicMock

import pytest

from core.engine import ArbitrageEngine, execution_control
from core.engine.execution_control import get_halt, halt, is_halted
from execution.clients.base import OrderResult
from tests.integration.test_arb_engine import _make_match, _seed_markets_for_engine


@pytest.fixture(autouse=True)
def _quiet_alerts_and_no_backoff(monkeypatch):
    monkeypatch.setattr(execution_control, "get_alert_manager", MagicMock)

    async def _no_sleep(_delay):
        return None

    monkeypatch.setattr("core.engine.arb_engine.asyncio.sleep", _no_sleep)


class FakeClient:
    """Returns scripted OrderResults in order; records legs and cancels."""

    def __init__(self, platform: str, *results: OrderResult):
        self.platform = platform
        self._results = list(results)
        self.legs = []
        self.cancelled: list[str] = []

    async def submit_order(self, leg, signal_id=None, strategy=None):
        self.legs.append(leg)
        return self._results.pop(0)

    async def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        return True


def _filled(platform, price, size, order_id="o-fill"):
    return OrderResult(
        order_id=order_id,
        platform=platform,
        status="filled",
        submission_latency_ms=1,
        filled_price=price,
        filled_size=size,
        fee_paid=0.0,
    )


def _partial(platform, price, size, order_id="o-part"):
    result = _filled(platform, price, size, order_id)
    result.status = "partially_filled"
    return result


def _failed(platform, order_id="o-fail"):
    return OrderResult(
        order_id=order_id,
        platform=platform,
        status="failed",
        submission_latency_ms=1,
        error_message="rejected",
    )


def _pending(platform, order_id="o-pend"):
    return OrderResult(
        order_id=order_id,
        platform=platform,
        status="pending",
        submission_latency_ms=1,
        error_message="fill poll timeout",
    )


async def _engine(db, buy: FakeClient, sell: FakeClient) -> ArbitrageEngine:
    # poly 0.40 < kalshi 0.50 → buy on Polymarket, sell on Kalshi.
    matches = [_make_match("poly_A", "kal_A", 0.40, 0.50)]
    await _seed_markets_for_engine(db, matches)
    engine = ArbitrageEngine(db, matches, min_spread=0.03)
    engine._poly_client = buy
    engine._kalshi_client = sell
    return engine


async def test_halted_engine_places_no_orders(db):
    await halt(db, "earlier unknown fill", component="arb_engine")
    buy, sell = FakeClient("polymarket"), FakeClient("kalshi")
    engine = await _engine(db, buy, sell)
    await engine.initial_sweep()
    assert buy.legs == [] and sell.legs == []
    assert engine.stats()["skipped_halted"] == 1


async def test_failed_buy_does_not_send_hedge(db):
    buy = FakeClient("polymarket", *[_failed("polymarket")] * 5)
    sell = FakeClient("kalshi")
    engine = await _engine(db, buy, sell)
    await engine.initial_sweep()
    assert sell.legs == []
    assert await is_halted(db) is False


async def test_pending_buy_halts_cancels_and_is_not_retried(db):
    buy = FakeClient("polymarket", _pending("polymarket", "o-unknown"))
    sell = FakeClient("kalshi")
    engine = await _engine(db, buy, sell)
    await engine.initial_sweep()
    assert len(buy.legs) == 1  # no retry: a retry could double the position
    assert buy.cancelled == ["o-unknown"]
    assert sell.legs == []
    state = await get_halt(db)
    assert state is not None and state["reason"].startswith("unknown_fill")
    assert engine.trades == []


async def test_hedge_sized_to_actual_buy_fill(db):
    buy = FakeClient("polymarket", _partial("polymarket", 0.40, 7.0))
    sell = FakeClient("kalshi", _filled("kalshi", 0.50, 7.0))
    engine = await _engine(db, buy, sell)
    await engine.initial_sweep()
    assert sell.legs[0].size == 7.0
    assert await is_halted(db) is False
    assert len(engine.trades) == 1


async def test_hedge_to_kalshi_floors_to_whole_contracts(db):
    buy = FakeClient("polymarket", _partial("polymarket", 0.40, 7.6))
    sell = FakeClient("kalshi", _filled("kalshi", 0.50, 7.0))
    engine = await _engine(db, buy, sell)
    await engine.initial_sweep()
    assert sell.legs[0].size == 7.0
    # 0.6 of the buy is unhedged → halt so an operator flattens it.
    state = await get_halt(db)
    assert state is not None and state["reason"].startswith("unbalanced_arb")


async def test_failed_hedge_halts_unbalanced(db):
    buy = FakeClient("polymarket", _filled("polymarket", 0.40, 10.0))
    sell = FakeClient("kalshi", *[_failed("kalshi")] * 5)
    engine = await _engine(db, buy, sell)
    await engine.initial_sweep()
    state = await get_halt(db)
    assert state is not None and state["reason"].startswith("unbalanced_arb")
    assert engine.trades == []


async def test_pending_hedge_halts_and_cancels(db):
    buy = FakeClient("polymarket", _filled("polymarket", 0.40, 10.0))
    sell = FakeClient("kalshi", _pending("kalshi", "o-hedge"))
    engine = await _engine(db, buy, sell)
    await engine.initial_sweep()
    assert sell.cancelled == ["o-hedge"]
    state = await get_halt(db)
    assert state is not None and state["reason"].startswith("unknown_fill")


async def test_partial_hedge_halts_unbalanced(db):
    buy = FakeClient("polymarket", _filled("polymarket", 0.40, 10.0))
    sell = FakeClient("kalshi", _partial("kalshi", 0.50, 4.0))
    engine = await _engine(db, buy, sell)
    await engine.initial_sweep()
    state = await get_halt(db)
    assert state is not None and state["reason"].startswith("unbalanced_arb")


async def test_balanced_fill_records_filled_size(db):
    buy = FakeClient("polymarket", _filled("polymarket", 0.40, 10.0))
    sell = FakeClient("kalshi", _filled("kalshi", 0.50, 10.0))
    engine = await _engine(db, buy, sell)
    await engine.initial_sweep()
    assert len(engine.trades) == 1
    cursor = await db.execute("SELECT entry_size, exit_size FROM positions")
    assert tuple(await cursor.fetchone()) == (10.0, 10.0)
    assert await is_halted(db) is False


async def test_translated_polymarket_sell_pnl_in_yes_space(db):
    # kalshi 0.55 < poly 0.70 → buy Kalshi, sell Polymarket. Without YES
    # inventory the Polymarket sell executes as BUY NO; its fill price is in
    # NO space and must be converted back (1 - p) for P&L.
    matches = [_make_match("poly_A", "kal_A", 0.70, 0.55)]
    await _seed_markets_for_engine(db, matches)
    engine = ArbitrageEngine(db, matches, min_spread=0.03)
    no_fill = _filled("polymarket", 0.36, 10.0)
    no_fill.book = "NO"
    engine._kalshi_client = FakeClient("kalshi", _filled("kalshi", 0.55, 10.0))
    engine._poly_client = FakeClient("polymarket", no_fill)
    await engine.initial_sweep()
    assert len(engine.trades) == 1
    # P&L on the 10 contracts that actually traded
    assert engine.trades[0]["actual_pnl"] == pytest.approx((0.64 - 0.55) * 10.0)
    cursor = await db.execute("SELECT exit_price FROM positions")
    assert (await cursor.fetchone())[0] == pytest.approx(0.64)
