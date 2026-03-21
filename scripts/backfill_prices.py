"""
One-off script to backfill historical market prices into the database.

Fetches historical prices from API and writes to market_prices table.
"""

import argparse
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
import httpx
from tqdm.asyncio import tqdm

logger = logging.getLogger(__name__)


class PriceBackfiller:
    """Backfill historical market prices."""

    def __init__(
        self,
        db_path: str,
        platform: str,
        polymarket_api_base: str = "https://api.polymarket.com",
        kalshi_api_base: str = "https://trading-api.kalshi.com/trade-api/v2",
    ) -> None:
        """
        Initialize the price backfiller.

        Args:
            db_path: Path to the SQLite database
            platform: Platform to backfill (polymarket or kalshi)
            polymarket_api_base: Base URL for Polymarket API
            kalshi_api_base: Base URL for Kalshi API
        """
        self.db_path = db_path
        self.platform = platform.lower()
        self.polymarket_api_base = polymarket_api_base
        self.kalshi_api_base = kalshi_api_base
        self.http_client = httpx.AsyncClient(timeout=30)

    async def get_polymarket_prices(
        self,
        since: datetime,
        until: datetime,
    ) -> list[dict]:
        """
        Fetch historical prices from Polymarket API.

        Args:
            since: Start date for historical data
            until: End date for historical data

        Returns:
            List of price records
        """
        logger.info(
            "Fetching Polymarket prices from %s to %s",
            since.isoformat(),
            until.isoformat(),
        )

        prices: list[dict] = []

        try:
            # Get all markets
            response = await self.http_client.get(
                f"{self.polymarket_api_base}/markets",
                params={"active": "true"},
            )

            if response.status_code != 200:
                logger.error("Failed to fetch markets: %s", response.text)
                return prices

            markets = response.json()

            # Fetch price history for each market
            for market in tqdm(markets, desc="Polymarket markets"):
                market_id = market.get("id")
                if not market_id:
                    continue

                try:
                    # Polymarket price history endpoint
                    hist_response = await self.http_client.get(
                        f"{self.polymarket_api_base}/markets/{market_id}/history",
                        params={
                            "start_date": int(since.timestamp()),
                            "end_date": int(until.timestamp()),
                        },
                    )

                    if hist_response.status_code == 200:
                        history = hist_response.json()

                        for entry in history:
                            prices.append(
                                {
                                    "market_id": market_id,
                                    "platform": "polymarket",
                                    "timestamp_utc": datetime.fromtimestamp(
                                        entry.get("timestamp", 0)
                                    ).isoformat(),
                                    "mid_price": entry.get("mid_price"),
                                    "bid": entry.get("bid"),
                                    "ask": entry.get("ask"),
                                }
                            )

                except Exception as e:
                    logger.warning(
                        "Error fetching history for market %s: %s",
                        market_id,
                        e,
                    )

        except Exception as e:
            logger.error("Error fetching Polymarket prices: %s", exc_info=e)

        return prices

    async def get_kalshi_prices(
        self,
        since: datetime,
        until: datetime,
    ) -> list[dict]:
        """
        Fetch historical prices from Kalshi API.

        Args:
            since: Start date for historical data
            until: End date for historical data

        Returns:
            List of price records
        """
        logger.info(
            "Fetching Kalshi prices from %s to %s",
            since.isoformat(),
            until.isoformat(),
        )

        prices: list[dict] = []

        try:
            # Get all markets
            response = await self.http_client.get(
                f"{self.kalshi_api_base}/markets",
                params={"limit": 500},
            )

            if response.status_code != 200:
                logger.error("Failed to fetch markets: %s", response.text)
                return prices

            data = response.json()
            markets = data.get("markets", [])

            # Fetch price history for each market
            for market in tqdm(markets, desc="Kalshi markets"):
                market_id = market.get("id")
                if not market_id:
                    continue

                try:
                    # Kalshi history endpoint
                    hist_response = await self.http_client.get(
                        f"{self.kalshi_api_base}/markets/{market_id}/history",
                        params={
                            "start_date": int(since.timestamp() * 1000),
                            "end_date": int(until.timestamp() * 1000),
                        },
                    )

                    if hist_response.status_code == 200:
                        history = hist_response.json()

                        for entry in history:
                            prices.append(
                                {
                                    "market_id": market_id,
                                    "platform": "kalshi",
                                    "timestamp_utc": datetime.fromtimestamp(
                                        entry.get("timestamp_ms", 0) / 1000
                                    ).isoformat(),
                                    "mid_price": entry.get("mid_price"),
                                    "bid": entry.get("bid"),
                                    "ask": entry.get("ask"),
                                }
                            )

                except Exception as e:
                    logger.warning(
                        "Error fetching history for market %s: %s",
                        market_id,
                        e,
                    )

        except Exception as e:
            logger.error("Error fetching Kalshi prices: %s", exc_info=e)

        return prices

    async def write_prices_to_db(self, prices: list[dict]) -> int:
        """
        Write price records to the database.

        Args:
            prices: List of price records

        Returns:
            Number of records written
        """
        if not prices:
            logger.warning("No prices to write")
            return 0

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.cursor()

            for price in tqdm(prices, desc="Writing to database"):
                try:
                    await cursor.execute(
                        """
                        INSERT OR IGNORE INTO market_prices
                        (market_id, platform, timestamp_utc, mid_price, bid, ask)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            price["market_id"],
                            price["platform"],
                            price["timestamp_utc"],
                            price.get("mid_price"),
                            price.get("bid"),
                            price.get("ask"),
                        ),
                    )
                except Exception as e:
                    logger.warning("Error writing price record: %s", e)

            await db.commit()

        logger.info("Wrote %d price records to database", len(prices))
        return len(prices)

    async def backfill(
        self,
        since: str | None,
        until: str | None,
    ) -> int:
        """
        Run the backfill process.

        Args:
            since: Start date (YYYY-MM-DD) or days back
            until: End date (YYYY-MM-DD)

        Returns:
            Number of records written
        """
        try:
            # Parse dates
            if until:
                end_date = datetime.fromisoformat(until)
            else:
                end_date = datetime.now(timezone.utc)

            if since:
                try:
                    start_date = datetime.fromisoformat(since)
                except ValueError:
                    # Assume it's a number of days back
                    days_back = int(since)
                    start_date = end_date - timedelta(days=days_back)
            else:
                start_date = end_date - timedelta(days=30)

            logger.info(
                "Backfilling %s prices from %s to %s",
                self.platform,
                start_date.isoformat(),
                end_date.isoformat(),
            )

            # Fetch prices
            if self.platform == "polymarket":
                prices = await self.get_polymarket_prices(start_date, end_date)
            elif self.platform == "kalshi":
                prices = await self.get_kalshi_prices(start_date, end_date)
            else:
                logger.error("Unknown platform: %s", self.platform)
                return 0

            # Write to database
            count = await self.write_prices_to_db(prices)

            return count

        except Exception as e:
            logger.error("Error during backfill: %s", exc_info=e)
            return 0

    async def close(self) -> None:
        """Close HTTP client."""
        await self.http_client.aclose()


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill historical market prices",
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=["polymarket", "kalshi"],
        help="Platform to backfill",
    )
    parser.add_argument(
        "--since",
        help="Start date (YYYY-MM-DD) or days back (default: 30 days ago)",
    )
    parser.add_argument(
        "--until",
        help="End date (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--db",
        default="prediction_market.db",
        help="Path to SQLite database",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    backfiller = PriceBackfiller(
        db_path=args.db,
        platform=args.platform,
    )

    try:
        count = await backfiller.backfill(
            since=args.since,
            until=args.until,
        )
        logger.info("Backfill completed: %d records", count)
    finally:
        await backfiller.close()


if __name__ == "__main__":
    asyncio.run(main())
