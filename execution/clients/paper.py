"""
Paper trading execution client.

Runs the full execution pipeline with real market data but does NOT
place orders on any exchange. Uses actual market prices from the DB
for fill simulation, producing analytics identical to live mode.

Enable via: EXECUTION_MODE=paper
"""

import logging
import random
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

    async def _get_db_price(
        self, market_id: str, book: Book = Book.YES
    ) -> float | None:
        """Return the most-recent price from the DB.

        For NO-book legs (translated Polymarket short-sells) reads
        ``markets.last_price_no`` directly. Falls back to ``market_prices``
        for the YES-book price in all other cases.
        """
        if book is Book.NO:
            try:
                cursor = await self.db.execute(
                    "SELECT last_price_no FROM markets WHERE id = ?",
                    (market_id,),
                )
                row = await cursor.fetchone()
                if row and row[0] is not None:
                    return float(row[0])
            except Exception as e:
                logger.debug(
                    "[PAPER] DB NO-price lookup failed for %s: %s", market_id, e
                )
        try:
            cursor = await self.db.execute(
                "SELECT yes_price FROM market_prices "
                "WHERE market_id = ? ORDER BY polled_at DESC LIMIT 1",
                (market_id,),
            )
            row = await cursor.fetchone()
            return float(row[0]) if row and row[0] is not None else None
        except Exception as e:
            logger.debug("[PAPER] DB price lookup failed for %s: %s", market_id, e)
            return None

    async def submit_order(
        self,
        leg: OrderLeg,
        signal_id: str | None = None,
        strategy: str | None = None,
    ) -> OrderResult:
        """Simulate an order fill using real market prices from the DB.

        1. Resolve through BookResolver when present (Polymarket no-naked-shorts).
        2. Fetch the current price; fall back to limit price when live lookup fails.
        3. Apply configured slippage and platform fee rate.
        4. Write order + fill event rows to the DB and return a filled OrderResult.
        """
        self.total_submitted += 1
        submission_latency = random.randint(self.min_latency_ms, self.max_latency_ms)
        order_id = f"paper_{uuid.uuid4().hex[:16]}"

        # BookResolver translates Polymarket SELL YES → BUY NO when there is
        # no inventory to deliver, enforcing the no-naked-shorts rule.
        resolved: ResolvedOrder | None = None
        if self._book_resolver is not None:
            try:
                resolved = await self._book_resolver.resolve(
                    leg.market_id, leg.side, leg.size, leg.limit_price
                )
            except Exception as e:
                logger.warning(
                    "[PAPER] BookResolver failed for %s: %s", leg.market_id, e
                )

        book = resolved.book if resolved is not None else Book.YES
        fill_price = await self._get_current_price(leg.market_id, book=book)

        if not _is_valid_price(fill_price):
            # Fall back to the limit price embedded in the order leg.
            limit = resolved.limit_price if resolved is not None else leg.limit_price
            if _is_valid_price(limit):
                fill_price = float(limit)  # type: ignore[arg-type]
            else:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency,
                    error_message=(
                        f"No market price available for {leg.market_id} (book={book.value})"
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

        assert fill_price is not None  # validated above

        # Reject LIMIT orders where the market has moved past the limit price.
        # Use the resolved (translated) limit when BookResolver is active.
        if getattr(leg, "order_type", None) and leg.order_type.upper() == "LIMIT":
            effective_side = resolved.side if resolved is not None else leg.side
            effective_limit = (
                float(resolved.limit_price)
                if resolved is not None
                else (float(leg.limit_price) if leg.limit_price is not None else None)
            )
            if effective_limit is not None:
                if effective_side is Side.BUY and fill_price > effective_limit:
                    result = OrderResult(
                        order_id=order_id,
                        platform=self.platform_label,
                        status="failed",
                        submission_latency_ms=submission_latency,
                        error_message=f"Market price {fill_price:.4f} above limit {effective_limit:.4f}",
                    )
                    await self.write_order(
                        leg,
                        result,
                        signal_id=signal_id,
                        strategy=strategy,
                        resolved=resolved,
                    )
                    return result
                elif effective_side is Side.SELL and fill_price < effective_limit:
                    result = OrderResult(
                        order_id=order_id,
                        platform=self.platform_label,
                        status="failed",
                        submission_latency_ms=submission_latency,
                        error_message=f"Market price {fill_price:.4f} below limit {effective_limit:.4f}",
                    )
                    await self.write_order(
                        leg,
                        result,
                        signal_id=signal_id,
                        strategy=strategy,
                        resolved=resolved,
                    )
                    return result

        # Apply slippage (adverse to direction): buys get a higher price,
        # sells get a lower one.
        slippage = 0.0
        if self.slippage_bps > 0:
            slippage_amt = fill_price * self.slippage_bps / 10_000
            effective_side = resolved.side if resolved is not None else leg.side
            if effective_side is Side.BUY:
                fill_price = min(0.99, fill_price + slippage_amt)
            else:
                fill_price = max(0.01, fill_price - slippage_amt)
            slippage = slippage_amt

        rate_key = "polymarket" if "polymarket" in self.platform_label else "kalshi"
        fee_rate = (
            self._fee_rate_override
            if self._fee_rate_override is not None
            else FEE_RATES.get(rate_key, 0.02)
        )
        fee_paid = round(fill_price * leg.size * fee_rate, 6)
        fill_latency = random.randint(
            self.min_fill_latency_ms, self.max_fill_latency_ms
        )

        result = OrderResult(
            order_id=order_id,
            platform=self.platform_label,
            status="filled",
            submission_latency_ms=submission_latency,
            fill_latency_ms=fill_latency,
            filled_price=fill_price,
            filled_size=leg.size,
            fee_paid=fee_paid,
            slippage=slippage,
        )

        await self.write_order(
            leg, result, signal_id=signal_id, strategy=strategy, resolved=resolved
        )
        await self.write_fill_event(result)
        self.total_filled += 1
        logger.debug(
            "[PAPER] %s: %s %s %.2f @ %.4f fee=%.6f",
            self.platform_label,
            leg.side,
            leg.market_id,
            leg.size,
            fill_price,
            fee_paid,
        )
        return result

    async def cancel_order(self, order_id: str) -> bool:
        """Paper orders are always cancellable (no real exchange state)."""
        return True

    async def get_order_status(self, order_id: str) -> dict | None:
        """Return the orders row for ``order_id`` from the DB."""
        try:
            cursor = await self.db.execute(
                "SELECT *, filled_price as fill_price FROM orders WHERE id = ?",
                (order_id,),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            logger.warning("[PAPER] get_order_status failed for %s: %s", order_id, e)
            return None

    async def get_balance(self) -> float | None:
        """Paper balance is not tracked; return None."""
        return None
