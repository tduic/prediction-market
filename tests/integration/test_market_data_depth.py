"""LiveMarketData.executable_depth — quantity fillable within a leg's limit."""

from datetime import datetime, timezone, UTC

import httpx
import pytest

from execution.market_data import LiveMarketData
from execution.models import OrderLeg

POLY = "https://clob.test"
KALSHI = "https://kalshi.test/trade-api/v2"

# Shapes probed from the live public endpoints on 2026-09-27.
POLY_BOOK = {
    "market": "0xabc",
    "asset_id": "tok_yes",
    # Asks arrive worst-first; depth must be summed by price, not position.
    "asks": [
        {"price": "0.99", "size": "500"},
        {"price": "0.45", "size": "30"},
        {"price": "0.42", "size": "20"},
        {"price": "0.41", "size": "10"},
    ],
    "bids": [
        {"price": "0.30", "size": "100"},
        {"price": "0.38", "size": "15"},
        {"price": "0.40", "size": "25"},
    ],
}
KALSHI_BOOK = {
    "orderbook_fp": {
        # Both sides are BID lists: [price_dollars, quantity]
        "yes_dollars": [["0.4800", "40.00"], ["0.5000", "12.00"], ["0.5200", "8.00"]],
        "no_dollars": [["0.4000", "100.00"], ["0.4600", "7.00"], ["0.4800", "5.00"]],
    }
}


async def _seed(db, market_id, platform, platform_id, yes_tok=None, no_tok=None):
    now = datetime.now(UTC).isoformat()
    await db.execute(
        "INSERT INTO markets (id, platform, platform_id, title, yes_token_id,"
        " no_token_id, created_at, updated_at) VALUES (?, ?, ?, 't', ?, ?, ?, ?)",
        (market_id, platform, platform_id, yes_tok, no_tok, now, now),
    )
    await db.commit()


def _md(handler) -> tuple[LiveMarketData, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return LiveMarketData(http=http, poly_host=POLY, kalshi_base=KALSHI), seen


def _leg(market_id, platform, side, price, size=10.0):
    return OrderLeg(
        market_id=market_id,
        platform=platform,
        side=side,
        size=size,
        limit_price=price,
        order_type="LIMIT",
    )


class TestPolymarketDepth:
    async def test_buy_sums_asks_at_or_below_limit(self, db):
        await _seed(db, "poly_0xabc", "polymarket", "0xabc", "tok_yes", "tok_no")
        md, seen = _md(lambda r: httpx.Response(200, json=POLY_BOOK))
        depth = await md.executable_depth(
            db, _leg("poly_0xabc", "polymarket", "BUY", 0.42)
        )
        assert depth == 30.0  # 0.41 x10 + 0.42 x20
        assert seen[0].url.params["token_id"] == "tok_yes"

    async def test_sell_without_inventory_reads_no_book(self, db):
        # SELL YES @ 0.60 without inventory → BUY NO @ 0.40 on the NO book.
        await _seed(db, "poly_0xabc", "polymarket", "0xabc", "tok_yes", "tok_no")
        md, seen = _md(lambda r: httpx.Response(200, json=POLY_BOOK))
        depth = await md.executable_depth(
            db, _leg("poly_0xabc", "polymarket", "SELL", 0.60)
        )
        assert seen[0].url.params["token_id"] == "tok_no"
        assert depth == 0.0  # cheapest NO ask in the fixture is 0.41

    async def test_unresolvable_market_is_unknown(self, db):
        await _seed(db, "poly_0xabc", "polymarket", "0xabc")  # no token ids
        md, seen = _md(lambda r: httpx.Response(200, json=POLY_BOOK))
        assert (
            await md.executable_depth(db, _leg("poly_0xabc", "polymarket", "BUY", 0.5))
            is None
        )
        assert seen == []


class TestKalshiDepth:
    async def test_buy_yes_consumes_no_bids(self, db):
        # BUY YES <= 0.55 matches NO bids >= 0.45 → 7 + 5
        await _seed(db, "kal_KXT", "kalshi", "KXT")
        md, seen = _md(lambda r: httpx.Response(200, json=KALSHI_BOOK))
        depth = await md.executable_depth(db, _leg("kal_KXT", "kalshi", "BUY", 0.55))
        assert depth == 12.0
        assert seen[0].url.path == "/trade-api/v2/markets/KXT/orderbook"

    async def test_sell_yes_consumes_yes_bids(self, db):
        await _seed(db, "kal_KXT", "kalshi", "KXT")
        md, _ = _md(lambda r: httpx.Response(200, json=KALSHI_BOOK))
        depth = await md.executable_depth(db, _leg("kal_KXT", "kalshi", "SELL", 0.50))
        assert depth == 20.0  # 0.50 x12 + 0.52 x8

    async def test_empty_side_is_zero(self, db):
        await _seed(db, "kal_KXT", "kalshi", "KXT")
        md, _ = _md(
            lambda r: httpx.Response(
                200, json={"orderbook_fp": {"yes_dollars": None, "no_dollars": None}}
            )
        )
        assert (
            await md.executable_depth(db, _leg("kal_KXT", "kalshi", "SELL", 0.5)) == 0.0
        )


@pytest.mark.parametrize("status", [404, 500])
async def test_http_error_is_unknown(db, status):
    await _seed(db, "kal_KXT", "kalshi", "KXT")
    md, _ = _md(lambda r: httpx.Response(status))
    assert await md.executable_depth(db, _leg("kal_KXT", "kalshi", "BUY", 0.5)) is None


async def test_depth_is_not_cached(db):
    await _seed(db, "kal_KXT", "kalshi", "KXT")
    md, seen = _md(lambda r: httpx.Response(200, json=KALSHI_BOOK))
    leg = _leg("kal_KXT", "kalshi", "BUY", 0.55)
    await md.executable_depth(db, leg)
    await md.executable_depth(db, leg)
    assert len(seen) == 2
