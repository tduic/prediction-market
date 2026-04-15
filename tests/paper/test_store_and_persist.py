"""
Integration tests for store_markets, persist_matches, and load_cached_matches.

Tests the full pipeline of storing market data, persisting matched pairs,
and loading them back from cache.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.ingestor.store import store_markets  # noqa: E402
from core.matching.engine import (  # noqa: E402
    find_matches,
    load_cached_matches,
    persist_matches,
)


def _poly(cond_id, title, price):
    return {
        "condition_id": cond_id,
        "question": title,
        "tokens": [{"price": str(price)}],
        "volume": "10000",
    }


def _kalshi(ticker, title, bid, ask):
    return {
        "ticker": ticker,
        "title": title,
        "yes_bid_dollars": str(bid),
        "yes_ask_dollars": str(ask),
    }


@pytest.mark.asyncio
class TestStoreMarkets:
    async def test_stores_polymarket(self, db):
        poly = [_poly("cond_1", "Test PM Market", 0.50)]
        count = await store_markets(db, poly, [])
        assert count == 1

        cursor = await db.execute(
            "SELECT COUNT(*) FROM markets WHERE platform = 'polymarket'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1

    async def test_stores_kalshi(self, db):
        kalshi = [_kalshi("TICK1", "Test KA Market", 0.40, 0.50)]
        count = await store_markets(db, [], kalshi)
        assert count == 1

        cursor = await db.execute(
            "SELECT COUNT(*) FROM markets WHERE platform = 'kalshi'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1

    async def test_stores_both(self, db):
        poly = [_poly("cond_both", "Both PM", 0.50)]
        kalshi = [_kalshi("BOTH", "Both KA", 0.40, 0.50)]
        count = await store_markets(db, poly, kalshi)
        assert count == 2

    async def test_writes_prices(self, db):
        poly = [_poly("cond_prices", "Price Test", 0.55)]
        await store_markets(db, poly, [])

        cursor = await db.execute(
            "SELECT yes_price, no_price FROM market_prices WHERE market_id LIKE 'poly_%'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert abs(row[0] - 0.55) < 0.01
        assert abs(row[1] - 0.45) < 0.01  # 1 - 0.55

    async def test_idempotent_upsert(self, db):
        """Storing the same market twice uses INSERT OR REPLACE."""
        poly = [_poly("cond_dup", "Dup Market", 0.50)]
        await store_markets(db, poly, [])
        await store_markets(db, poly, [])

        cursor = await db.execute(
            "SELECT COUNT(*) FROM markets WHERE id LIKE 'poly_cond_dup%'"
        )
        row = await cursor.fetchone()
        assert row[0] == 1  # No duplicate market row

    async def test_empty_input(self, db):
        count = await store_markets(db, [], [])
        assert count == 0


@pytest.mark.asyncio
class TestPersistMatches:
    async def test_persist_and_count(self, db_with_markets):
        matches = await find_matches(db_with_markets, threshold=0.40)
        if not matches:
            pytest.skip("No matches found in test data")

        count = await persist_matches(db_with_markets, matches)
        assert count == len(matches)

        cursor = await db_with_markets.execute("SELECT COUNT(*) FROM market_pairs")
        row = await cursor.fetchone()
        assert row[0] == count

    async def test_persist_idempotent(self, db_with_markets):
        """Persisting same matches twice doesn't duplicate."""
        matches = await find_matches(db_with_markets, threshold=0.40)
        if not matches:
            pytest.skip("No matches found in test data")

        await persist_matches(db_with_markets, matches)
        await persist_matches(db_with_markets, matches)

        cursor = await db_with_markets.execute("SELECT COUNT(*) FROM market_pairs")
        row = await cursor.fetchone()
        assert row[0] == len(matches)

    async def test_persist_empty(self, db):
        count = await persist_matches(db, [])
        assert count == 0


@pytest.mark.asyncio
class TestLoadCachedMatches:
    async def test_load_after_persist(self, db_with_markets):
        """load_cached_matches returns what was persisted."""
        matches = await find_matches(db_with_markets, threshold=0.40)
        if not matches:
            pytest.skip("No matches found in test data")

        await persist_matches(db_with_markets, matches)
        loaded = await load_cached_matches(db_with_markets)

        assert len(loaded) == len(matches)

        # Verify structure
        m = loaded[0]
        assert "poly_id" in m
        assert "kalshi_id" in m
        assert "poly_price" in m
        assert "kalshi_price" in m

    async def test_load_empty_db(self, db):
        """Empty DB returns empty list."""
        loaded = await load_cached_matches(db)
        assert loaded == []

    async def test_load_has_prices(self, db_with_markets):
        """Loaded matches include latest prices from market_prices table."""
        matches = await find_matches(db_with_markets, threshold=0.40)
        if not matches:
            pytest.skip("No matches found in test data")

        await persist_matches(db_with_markets, matches)
        loaded = await load_cached_matches(db_with_markets)

        for m in loaded:
            assert m["poly_price"] is not None or m["kalshi_price"] is not None
            if m["poly_price"] is not None:
                assert 0 < m["poly_price"] < 1
            if m["kalshi_price"] is not None:
                assert 0 < m["kalshi_price"] < 1


@pytest.mark.asyncio
class TestEndToEndPipeline:
    async def test_store_match_persist_load(self, db):
        """Full pipeline: store → match → persist → load → verify."""
        # 1. Store markets with clearly matching titles
        poly = [
            _poly("cond_btc", "Will Bitcoin exceed $100,000 by end of 2025?", 0.55),
            _poly(
                "cond_fed", "Will the Federal Reserve cut rates in March 2026?", 0.40
            ),
        ]
        kalshi = [
            _kalshi("BTC100K", "Bitcoin above $100,000 by end of 2025?", 0.58, 0.62),
            _kalshi("FEDCUT", "Federal Reserve rate cut in March 2026?", 0.42, 0.48),
        ]

        stored = await store_markets(db, poly, kalshi)
        assert stored == 4

        # 2. Find matches
        matches = await find_matches(db, threshold=0.50)
        assert len(matches) >= 1  # At least BTC should match

        # 3. Persist
        await persist_matches(db, matches)

        # 4. Load
        loaded = await load_cached_matches(db)
        assert len(loaded) == len(matches)

        # 5. Verify loaded data has correct IDs
        loaded_poly_ids = {m["poly_id"] for m in loaded}
        loaded_kalshi_ids = {m["kalshi_id"] for m in loaded}

        for m in matches:
            assert m["poly_id"] in loaded_poly_ids
            assert m["kalshi_id"] in loaded_kalshi_ids
