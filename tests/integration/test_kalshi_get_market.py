"""KalshiClient.get_market against the current /markets/{ticker} schema.

The fixture is a real response: the market is wrapped in ``{"market": {...}}``
and prices/sizes are strings in ``*_dollars`` / ``*_fp`` fields (the old
integer-cent ``last_price`` / ``yes_bid`` / ``volume`` fields are gone).
execution/clients/paper.py uses ``get_market(...).last_price`` as the live
Kalshi reference price for paper fills.
"""

import json
from pathlib import Path

import httpx
import pytest

from core.ingestor.kalshi import KalshiClient

FIXTURE = Path(__file__).parent.parent / "fixtures" / "kalshi_market_single.json"
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _payload() -> dict:
    data = json.loads(FIXTURE.read_text())
    data.pop("_source", None)
    return data


def _client(handler) -> KalshiClient:
    client = KalshiClient(api_key="", rsa_key_path="/nonexistent", api_base=BASE)
    client._private_key = None  # public endpoint, unsigned
    client._client = httpx.AsyncClient(
        base_url=BASE, transport=httpx.MockTransport(handler)
    )
    return client


async def _get(payload: dict, ticker: str = "KXNBATOTAL-26OCT23HOUSAS-208"):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=payload)

    client = _client(handler)
    try:
        market = await client.get_market(ticker)
    finally:
        await client.client.aclose()
    return market, seen


async def test_parses_wrapped_market_with_dollar_fields():
    market, seen = await _get(_payload())
    assert seen[0].url.path == "/trade-api/v2/markets/KXNBATOTAL-26OCT23HOUSAS-208"
    assert market is not None
    assert market.market_id == "KXNBATOTAL-26OCT23HOUSAS-208"
    assert market.question.startswith("Full Game: Over 207.5")
    assert market.is_active is True
    # No trades yet (last_price_dollars "0.0000"): the live reference is the
    # mid of the quoted YES book, 0.69 / 0.88.
    assert market.last_price == pytest.approx((0.69 + 0.88) / 2)
    assert market.last_price_no == pytest.approx((0.12 + 0.31) / 2)
    md = market.metadata
    assert md["yes_bid"] == pytest.approx(0.69)
    assert md["yes_ask"] == pytest.approx(0.88)
    assert md["last_trade_price"] == pytest.approx(0.0)
    assert md["volume"] == pytest.approx(0.0)
    assert md["open_interest"] == pytest.approx(0.0)
    assert market.resolution_date is not None


async def test_falls_back_to_last_trade_when_book_is_one_sided():
    payload = _payload()
    payload["market"].update(
        {"yes_bid_dollars": "0.0000", "last_price_dollars": "0.6100"}
    )
    market, _ = await _get(payload)
    assert market is not None
    assert market.last_price == pytest.approx(0.61)


async def test_no_price_at_all_is_zero_not_an_exception():
    payload = _payload()
    payload["market"].update(
        {
            "yes_bid_dollars": "0.0000",
            "yes_ask_dollars": "0.0000",
            "last_price_dollars": "0.0000",
        }
    )
    market, _ = await _get(payload)
    assert market is not None
    assert market.last_price == 0.0  # paper client treats 0 as "no price"


async def test_unwrapped_legacy_cent_fields_still_parse():
    legacy = {
        "ticker": "OLD-1",
        "title": "Old",
        "status": "active",
        "yes_bid": 40,
        "yes_ask": 44,
        "last_price": 41,
        "volume": 1200,
    }
    market, _ = await _get(legacy, "OLD-1")
    assert market is not None
    assert market.market_id == "OLD-1"
    assert market.last_price == pytest.approx(0.42)
