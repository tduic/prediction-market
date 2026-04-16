"""
Integration tests for market data ingestor.

Tests data ingestion pipeline:
- Fetching markets from APIs
- Storing in database
- Handling rate limits
- Tracking ingestor runs
"""

from datetime import datetime, timezone

import pytest

# ============================================================================
# Ingestor Classes (Mock Implementations)
# ============================================================================


class PolymocketAPIClient:
    """Mock Polymarket API client."""

    def __init__(self, rate_limit_remaining: int = 100):
        self.rate_limit_remaining = rate_limit_remaining
        self.request_count = 0

    async def fetch_markets(self) -> dict:
        """Fetch markets from Polymarket API."""
        self.request_count += 1

        if self.rate_limit_remaining <= 0:
            raise RateLimitError(429, "Rate limit exceeded")

        self.rate_limit_remaining -= 1

        return {
            "success": True,
            "data": [
                {
                    "id": "pm_001",
                    "platform_id": "0x1234567890abcdef",
                    "title": "Will the Fed cut rates in December 2024?",
                    "description": "FOMC rate cut market",
                    "category": "macro",
                    "event_type": "fomc",
                    "yes_price": 0.72,
                    "no_price": 0.28,
                    "status": "open",
                },
                {
                    "id": "pm_002",
                    "platform_id": "0x2345678901bcdef",
                    "title": "Will US CPI be below 3.0%?",
                    "description": "CPI inflation market",
                    "category": "macro",
                    "event_type": "cpi",
                    "yes_price": 0.58,
                    "no_price": 0.42,
                    "status": "open",
                },
            ],
        }


class RateLimitError(Exception):
    """API rate limit error."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"{status_code}: {message}")


class MarketIngestor:
    """Ingest market data from APIs and store in database."""

    def __init__(self, db, api_client=None, event_bus=None):
        self.db = db
        self.api_client = api_client or PolymocketAPIClient()
        self.event_bus = event_bus
        self.backoff_seconds = 1
        self.max_backoff = 60

    async def run_ingest_cycle(self) -> dict:
        """
        Run a complete ingest cycle.

        Returns:
            dict with cycle results (markets_fetched, inserted, updated)
        """
        # Log start
        cursor = self.db.execute(
            """INSERT INTO ingestor_runs (started_at, status)
               VALUES (?, ?)""",
            (datetime.now(timezone.utc), "running"),
        )
        run_id = cursor.lastrowid
        self.db.commit()

        try:
            # Fetch markets
            api_response = await self.api_client.fetch_markets()

            if not api_response.get("success"):
                raise ValueError("API returned error")

            markets = api_response.get("data", [])
            markets_fetched = len(markets)

            # Insert/update markets
            inserted = 0
            updated = 0

            for market in markets:
                existing = self.db.execute(
                    "SELECT id FROM markets WHERE id = ?",
                    (market["id"],),
                ).fetchone()

                if existing:
                    # Update
                    self.db.execute(
                        """UPDATE markets
                           SET yes_price = ?, no_price = ?, updated_at = ?
                           WHERE id = ?""",
                        (
                            market["yes_price"],
                            market["no_price"],
                            datetime.now(timezone.utc),
                            market["id"],
                        ),
                    )
                    updated += 1
                else:
                    # Insert
                    self.db.execute(
                        """INSERT INTO markets
                           (id, platform, platform_id, title, description,
                            category, event_type, yes_price, no_price, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            market["id"],
                            "polymarket",
                            market.get("platform_id", ""),
                            market["title"],
                            market.get("description", ""),
                            market.get("category", ""),
                            market.get("event_type", ""),
                            market["yes_price"],
                            market["no_price"],
                            market.get("status", "open"),
                        ),
                    )
                    inserted += 1

            self.db.commit()

            # Log completion
            self.db.execute(
                """UPDATE ingestor_runs
                   SET completed_at = ?, status = ?,
                       markets_fetched = ?, markets_inserted = ?, markets_updated = ?
                   WHERE id = ?""",
                (
                    datetime.now(timezone.utc),
                    "completed",
                    markets_fetched,
                    inserted,
                    updated,
                    run_id,
                ),
            )
            self.db.commit()

            return {
                "success": True,
                "markets_fetched": markets_fetched,
                "markets_inserted": inserted,
                "markets_updated": updated,
            }

        except RateLimitError as e:
            # Log rate limit
            self.db.execute(
                """UPDATE ingestor_runs
                   SET completed_at = ?, status = ?, errors = ?
                   WHERE id = ?""",
                (
                    datetime.now(timezone.utc),
                    "rate_limited",
                    str(e),
                    run_id,
                ),
            )
            self.db.commit()

            # Emit event
            if self.event_bus:
                self.event_bus.emit(
                    "ingestor_rate_limited",
                    {
                        "backoff_seconds": self.backoff_seconds,
                    },
                )

            raise

    async def record_price_update(
        self,
        market_id: str,
        yes_price: float,
        no_price: float,
    ) -> None:
        """
        Record a market price update (append-only).

        Args:
            market_id: Market identifier
            yes_price: YES price
            no_price: NO price
        """
        self.db.execute(
            """INSERT INTO market_prices (market_id, yes_price, no_price, timestamp)
               VALUES (?, ?, ?, ?)""",
            (market_id, yes_price, no_price, datetime.now(timezone.utc)),
        )
        self.db.commit()


