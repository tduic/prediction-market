"""
Mock execution client for safe end-to-end testing.

Simulates realistic order fills without touching any real platform.
Configurable fill probability, latency, slippage, and partial fills.
Writes to the same DB tables as the real clients so the entire
analytics pipeline works identically.

Used internally by run_mock_session.py for synthetic end-to-end testing.
"""

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field

import aiosqlite

from execution.models import OrderLeg

logger = logging.getLogger(__name__)


@dataclass
class MockConfig:
    """Configuration for the mock execution client."""

    # Probability that a submitted order fills (0.0 - 1.0)
    fill_probability: float = 0.95

    # Probability of a partial fill (given the order does fill)
    partial_fill_probability: float = 0.05

    # When partial, fill this fraction of requested size
    partial_fill_fraction: float = 0.6

    # Simulated submission latency range (ms)
    min_latency_ms: int = 50
    max_latency_ms: int = 300

    # Simulated fill latency range (ms) — time after submission
    min_fill_latency_ms: int = 200
    max_fill_latency_ms: int = 2000

    # Max slippage from limit price (absolute, 0-1 scale)
    max_slippage: float = 0.01

    # Simulated fee rate (matches real platform fees for realistic P&L)
    fee_rate: float = 0.02

    # If True, log every mock action at INFO level (noisy but useful)
    verbose: bool = True

    @classmethod
    def from_env(cls) -> "MockConfig":
        """Load mock config from environment variables."""
        import os

        return cls(
            fill_probability=float(os.getenv("MOCK_FILL_PROBABILITY", "0.95")),
            partial_fill_probability=float(
                os.getenv("MOCK_PARTIAL_FILL_PROBABILITY", "0.05")
            ),
            partial_fill_fraction=float(os.getenv("MOCK_PARTIAL_FILL_FRACTION", "0.6")),
            min_latency_ms=int(os.getenv("MOCK_MIN_LATENCY_MS", "50")),
            max_latency_ms=int(os.getenv("MOCK_MAX_LATENCY_MS", "300")),
            min_fill_latency_ms=int(os.getenv("MOCK_MIN_FILL_LATENCY_MS", "200")),
            max_fill_latency_ms=int(os.getenv("MOCK_MAX_FILL_LATENCY_MS", "2000")),
            max_slippage=float(os.getenv("MOCK_MAX_SLIPPAGE", "0.01")),
            fee_rate=float(os.getenv("MOCK_FEE_RATE", "0.02")),
            verbose=os.getenv("MOCK_VERBOSE", "true").lower() == "true",
        )


@dataclass
class MockOrderResult:
    """Result of a mock order submission."""

    order_id: str
    leg_index: int = 0
    platform: str = "mock"
    status: str = "ACCEPTED"  # ACCEPTED | REJECTED | PENDING
    submission_latency_ms: int = 0
    fill_latency_ms: int | None = None
    filled_price: float | None = None
    filled_size: float | None = None
    fee_paid: float | None = None
    slippage: float | None = None
    error_message: str | None = None


@dataclass
class MockOrderBook:
    """Simulated order book state for a single market."""

    market_id: str
    yes_price: float = 0.50
    no_price: float = 0.50
    spread: float = 0.02
    liquidity: float = 10000.0
    last_updated: float = field(default_factory=time.time)

    def simulate_fill_price(
        self, side: str, limit_price: float | None, max_slippage: float
    ) -> float:
        """
        Simulate a realistic fill price given side and limit.

        For BUY YES: fill at or slightly above the current yes_price.
        For SELL YES (buy NO): fill at or slightly above no_price.
        Slippage is random within bounds, never exceeds limit_price.
        """
        if side.upper() in ("BUY", "YES"):
            base = self.yes_price
        else:
            base = self.no_price

        # Add random slippage (adverse to the trader)
        slippage = random.uniform(0, max_slippage)
        fill_price = base + slippage

        # Respect limit price ceiling
        if limit_price is not None:
            fill_price = min(fill_price, limit_price)

        # Clamp to valid range
        return max(0.001, min(0.999, round(fill_price, 4)))


