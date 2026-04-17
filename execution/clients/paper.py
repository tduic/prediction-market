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
from execution.models import OrderLeg

logger = logging.getLogger(__name__)

# Use realistic fee rates matching each platform
FEE_RATES = {
    "polymarket": 0.02,
    "kalshi": 0.07,
}


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

        # Stats
        self.total_submitted = 0
        self.total_filled = 0

    async def _get_current_price(self, market_id: str) -> float | None:
        """Fetch the live price from the exchange API, falling back to DB.

        Tries to call the exchange's price endpoint directly so paper/shadow
        fills use the actual market price at order time, not stale polled data.
        Falls back to the most recent DB price if the live fetch fails.
        """
        try:
            cursor = await self.db.execute(
                "SELECT platform, platform_id FROM markets WHERE id = ?",
                (market_id,),
            )
            row = await cursor.fetchone()
            if row:
                platform, platform_id = row[0], row[1]
                live_price = await self._fetch_live_price(platform, platform_id)
                if live_price is not None:
                    return live_price
        except Exception as e:
            logger.debug("[PAPER] Live price lookup failed for %s: %s", market_id, e)

        return await self._get_db_price(market_id)

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

    async def _get_db_price(self, market_id: str) -> float | None:
        """Read the most recently polled price from the DB (fallback)."""
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

        # Get real market price
        market_price = await self._get_current_price(leg.market_id)

        if market_price is None:
            # No price data — use limit price or reject
            if leg.limit_price:
                market_price = leg.limit_price
            else:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message="No market price available",
                )
                await self.write_order(
                    leg, result, signal_id=signal_id, strategy=strategy
                )
                return result

        # For limit orders, check if price is executable
        if leg.order_type.upper() == "LIMIT" and leg.limit_price is not None:
            if leg.side.upper() == "BUY" and market_price > leg.limit_price:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message=f"Market price {market_price:.4f} above limit {leg.limit_price:.4f}",
                )
                await self.write_order(
                    leg, result, signal_id=signal_id, strategy=strategy
                )
                return result
            if leg.side.upper() == "SELL" and market_price < leg.limit_price:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message=f"Market price {market_price:.4f} below limit {leg.limit_price:.4f}",
                )
                await self.write_order(
                    leg, result, signal_id=signal_id, strategy=strategy
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
            if leg.side.upper() == "BUY":
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
            leg.limit_price if leg.limit_price is not None else market_price
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

        await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
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
