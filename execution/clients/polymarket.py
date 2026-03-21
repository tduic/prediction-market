"""
Polymarket CLOB execution client using py-clob-client.

Submits orders via Polymarket's CLOB API with two-stage authentication:
1. Derive API credentials from private key
2. Create and post signed orders

Reference: https://github.com/Polymarket/py-clob-client
"""

import logging
import os
import time
from dataclasses import dataclass


import aiosqlite

from execution.models import OrderLeg

logger = logging.getLogger(__name__)

# Lazy imports for py_clob_client to avoid import errors when in mock mode
_ClobClient = None
_ApiCreds = None
_OrderArgs = None
_OrderType = None


def _ensure_clob_imports():
    """Lazy-load py_clob_client modules."""
    global _ClobClient, _ApiCreds, _OrderArgs, _OrderType
    if _ClobClient is None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType

        _ClobClient = ClobClient
        _ApiCreds = ApiCreds
        _OrderArgs = OrderArgs
        _OrderType = OrderType


@dataclass
class OrderStatus:
    """Status of an order on Polymarket."""

    order_id: str
    status: str
    filled_amount: float
    fill_price: float | None
    timestamp: float


@dataclass
class OrderResult:
    """Result of order submission."""

    order_id: str
    status: str  # "ACCEPTED", "REJECTED", "PENDING"
    submission_latency_ms: int
    fill_latency_ms: int | None = None
    error_message: str | None = None


