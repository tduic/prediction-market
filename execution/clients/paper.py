"""
Paper trading execution client.

Runs the full execution pipeline with real market data but does NOT
place orders on any exchange. Uses actual market prices from the DB
for fill simulation, producing analytics identical to live mode.

Enable via: EXECUTION_MODE=paper
"""

import logging
import random
import time
import uuid

import aiosqlite

from execution.clients.base import BaseExecutionClient, OrderResult
from execution.clients.polymarket_book import BookResolver, ResolvedOrder
from execution.enums import Book, Side
from execution.models import OrderLeg

logger = logging.getLogger(__name__)

# Use realistic fee rates matching each platform
FEE_RATES = {
    "polymarket": 0.02,
    "kalshi": 0.07,
}


def _is_valid_price(price: float | None) -> bool:
    """Prediction-market prices must be strictly in (0, 1).

    The Kalshi and Polymarket ingestors default ``last_price`` to 0.0 when
    the field is missing from the upstream response, and the DB can also
    hold stale 0.0 rows. Treat anything outside the open interval as a
    "no price" signal so the caller falls back to the limit price or
    rejects the order, instead of booking a fill at $0.00.
    """
    return price is not None and 0.0 < price < 1.0


class PaperExecutionClient(BaseExecutionClient):
    """
    Paper trading client: real prices, no real orders.

    Fills are simulated using the latest market prices from the DB
    (written by the ingestor). This gives realistic PnL tracking
    without any capital at risk.

    The data contract is identical to the live clients — same tables,
    same fields, same format. Analytics and dashboard work the same way.
    """

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        platform_label: str = "paper_polymarket",
        slippage_bps: float = 0.0,
        fee_rate: float | None = None,
    ) -> None:
        super().__init__(db_connection, platform_label=platform_label)

        # Slippage: adverse price movement applied at fill time.
        # 0 = no slippage (backward compat default). Set via RiskControlConfig.slippage_bps.
        self.slippage_bps = slippage_bps

        # Override platform fee rate. None = use FEE_RATES dict default.
        self._fee_rate_override = fee_rate

        # Simulated latency range (realistic but no actual network call)
        self.min_latency_ms = 80
        self.max_latency_ms = 400
        self.min_fill_latency_ms = 150
        self.max_fill_latency_ms = 1500

        # BookResolver for Polymarket no-naked-shorts translation.
        self._book_resolver = (
            BookResolver(db_connection) if platform_label == "polymarket" else None
        )

        # Stats
        self.total_submitted = 0
        self.total_filled = 0

    async def _get_current_price(
        self, market_id: str, book: Book = Book.YES
    ) -> float | None:
        """Fetch the live price from the exchange API, falling back to DB.

        Tries to call the exchange's price endpoint directly so paper/shadow
        fills use the actual market price at order time, not stale polled data.
        Falls back to the most recent DB price if the live fetch fails.

        For NO-book legs (translated Polymarket short-sells), skips the live
        fetch and reads ``markets.last_price_no`` directly — there is no
        dedicated API endpoint for the NO token price.
        """
        if book is Book.YES:
            try:
                cursor = await self.db.execute(
                    "SELECT platform, platform_id FROM markets WHERE id = ?",
                    (market_id,),
                )
                row = await cursor.fetchone()
                if row:
                    platform, platform_id = row[0], row[1]
                    live_price = await self._fetch_live_price(platform, platform_id)
                    if _is_valid_price(live_price):
                        return live_price
            except BaseException as e:
                logger.debug(
                    "[PAPER] Live price lookup failed for %s: %s", market_id, e
                )

        db_price = await self._get_db_price(market_id, book)
        return db_price if _is_valid_price(db_price) else None

    async def _fetch_live_price(self, platform: str, platform_id: str) -> float | None:
        """Call the exchange API for the current price."""
        try:
            if platform == "polymarket":
                from core.ingestor.polymarket import PolymarketClient

                async with PolymarketClient() as client:
                    market = await client.get_market(platform_id)
                    return market.last_price if market else None
            elif platform == "kalshi":
                from core.ingestor.kalshi import KalshiClient

                async with KalshiClient() as client:
                    market = await client.get_market(platform_id)
                    return market.last_price if market else None
        except BaseException as e:
            logger.debug(
                "[PAPER] %s price API error for %s: %s", platform, platform_id, e
            )
        return None
