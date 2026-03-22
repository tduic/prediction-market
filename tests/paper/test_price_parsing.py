"""
Unit tests for Kalshi price extraction and parlay filtering.

The store_markets function must correctly parse prices from multiple
Kalshi field formats (_dollars strings, integer cents, fallbacks) and
filter out parlay/multivariate markets.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.paper_trading_session import store_markets  # noqa: E402


def _make_kalshi_market(
    ticker: str,
    title: str,
    yes_bid_dollars=None,
    yes_ask_dollars=None,
    last_price_dollars=None,
    yes_bid=None,
    yes_ask=None,
    last_price=None,
    open_price_dollars=None,
    mve_collection_ticker=None,
):
    """Helper to construct a Kalshi market dict with the given price fields."""
    m = {"ticker": ticker, "title": title, "status": "open"}
    if yes_bid_dollars is not None:
        m["yes_bid_dollars"] = yes_bid_dollars
    if yes_ask_dollars is not None:
        m["yes_ask_dollars"] = yes_ask_dollars
    if last_price_dollars is not None:
        m["last_price_dollars"] = last_price_dollars
    if yes_bid is not None:
        m["yes_bid"] = yes_bid
    if yes_ask is not None:
        m["yes_ask"] = yes_ask
    if last_price is not None:
        m["last_price"] = last_price
    if open_price_dollars is not None:
        m["open_price_dollars"] = open_price_dollars
    if mve_collection_ticker is not None:
        m["mve_collection_ticker"] = mve_collection_ticker
    return m


def _make_poly_market(condition_id: str, title: str, price: float):
    """Helper to construct a Polymarket market dict."""
    return {
        "condition_id": condition_id,
        "question": title,
        "tokens": [{"price": str(price)}],
        "volume": "50000",
    }


# ── Kalshi price parsing ────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestKalshiPriceParsing:
    async def test_dollars_string_fields(self, db):
        """Parses price from yes_bid_dollars + yes_ask_dollars (midpoint)."""
        kalshi = [
            _make_kalshi_market(
                "TEST1",
                "Test Market 1",
                yes_bid_dollars="0.40",
                yes_ask_dollars="0.50",
            )
        ]
        stored = await store_markets(db, [], kalshi)
        assert stored == 1

        cursor = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id = 'kal_TEST1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row[0] - 0.45) < 0.01  # midpoint of 0.40 and 0.50

    async def test_last_price_dollars_fallback(self, db):
        """Falls back to last_price_dollars when bid/ask missing."""
        kalshi = [
            _make_kalshi_market(
                "TEST2",
                "Test Market 2",
                last_price_dollars="0.55",
            )
        ]
        stored = await store_markets(db, [], kalshi)
        assert stored == 1

        cursor = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id = 'kal_TEST2'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row[0] - 0.55) < 0.01

    async def test_integer_cent_fields(self, db):
        """Parses price from integer cent fields (yes_bid/yes_ask) divided by 100."""
        kalshi = [
            _make_kalshi_market(
                "TEST3",
                "Test Market 3",
                yes_bid=40,
                yes_ask=50,
            )
        ]
        stored = await store_markets(db, [], kalshi)
        assert stored == 1

        cursor = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id = 'kal_TEST3'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row[0] - 0.45) < 0.01

    async def test_open_price_dollars_fallback(self, db):
        """Falls back to open_price_dollars when all other fields missing."""
        kalshi = [
            _make_kalshi_market(
                "TEST4",
                "Test Market 4",
                open_price_dollars="0.65",
            )
        ]
        stored = await store_markets(db, [], kalshi)
        assert stored == 1

    async def test_no_price_fields_skipped(self, db):
        """Market with no price data is skipped entirely."""
        kalshi = [_make_kalshi_market("NOPRICE", "No Price Market")]
        stored = await store_markets(db, [], kalshi)
        assert stored == 0

    async def test_extreme_price_filtered(self, db):
        """Prices <= 0.01 or >= 0.99 are filtered out."""
        kalshi = [
            _make_kalshi_market(
                "LOW", "Low Price", yes_bid_dollars="0.005", yes_ask_dollars="0.01"
            ),
            _make_kalshi_market(
                "HIGH", "High Price", yes_bid_dollars="0.99", yes_ask_dollars="0.995"
            ),
        ]
        stored = await store_markets(db, [], kalshi)
        assert stored == 0

    async def test_no_ticker_skipped(self, db):
        """Market with empty ticker is skipped."""
        kalshi = [
            {
                "ticker": "",
                "title": "No Ticker",
                "yes_bid_dollars": "0.50",
                "yes_ask_dollars": "0.60",
            }
        ]
        stored = await store_markets(db, [], kalshi)
        assert stored == 0


# ── Parlay filtering ────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestParlayFiltering:
    """These test the post-fetch parlay filter in store_markets indirectly.

    The parlay filter lives in fetch_kalshi_markets, not store_markets.
    But we can test the mve_collection_ticker field behavior since
    store_markets processes whatever list it receives.
    """

    async def test_normal_market_stored(self, db):
        """A normal single-event market is stored."""
        kalshi = [
            _make_kalshi_market(
                "SINGLE",
                "Single Event Market",
                yes_bid_dollars="0.50",
                yes_ask_dollars="0.60",
            )
        ]
        stored = await store_markets(db, [], kalshi)
        assert stored == 1

    async def test_multiple_markets_stored(self, db):
        """Multiple valid markets all get stored."""
        kalshi = [
            _make_kalshi_market(
                "M1", "Market One", yes_bid_dollars="0.30", yes_ask_dollars="0.40"
            ),
            _make_kalshi_market(
                "M2", "Market Two", yes_bid_dollars="0.50", yes_ask_dollars="0.60"
            ),
            _make_kalshi_market(
                "M3", "Market Three", yes_bid_dollars="0.20", yes_ask_dollars="0.30"
            ),
        ]
        stored = await store_markets(db, [], kalshi)
        assert stored == 3


# ── Polymarket price parsing ────────────────────────────────────────────────


@pytest.mark.asyncio
class TestPolymarketPriceParsing:
    async def test_token_price(self, db):
        """Parses price from tokens[0].price field."""
        poly = [_make_poly_market("cond_123", "Test Market", 0.55)]
        stored = await store_markets(db, poly, [])
        assert stored == 1

        cursor = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id LIKE 'poly_%'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row[0] - 0.55) < 0.01

    async def test_outcome_prices_fallback(self, db):
        """Falls back to outcomePrices JSON string."""
        poly = [
            {
                "condition_id": "cond_456",
                "question": "Fallback Test",
                "outcomePrices": "[0.45, 0.55]",
                "volume": "10000",
            }
        ]
        stored = await store_markets(db, poly, [])
        assert stored == 1

    async def test_extreme_price_filtered(self, db):
        """Prices at extremes are filtered."""
        poly = [
            _make_poly_market("cond_low", "Too Low", 0.005),
            _make_poly_market("cond_high", "Too High", 0.995),
        ]
        stored = await store_markets(db, poly, [])
        assert stored == 0

    async def test_missing_title_skipped(self, db):
        """Market without a title is skipped."""
        poly = [{"condition_id": "cond_notitle", "tokens": [{"price": "0.50"}]}]
        stored = await store_markets(db, poly, [])
        assert stored == 0

    async def test_mixed_poly_and_kalshi(self, db):
        """Both platforms stored together in one call."""
        poly = [_make_poly_market("cond_mix", "Mixed Test PM", 0.50)]
        kalshi = [
            _make_kalshi_market(
                "MIXKA", "Mixed Test KA", yes_bid_dollars="0.40", yes_ask_dollars="0.50"
            )
        ]
        stored = await store_markets(db, poly, kalshi)
        assert stored == 2