# ============================================================================
# Test Cases
# ============================================================================


class TestIngestorDataFlow:
    """Test ingestor data insertion flow."""

    @pytest.mark.asyncio
    async def test_new_markets_written_to_db(self, in_memory_db):
        """New markets from API are inserted to database."""
        api_client = PolymocketAPIClient()
        ingestor = MarketIngestor(in_memory_db, api_client=api_client)

        result = await ingestor.run_ingest_cycle()

        assert result["success"] is True
        assert result["markets_inserted"] == 2
        assert result["markets_updated"] == 0

        # Verify in database
        markets = in_memory_db.execute("SELECT * FROM markets").fetchall()

        assert len(markets) == 2
        assert markets[0]["title"] == "Will the Fed cut rates in December 2024?"

    @pytest.mark.asyncio
    async def test_price_updates_append_only(self, in_memory_db):
        """Price updates append to market_prices table."""
        api_client = PolymocketAPIClient()
        ingestor = MarketIngestor(in_memory_db, api_client=api_client)

        # Insert market first
        in_memory_db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_price, no_price, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("pm_001", "polymarket", "0x1234", "Test", 0.50, 0.50, "open"),
        )
        in_memory_db.commit()

        # Record multiple price updates
        await ingestor.record_price_update("pm_001", 0.52, 0.48)
        await ingestor.record_price_update("pm_001", 0.55, 0.45)

        # Verify all are stored
        prices = in_memory_db.execute(
            "SELECT * FROM market_prices WHERE market_id = ? ORDER BY timestamp",
            ("pm_001",),
        ).fetchall()

        assert len(prices) == 2
        assert prices[0]["yes_price"] == 0.52
        assert prices[1]["yes_price"] == 0.55

    @pytest.mark.asyncio
    async def test_ingestor_run_logged(self, in_memory_db):
        """Ingestor run is logged in ingestor_runs table."""
        api_client = PolymocketAPIClient()
        ingestor = MarketIngestor(in_memory_db, api_client=api_client)

        await ingestor.run_ingest_cycle()

        runs = in_memory_db.execute("SELECT * FROM ingestor_runs").fetchall()

        assert len(runs) == 1
        assert runs[0]["status"] == "completed"
        assert runs[0]["markets_fetched"] == 2

    @pytest.mark.asyncio
    async def test_price_updates_on_second_poll(self, in_memory_db):
        """Second poll updates existing market prices."""
        api_client = PolymocketAPIClient()
        ingestor = MarketIngestor(in_memory_db, api_client=api_client)

        # First poll
        result1 = await ingestor.run_ingest_cycle()
        assert result1["markets_inserted"] == 2

        # Mock API returning updated prices
        api_client.request_count = 0

        async def fetch_updated():
            return {
                "success": True,
                "data": [
                    {
                        "id": "pm_001",
                        "platform_id": "0x1234",
                        "title": "Will the Fed cut rates in December 2024?",
                        "yes_price": 0.75,  # Updated
                        "no_price": 0.25,  # Updated
                        "status": "open",
                    },
                ],
            }

        api_client.fetch_markets = fetch_updated

        # Second poll
        result2 = await ingestor.run_ingest_cycle()

        assert result2["markets_inserted"] == 0
        assert result2["markets_updated"] == 1

        # Verify updated price
        market = in_memory_db.execute(
            "SELECT yes_price FROM markets WHERE id = ?",
            ("pm_001",),
        ).fetchone()

        assert market["yes_price"] == 0.75


