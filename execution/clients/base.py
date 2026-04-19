"""
Base execution client with shared DB write logic.

All execution clients (mock, kalshi, polymarket, paper) write to the same
DB tables in the same format. This base class enforces that contract.
"""

import logging
import time
from dataclasses import dataclass

import aiosqlite

from execution.clients.polymarket_book import ResolvedOrder
from execution.models import OrderLeg

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    """Unified order result across all execution modes."""

    order_id: str
    platform: str
    status: str  # "filled", "partially_filled", "failed", "pending"
    submission_latency_ms: int
    fill_latency_ms: int | None = None
    filled_price: float | None = None
    filled_size: float | None = None
    fee_paid: float | None = None
    slippage: float | None = None
    error_message: str | None = None


class BaseExecutionClient:
    """
    Base class for all execution clients.

    Provides shared DB write methods so that orders, order_events, and
    downstream analytics tables are populated identically regardless of
    whether the execution mode is mock, paper, or live.
    """

    def __init__(self, db_connection: aiosqlite.Connection, platform_label: str):
        self.db = db_connection
        self.platform_label = platform_label

    async def submit_order(
        self,
        leg: OrderLeg,
        signal_id: str | None = None,
        strategy: str | None = None,
    ) -> OrderResult:
        """Submit an order. Subclasses must implement."""
        raise NotImplementedError

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order. Subclasses must implement."""
        raise NotImplementedError

    async def get_order_status(self, order_id: str) -> dict | None:
        """Get order status. Subclasses must implement."""
        raise NotImplementedError

    async def get_balance(self) -> float | None:
        """Get account balance. Subclasses must implement."""
        raise NotImplementedError

    async def close(self) -> None:
        """Clean up resources. Override if needed."""
        pass

    # ── Shared DB writes ─────────────────────────────────────────────────

    async def write_order(
        self,
        leg: OrderLeg,
        result: OrderResult,
        signal_id: str | None = None,
        strategy: str | None = None,
        resolved: "ResolvedOrder | None" = None,
    ) -> None:
        """
        Write an order record to the orders table.

        When ``resolved`` is provided, ``side``, ``requested_price``, and
        ``book`` are pulled from it — reflecting what actually hit the
        exchange rather than the strategy's original intent. Callers that
        don't route through a resolver (Kalshi, paper-Kalshi) pass None
        and rows get book='YES' via the column default.
        """
        now = int(time.time())
        requested_price: float | None
        if resolved is not None:
            side_str = resolved.side.value
            requested_price = resolved.limit_price
            book_str = resolved.book.value
        else:
            # Normalize to uppercase for consistency with new enum-typed writers.
            side_str = (
                leg.side.value if hasattr(leg.side, "value") else str(leg.side).upper()
            )
            requested_price = leg.limit_price
            book_str = "YES"

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
                    strategy, updated_at, book
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?,
                    0, ?,
                    ?, ?, ?,
                    ?, ?, ?
                )
                """,
                (
                    result.order_id,
                    signal_id,
                    result.platform,
                    result.order_id,
                    leg.market_id,
                    side_str,
                    (
                        leg.order_type.lower()
                        if isinstance(leg.order_type, str)
                        else leg.order_type
                    ),
                    requested_price,
                    leg.size,
                    result.filled_price,
                    result.filled_size,
                    result.slippage,
                    result.fee_paid,
                    result.status,
                    result.error_message,
                    now,
                    now if result.filled_price else None,
                    result.submission_latency_ms,
                    result.fill_latency_ms,
                    strategy,
                    now,
                    book_str,
                ),
            )
            # Note: caller is responsible for committing in batches
        except Exception as e:
            logger.error("Failed to write order to DB: %s", e, exc_info=True)

    async def write_fill_event(self, result: OrderResult, detail: str = "") -> None:
        """Write a fill event to order_events."""
        if result.filled_price is None:
            return

        now = int(time.time())
        event_type = "filled" if result.status == "filled" else "partially_filled"
        try:
            await self.db.execute(
                """
                INSERT INTO order_events (
                    order_id, event_type, price, size, detail, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.order_id,
                    event_type,
                    result.filled_price,
                    result.filled_size,
                    detail or f"[{self.platform_label.upper()}] {event_type}",
                    now,
                ),
            )
            # Note: caller is responsible for committing in batches
        except Exception as e:
            logger.error("Failed to write fill event to DB: %s", e, exc_info=True)

    async def update_order_fill(self, result: OrderResult) -> None:
        """Update an existing pending order with fill data (for live polling)."""
        now = int(time.time())
        try:
            await self.db.execute(
                """
                UPDATE orders SET
                    filled_price = ?,
                    filled_size = ?,
                    slippage = ?,
                    fee_paid = ?,
                    fill_latency_ms = ?,
                    filled_at = ?,
                    status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    result.filled_price,
                    result.filled_size,
                    result.slippage,
                    result.fee_paid,
                    result.fill_latency_ms,
                    now,
                    result.status,
                    now,
                    result.order_id,
                ),
            )
            await self.db.commit()
        except Exception as e:
            logger.error("Failed to update order fill: %s", e, exc_info=True)