class PolymarketExecutionClient:
    """Handles order submission to Polymarket via CLOB API."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        private_key: str | None = None,
        funder: str | None = None,
        chain_id: int = 137,
    ) -> None:
        """
        Initialize the Polymarket execution client.

        Args:
            db_connection: SQLite connection for tracking
            private_key: Ethereum hex private key (0x...)
            funder: Proxy wallet address (optional)
            chain_id: Polygon chain ID (default: 137)
        """
        self.db_connection = db_connection
        self.private_key = private_key or os.getenv("POLYMARKET_PRIVATE_KEY", "")
        self.funder = funder or os.getenv("POLYMARKET_WALLET_ADDRESS", "")
        self.chain_id = chain_id
        self.host = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))

        self._client = None
        self._initialized = False

        # Rate limiter
        self._rate_limit_tokens = 10.0
        self._rate_limit_max = 10.0
        self._rate_limit_refill_per_sec = 5.0
        self._rate_limit_last_refill = time.monotonic()

    async def _acquire_rate_limit(self) -> None:
        """Wait until a rate limit token is available."""
        import asyncio

        while True:
            now = time.monotonic()
            elapsed = now - self._rate_limit_last_refill
            self._rate_limit_tokens = min(
                self._rate_limit_max,
                self._rate_limit_tokens + elapsed * self._rate_limit_refill_per_sec,
            )
            self._rate_limit_last_refill = now

            if self._rate_limit_tokens >= 1.0:
                self._rate_limit_tokens -= 1.0
                return

            await asyncio.sleep(0.1)

    def _ensure_client(self) -> None:
        """
        Initialize the CLOB client with two-stage auth.

        Stage 1: Create L1 client and derive API credentials
        Stage 2: Create L2 client with credentials for authenticated trading
        """
        if self._initialized:
            return

        _ensure_clob_imports()

        if not self.private_key:
            raise ValueError("Polymarket private key required")

        logger.info("Initializing Polymarket CLOB client")

        # Stage 1: derive API credentials
        l1_client = _ClobClient(
            host=self.host,
            key=self.private_key,
            chain_id=self.chain_id,
        )
        creds = l1_client.create_or_derive_api_creds()
        logger.info("Polymarket API credentials derived successfully")

        # Stage 2: authenticated client
        self._client = _ClobClient(
            host=self.host,
            key=self.private_key,
            chain_id=self.chain_id,
            creds=creds,
            signature_type=self.signature_type,
            funder=self.funder if self.funder else None,
        )

        self._initialized = True
        logger.info("Polymarket CLOB client initialized")

    async def submit_order(self, leg: OrderLeg) -> OrderResult:
        """
        Submit an order via Polymarket CLOB API.

        Flow: OrderArgs → create_order (sign) → post_order (submit)

        Args:
            leg: The order leg to submit

        Returns:
            OrderResult with order details
        """
        await self._acquire_rate_limit()
        start_time = time.time()

        try:
            self._ensure_client()

            logger.info(
                "Submitting order to Polymarket: market=%s, side=%s, size=%f, price=%s",
                leg.market_id,
                leg.side,
                leg.size,
                leg.limit_price,
            )

            # Build order args
            # token_id is the market_id for Polymarket (condition token ID)
            order_args = _OrderArgs(
                token_id=leg.market_id,
                price=leg.limit_price or 0.50,
                size=leg.size,
                side=leg.side.upper(),
            )

            # Sign order
            signed_order = self._client.create_order(order_args)

            # Determine order type
            if leg.order_type.upper() == "MARKET":
                order_type = _OrderType.FOK  # Fill-or-Kill for market orders
            else:
                order_type = _OrderType.GTC  # Good-til-Cancelled for limit

            # Submit signed order
            result = self._client.post_order(signed_order, order_type)

            submission_latency_ms = int((time.time() - start_time) * 1000)

            # Extract order ID from response
            if isinstance(result, dict):
                order_id = result.get("orderID", result.get("id", ""))
                status = result.get("status", "ACCEPTED")
                if result.get("success", True) and order_id:
                    order_status = "ACCEPTED"
                else:
                    order_status = "REJECTED"
            else:
                order_id = str(result) if result else ""
                order_status = "ACCEPTED" if order_id else "REJECTED"

            if not order_id:
                order_id = f"POLY-{leg.market_id}-{int(time.time() * 1000)}"

            # Log order to database
            await self.db_connection.execute(
                """
                INSERT INTO orders
                (order_id, platform, market_id, side, size, limit_price, status,
                 submission_latency_ms, created_at_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    order_id,
                    "polymarket",
                    leg.market_id,
                    leg.side,
                    leg.size,
                    leg.limit_price,
                    "PENDING" if order_status == "ACCEPTED" else "REJECTED",
                    submission_latency_ms,
                ),
            )
            await self.db_connection.commit()

            logger.info(
                "Order submitted to Polymarket: %s (latency: %dms, status: %s)",
                order_id,
                submission_latency_ms,
                order_status,
            )

            return OrderResult(
                order_id=order_id,
                status=order_status,
                submission_latency_ms=submission_latency_ms,
            )

        except Exception as e:
            submission_latency_ms = int((time.time() - start_time) * 1000)
            logger.error("Error submitting order to Polymarket: %s", e, exc_info=True)
            return OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                status="REJECTED",
                submission_latency_ms=submission_latency_ms,
                error_message=str(e),
            )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order on Polymarket."""
        await self._acquire_rate_limit()

        try:
            self._ensure_client()

            logger.info("Cancelling Polymarket order: %s", order_id)
            result = self._client.cancel(order_id)

            await self.db_connection.execute(
                "UPDATE orders SET status = ? WHERE order_id = ?",
                ("CANCELLED", order_id),
            )
            await self.db_connection.commit()

            logger.info("Order cancelled: %s", order_id)
            return True

        except Exception as e:
            logger.error("Error cancelling order: %s", e, exc_info=True)
            return False

    async def get_order_status(self, order_id: str) -> OrderStatus | None:
        """Get the status of an order on Polymarket."""
        await self._acquire_rate_limit()

        try:
            self._ensure_client()

            result = self._client.get_order(order_id)

            if result:
                return OrderStatus(
                    order_id=order_id,
                    status=result.get("status", "unknown"),
                    filled_amount=float(result.get("size_matched", 0)),
                    fill_price=(
                        float(result.get("price", 0)) if result.get("price") else None
                    ),
                    timestamp=time.time(),
                )
            return None

        except Exception as e:
            logger.error("Error getting order status: %s", e, exc_info=True)
            return None

    async def close(self) -> None:
        """Clean up client resources."""
        self._client = None
        self._initialized = False