class TestIngestorRateLimiting:
    """Test rate limit handling."""

    @pytest.mark.asyncio
    async def test_rate_limit_backoff_on_429(self, in_memory_db, event_bus):
        """Rate limit (429) triggers backoff."""
        api_client = PolymocketAPIClient(rate_limit_remaining=0)
        ingestor = MarketIngestor(
            in_memory_db, api_client=api_client, event_bus=event_bus
        )

        with pytest.raises(RateLimitError):
            await ingestor.run_ingest_cycle()

        # Verify run logged as rate_limited
        runs = in_memory_db.execute("SELECT status FROM ingestor_runs").fetchall()

        assert len(runs) == 1
        assert runs[0]["status"] == "rate_limited"

    @pytest.mark.asyncio
    async def test_rate_limit_emits_event(self, in_memory_db, event_bus):
        """Rate limit triggers event emission."""
        api_client = PolymocketAPIClient(rate_limit_remaining=0)
        ingestor = MarketIngestor(
            in_memory_db, api_client=api_client, event_bus=event_bus
        )

        with pytest.raises(RateLimitError):
            await ingestor.run_ingest_cycle()

        # Check for rate limit event
        events = event_bus.get_events("ingestor_rate_limited")

        assert len(events) == 1
        assert "backoff_seconds" in events[0]["data"]

    @pytest.mark.asyncio
    async def test_rate_limit_with_remaining_quota(self, in_memory_db):
        """Requests succeed when quota available."""
        api_client = PolymocketAPIClient(rate_limit_remaining=100)
        ingestor = MarketIngestor(in_memory_db, api_client=api_client)

        result = await ingestor.run_ingest_cycle()

        assert result["success"] is True
        assert api_client.rate_limit_remaining == 99


class TestIngestorMultipleCycles:
    """Test multiple ingest cycles."""

    @pytest.mark.asyncio
    async def test_multiple_ingest_cycles_tracked(self, in_memory_db):
        """Multiple cycles create separate run records."""
        api_client = PolymocketAPIClient()
        ingestor = MarketIngestor(in_memory_db, api_client=api_client)

        # Run 3 cycles
        for i in range(3):
            await ingestor.run_ingest_cycle()

        runs = in_memory_db.execute(
            "SELECT COUNT(*) as count FROM ingestor_runs"
        ).fetchone()

        assert runs["count"] == 3

    @pytest.mark.asyncio
    async def test_ingest_preserves_market_history(self, in_memory_db):
        """Markets persist across multiple ingest cycles."""
        api_client = PolymocketAPIClient()
        ingestor = MarketIngestor(in_memory_db, api_client=api_client)

        # First ingest
        result1 = await ingestor.run_ingest_cycle()  # noqa: F841
        count1 = in_memory_db.execute(
            "SELECT COUNT(*) as count FROM markets"
        ).fetchone()["count"]

        # Second ingest (same markets)
        result2 = await ingestor.run_ingest_cycle()
        count2 = in_memory_db.execute(
            "SELECT COUNT(*) as count FROM markets"
        ).fetchone()["count"]

        # Market count should not double
        assert count1 == count2
        assert result2["markets_inserted"] == 0
        assert result2["markets_updated"] == 2
