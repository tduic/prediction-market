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
            except Exception as e:
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
        except Exception as e:
            logger.debug(
                "[PAPER] %s price API error for %s: %s", platform, platform_id, e
            )
        return None

    async def _get_db_price(
        self, market_id: str, book: Book = Book.YES
    ) -> float | None:
        """Read the most recently polled price from the DB (fallback)."""
        if book is Book.NO:
            # NO-book reference for translated SELL legs.
            cursor = await self.db.execute(
                "SELECT last_price_no FROM markets WHERE id = ?",
                (market_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row and row[0] is not None else None
        try:
            cursor = await self.db.execute(
                """
                SELECT yes_price FROM market_prices
                WHERE market_id = ?
                ORDER BY polled_at DESC
                LIMIT 1
                """,
                (market_id,),
            )
            row = await cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None

    async def submit_order(
        self,
        leg: OrderLeg,
        signal_id: str | None = None,
        strategy: str | None = None,
    ) -> OrderResult:
        """
        Simulate order submission using real market prices.

        Does NOT call any exchange API. Fills at the current market
        price from the database, with realistic latency and fee calculation.
        """
        self.total_submitted += 1
        order_id = f"PAPER-{self.platform_label}-{uuid.uuid4().hex[:12]}"

        # Simulate submission latency (no actual sleep — just record the number)
        latency_ms = random.randint(self.min_latency_ms, self.max_latency_ms)
        submission_latency_ms = latency_ms

        logger.info(
            "[PAPER] Order submitted: %s | market=%s side=%s size=%.2f price=%s",
            order_id,
            leg.market_id,
            leg.side,
            leg.size,
            leg.limit_price,
        )

        # Route Polymarket legs through BookResolver (no-naked-shorts rule).
        resolved: ResolvedOrder | None = None
        if self._book_resolver is not None:
            resolved = await self._book_resolver.resolve(
                leg.market_id, leg.side, leg.size, leg.limit_price
            )
            if resolved is None:
                submission_latency_ms = latency_ms
                error_msg = (
                    f"BookResolver rejected order for {leg.market_id} "
                    f"(side={leg.side.value}, size={leg.size}, "
                    f"price={leg.limit_price})"
                )
                logger.error(error_msg)
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message=error_msg,
                )
                await self.write_order(
                    leg, result, signal_id=signal_id, strategy=strategy
                )
                return result

        # Effective values after translation (Kalshi legs: resolved is None,
        # so effective_* fall back to original leg values with Book.YES).
        effective_side = resolved.side if resolved else leg.side
        effective_limit = resolved.limit_price if resolved else leg.limit_price
        effective_book = resolved.book if resolved else Book.YES

        # Get real market price, routed through the correct book.
        market_price = await self._get_current_price(leg.market_id, effective_book)

        if market_price is None:
            # No price data — use effective limit price or reject
            if effective_limit:
                market_price = effective_limit
            else:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message="No market price available",
                )
                await self.write_order(
                    leg,
                    result,
                    signal_id=signal_id,
                    strategy=strategy,
                    resolved=resolved,
                )
                return result

        # Marketable-at-limit check uses effective values.
        if leg.order_type.upper() == "LIMIT" and effective_limit is not None:
            if effective_side is Side.BUY and market_price > effective_limit:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message=(
                        f"Market price {market_price:.4f} above limit "
                        f"{effective_limit:.4f}"
                    ),
                )
                await self.write_order(
                    leg,
                    result,
                    signal_id=signal_id,
                    strategy=strategy,
                    resolved=resolved,
                )
                return result
            if effective_side is Side.SELL and market_price < effective_limit:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message=(
                        f"Market price {market_price:.4f} below limit "
                        f"{effective_limit:.4f}"
                    ),
                )
                await self.write_order(
                    leg,
                    result,
                    signal_id=signal_id,
                    strategy=strategy,
                    resolved=resolved,
                )
                return result

        # Simulate fill latency (no actual sleep — just record the number)
        fill_latency_ms = random.randint(
            self.min_fill_latency_ms, self.max_fill_latency_ms
        )

        # Apply slippage: adverse price movement (BUY pays more, SELL receives less)
        slippage_factor = self.slippage_bps / 10000.0
        if slippage_factor > 0:
            adverse = market_price * slippage_factor * random.uniform(0, 1)
            if effective_side is Side.BUY:
                filled_price = min(round(market_price + adverse, 6), 0.99)
            else:
                filled_price = max(round(market_price - adverse, 6), 0.01)
        else:
            filled_price = market_price
        filled_size = leg.size
        # For LIMIT orders, slippage is fill vs. requested limit.
        # For MARKET orders (limit_price=None), compare against the observed
        # market price at fill time so the simulated adverse movement is
        # actually reported instead of collapsing to 0.
        reference_price = (
            effective_limit if effective_limit is not None else market_price
        )
        slippage = abs(filled_price - reference_price)

        # Use override fee rate if provided, else platform default
        if self._fee_rate_override is not None:
            fee_rate = self._fee_rate_override
        else:
            base_platform = self.platform_label.replace("paper_", "")
            fee_rate = FEE_RATES.get(base_platform, 0.02)
        fee_paid = round(filled_size * filled_price * fee_rate, 4)

        self.total_filled += 1

        result = OrderResult(
            order_id=order_id,
            platform=self.platform_label,
            status="filled",
            submission_latency_ms=submission_latency_ms,
            fill_latency_ms=fill_latency_ms,
            filled_price=round(filled_price, 4),
            filled_size=filled_size,
            fee_paid=fee_paid,
            slippage=round(slippage, 4),
        )

        await self.write_order(
            leg, result, signal_id=signal_id, strategy=strategy, resolved=resolved
        )
        await self.write_fill_event(result)

        logger.info(
            "[PAPER] Order FILLED: %s | price=%.4f size=%.2f fees=%.4f | fill_latency=%dms",
            order_id,
            filled_price,
            filled_size,
            fee_paid,
            fill_latency_ms,
        )

        return result

    async def cancel_order(self, order_id: str) -> bool:
        """Paper cancel always succeeds."""
        try:
            await self.db.execute(
                "UPDATE orders SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                (int(time.time()), order_id),
            )
            await self.db.commit()
            return True
        except Exception:
            return True

    async def get_order_status(self, order_id: str) -> dict | None:
        """Get paper order status from DB."""
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

    async def get_balance(self) -> float | None:
        """Get paper balance from trade history."""
        try:
            cursor = await self.db.execute(
                """
                SELECT COALESCE(SUM(CASE
                    WHEN side = 'buy' THEN -filled_size * filled_price
                    ELSE filled_size * filled_price
                END), 0) + COALESCE(SUM(-fee_paid), 0)
                FROM orders
                WHERE platform = ? AND status = 'filled'
                """,
                (self.platform_label,),
            )
            row = await cursor.fetchone()
            return row[0] if row else None
        except Exception:
            return None