class MockExecutionClient:
    """
    Mock execution client that simulates realistic trading behavior.

    Writes to the real `orders` and `order_events` DB tables so the
    entire downstream pipeline (positions, P&L, analytics) works
    exactly as it would in production.
    """

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        config: MockConfig | None = None,
        platform_label: str = "mock",
    ) -> None:
        """
        Initialize the mock execution client.

        Args:
            db_connection: SQLite connection (shared with core)
            config: Mock behavior configuration
            platform_label: Label written to `orders.platform` column.
                            Use "mock_polymarket" / "mock_kalshi" for
                            platform-specific mock testing.
        """
        self.db = db_connection
        self.config = config or MockConfig()
        self.platform_label = platform_label

        # Simulated order book per market
        self._order_books: dict[str, MockOrderBook] = {}

        # Track all mock orders for inspection
        self.order_history: list[MockOrderResult] = []

        # Stats
        self.total_submitted = 0
        self.total_filled = 0
        self.total_rejected = 0
        self.total_partial = 0

    def _get_order_book(self, market_id: str) -> MockOrderBook:
        """Get or create a simulated order book for a market."""
        if market_id not in self._order_books:
            # Try to initialize from DB price data, fall back to defaults
            self._order_books[market_id] = MockOrderBook(market_id=market_id)
        return self._order_books[market_id]

    async def seed_order_book_from_db(self, market_id: str) -> None:
        """
        Seed the mock order book with the latest price from the database.

        This makes mock fills realistic — they're based on actual market data
        that the ingestor has been collecting.
        """
        try:
            cursor = await self.db.execute(
                """
                SELECT yes_price, no_price, spread, liquidity
                FROM market_prices
                WHERE market_id = ?
                ORDER BY polled_at DESC
                LIMIT 1
                """,
                (market_id,),
            )
            row = await cursor.fetchone()
            if row:
                yes_price, no_price, spread, liquidity = row
                self._order_books[market_id] = MockOrderBook(
                    market_id=market_id,
                    yes_price=yes_price or 0.50,
                    no_price=no_price or 0.50,
                    spread=spread or 0.02,
                    liquidity=liquidity or 10000.0,
                )
                if self.config.verbose:
                    logger.info(
                        "[MOCK] Seeded order book for %s: yes=%.3f no=%.3f",
                        market_id,
                        yes_price,
                        no_price,
                    )
        except Exception as e:
            logger.debug(
                "[MOCK] Could not seed order book from DB for %s: %s", market_id, e
            )

    async def submit_order(
        self, leg: OrderLeg, signal_id: str | None = None, strategy: str | None = None
    ) -> MockOrderResult:
        """
        Simulate order submission with realistic latency and fill behavior.

        Args:
            leg: The order leg to submit

        Returns:
            MockOrderResult with simulated execution details
        """
        start_time = time.time()
        self.total_submitted += 1

        order_id = f"MOCK-{self.platform_label}-{uuid.uuid4().hex[:12]}"

        # Simulate submission latency
        latency_ms = random.randint(
            self.config.min_latency_ms, self.config.max_latency_ms
        )
        await asyncio.sleep(latency_ms / 1000.0)

        submission_latency_ms = int((time.time() - start_time) * 1000)

        if self.config.verbose:
            logger.info(
                "[MOCK] Order submitted: %s | market=%s side=%s size=%.2f price=%s | latency=%dms",
                order_id,
                leg.market_id,
                leg.side,
                leg.size,
                leg.limit_price,
                submission_latency_ms,
            )

        # Seed order book from DB if we haven't yet
        if leg.market_id not in self._order_books:
            await self.seed_order_book_from_db(leg.market_id)

        # Determine fill outcome
        roll = random.random()

        if roll > self.config.fill_probability:
            # ORDER REJECTED — simulates insufficient liquidity, price moved, etc.
            self.total_rejected += 1
            reason = random.choice(
                [
                    "Insufficient liquidity at requested price",
                    "Market moved beyond limit price",
                    "Order book too thin for requested size",
                    "Simulated platform rejection",
                ]
            )

            result = MockOrderResult(
                order_id=order_id,
                platform=self.platform_label,
                status="REJECTED",
                submission_latency_ms=submission_latency_ms,
                error_message=reason,
            )
            await self._write_order_to_db(leg, result)
            self.order_history.append(result)

            if self.config.verbose:
                logger.info("[MOCK] Order REJECTED: %s — %s", order_id, reason)

            return result

        # ORDER FILLS — simulate fill latency
        fill_latency_ms = random.randint(
            self.config.min_fill_latency_ms,
            self.config.max_fill_latency_ms,
        )
        await asyncio.sleep(fill_latency_ms / 1000.0)

        # Determine fill size (full or partial)
        is_partial = random.random() < self.config.partial_fill_probability
        if is_partial:
            filled_size = round(leg.size * self.config.partial_fill_fraction, 2)
            self.total_partial += 1
            status = "PARTIALLY_FILLED"
        else:
            filled_size = leg.size
            status = "FILLED"
            self.total_filled += 1

        # Simulate fill price with slippage
        book = self._get_order_book(leg.market_id)
        filled_price = book.simulate_fill_price(
            side=leg.side,
            limit_price=leg.limit_price,
            max_slippage=self.config.max_slippage,
        )

        slippage = abs(filled_price - (leg.limit_price or filled_price))
        fee_paid = round(filled_size * filled_price * self.config.fee_rate, 4)

        result = MockOrderResult(
            order_id=order_id,
            platform=self.platform_label,
            status="ACCEPTED",  # From router's perspective, it's accepted
            submission_latency_ms=submission_latency_ms,
            fill_latency_ms=fill_latency_ms,
            filled_price=filled_price,
            filled_size=filled_size,
            fee_paid=fee_paid,
            slippage=round(slippage, 4),
        )

        await self._write_order_to_db(leg, result)
        await self._write_fill_event(leg, result, status)
        self.order_history.append(result)

        if self.config.verbose:
            logger.info(
                "[MOCK] Order %s: %s | filled=%.2f @ %.4f | slip=%.4f fee=%.4f | fill_latency=%dms",
                status,
                order_id,
                filled_size,
                filled_price,
                slippage,
                fee_paid,
                fill_latency_ms,
            )

        return result

    async def cancel_order(self, order_id: str) -> bool:
        """
        Simulate order cancellation.

        Args:
            order_id: The mock order ID to cancel

        Returns:
            True (mock cancellations always succeed)
        """
        try:
            await self.db.execute(
                """
                UPDATE orders SET status = 'CANCELLED', cancelled_at = ?
                WHERE id = ?
                """,
                (int(time.time()), order_id),
            )
            await self.db.commit()

            if self.config.verbose:
                logger.info("[MOCK] Order cancelled: %s", order_id)

            return True
        except Exception as e:
            logger.error("[MOCK] Error cancelling order: %s", e)
            return True  # Mock cancels always succeed logically

    async def get_order_status(self, order_id: str):
        """
        Get mock order status from DB.

        Args:
            order_id: The order ID to check

        Returns:
            Dict with status info, or None
        """
        try:
            cursor = await self.db.execute(
                "SELECT status, filled_price, filled_size FROM orders WHERE id = ?",
                (order_id,),
            )
            row = await cursor.fetchone()
            if row:
                return {
                    "order_id": order_id,
                    "status": row[0],
                    "fill_price": row[1],
                    "fill_size": row[2],
                }
            return None
        except Exception:
            return None

    async def _write_order_to_db(
        self, leg: OrderLeg, result: MockOrderResult, strategy: str | None = None
    ) -> None:
        """Write the mock order to the orders table."""
        now = int(time.time())
        try:
            await self.db.execute(
                """
                INSERT INTO orders (
                    id, signal_id, platform, platform_order_id,
                    market_id, side, order_type,
                    requested_price, requested_size,
                    filled_price, filled_size, slippage, fee_paid,
                    status, failure_reason,
                    retry_count, submitted_at,
                    filled_at, submission_latency_ms, fill_latency_ms,
                    strategy, updated_at
                ) VALUES (
                    ?, NULL, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    0, ?,
                    ?, ?, ?,
                    ?, ?
                )
                """,
                (
                    result.order_id,
                    result.platform,
                    result.order_id,  # platform_order_id = mock order id
                    leg.market_id,
                    leg.side.lower(),
                    leg.order_type.lower(),
                    leg.limit_price,
                    leg.size,
                    result.filled_price,
                    result.filled_size,
                    result.slippage,
                    result.fee_paid,
                    "filled" if result.status == "ACCEPTED" else "failed",
                    result.error_message,
                    now,
                    now if result.filled_price else None,
                    result.submission_latency_ms,
                    result.fill_latency_ms,
                    strategy,
                    now,
                ),
            )
            await self.db.commit()
        except Exception as e:
            logger.error("[MOCK] Failed to write order to DB: %s", e)

    async def _write_fill_event(
        self, leg: OrderLeg, result: MockOrderResult, status: str
    ) -> None:
        """Write a fill event to order_events."""
        now = int(time.time())
        try:
            await self.db.execute(
                """
                INSERT INTO order_events (
                    order_id, event_type, price, size, detail, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.order_id,
                    status.lower(),  # 'filled' or 'partially_filled'
                    result.filled_price,
                    result.filled_size,
                    f"[MOCK] {status} on {result.platform}",
                    now,
                ),
            )
            await self.db.commit()
        except Exception as e:
            logger.error("[MOCK] Failed to write fill event to DB: %s", e)

    def get_stats(self) -> dict[str, int | float]:
        """Return summary stats for the mock session."""
        return {
            "total_submitted": self.total_submitted,
            "total_filled": self.total_filled,
            "total_partial": self.total_partial,
            "total_rejected": self.total_rejected,
            "fill_rate_pct": round(
                (self.total_filled + self.total_partial)
                / max(1, self.total_submitted)
                * 100,
                1,
            ),
        }

    def print_stats(self) -> None:
        """Print session stats to logger."""
        stats = self.get_stats()
        logger.info(
            "[MOCK] Session stats: %d submitted | %d filled | %d partial | %d rejected | %.1f%% fill rate",
            stats["total_submitted"],
            stats["total_filled"],
            stats["total_partial"],
            stats["total_rejected"],
            stats["fill_rate_pct"],
        )
