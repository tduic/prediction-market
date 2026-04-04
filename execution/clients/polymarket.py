"""
Polymarket CLOB execution client using py-clob-client.

Submits orders via Polymarket's CLOB API, polls for fill status,
and writes complete order data to DB via BaseExecutionClient.
"""

import asyncio
import logging
import os
import time

import aiosqlite

from core.secrets import get_secret
from execution.clients.base import BaseExecutionClient, OrderResult
from execution.models import OrderLeg

logger = logging.getLogger(__name__)

# Lazy imports for py_clob_client
_ClobClient = None
_ApiCreds = None
_OrderArgs = None
_OrderType = None

# Polymarket is fee-free for makers, takers pay ~1-2%
POLYMARKET_FEE_RATE = 0.02


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


class PolymarketExecutionClient(BaseExecutionClient):
    """Handles order submission to Polymarket via CLOB API."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        private_key: str | None = None,
        funder: str | None = None,
        chain_id: int = 137,
        proxy_url: str | None = None,
    ) -> None:
        super().__init__(db_connection, platform_label="polymarket")

        self.private_key = private_key or get_secret("POLYMARKET_PRIVATE_KEY", "") or ""
        self.funder = funder or get_secret("POLYMARKET_WALLET_ADDRESS", "") or ""
        self.chain_id = chain_id
        self.host = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))

        # SOCKS5 proxy URL for EU routing (e.g. "socks5://1.2.3.4:1080")
        self.proxy_url = proxy_url or os.getenv("POLYMARKET_PROXY", "")

        self._client = None
        self._initialized = False

        # Rate limiter
        self._rate_limit_tokens = 10.0
        self._rate_limit_max = 10.0
        self._rate_limit_refill_per_sec = 5.0
        self._rate_limit_last_refill = time.monotonic()

    async def _acquire_rate_limit(self) -> None:
        """Wait until a rate limit token is available."""
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
        """Initialize the CLOB client with two-stage auth."""
        if self._initialized:
            return

        _ensure_clob_imports()

        if not self.private_key:
            raise ValueError("Polymarket private key required")

        logger.info("Initializing Polymarket CLOB client")

        l1_client = _ClobClient(
            host=self.host,
            key=self.private_key,
            chain_id=self.chain_id,
        )
        creds = l1_client.create_or_derive_api_creds()

        self._client = _ClobClient(
            host=self.host,
            key=self.private_key,
            chain_id=self.chain_id,
            creds=creds,
            signature_type=self.signature_type,
            funder=self.funder if self.funder else None,
        )

        # Patch py-clob-client's HTTP layer to use SOCKS5 proxy
        if self.proxy_url:
            self._install_proxy()

        self._initialized = True
        logger.info("Polymarket CLOB client initialized")

    def _install_proxy(self) -> None:
        """
        Replace py-clob-client's global httpx client with a proxy-aware one.

        py-clob-client uses a module-level `_http_client = httpx.Client(http2=True)`
        in `py_clob_client.http_helpers.helpers`. We swap it for an httpx client
        configured with a SOCKS5 transport so all CLOB API calls route through
        the EU proxy.
        """
        try:
            import httpx
            from httpx_socks import SyncProxyTransport
            from py_clob_client.http_helpers import helpers as clob_helpers

            transport = SyncProxyTransport.from_url(self.proxy_url)
            proxied_client = httpx.Client(transport=transport, http2=False)
            clob_helpers._http_client = proxied_client

            logger.info(
                "Polymarket HTTP traffic routed through proxy: %s",
                self.proxy_url.split("@")[-1],  # log host:port only, not creds
            )
        except ImportError:
            logger.error(
                "httpx-socks not installed — cannot use POLYMARKET_PROXY. "
                "Install with: pip install httpx-socks"
            )
            raise
        except Exception as e:
            logger.error("Failed to configure proxy: %s", e, exc_info=True)
            raise

    async def submit_order(self, leg: OrderLeg) -> OrderResult:
        """Submit order to Polymarket, poll for fill. Returns unified OrderResult."""
        await self._acquire_rate_limit()
        start_time = time.time()

        try:
            self._ensure_client()

            logger.info(
                "Submitting to Polymarket: market=%s side=%s size=%f price=%s",
                leg.market_id,
                leg.side,
                leg.size,
                leg.limit_price,
            )

            order_args = _OrderArgs(
                token_id=leg.market_id,
                price=leg.limit_price or 0.50,
                size=leg.size,
                side=leg.side.upper(),
            )

            signed_order = self._client.create_order(order_args)

            if leg.order_type.upper() == "MARKET":
                order_type = _OrderType.FOK
            else:
                order_type = _OrderType.GTC

            api_result = self._client.post_order(signed_order, order_type)

            submission_latency_ms = int((time.time() - start_time) * 1000)

            # Extract order ID
            if isinstance(api_result, dict):
                order_id = api_result.get("orderID", api_result.get("id", ""))
                if not api_result.get("success", True) or not order_id:
                    error_msg = api_result.get("errorMsg", "Order rejected")
                    result = OrderResult(
                        order_id=order_id or f"FAILED-{leg.market_id}",
                        platform="polymarket",
                        status="failed",
                        submission_latency_ms=submission_latency_ms,
                        error_message=error_msg,
                    )
                    await self.write_order(leg, result)
                    return result
            else:
                order_id = str(api_result) if api_result else ""

            if not order_id:
                order_id = f"POLY-{int(time.time() * 1000)}"

            # Write initial pending order
            pending_result = OrderResult(
                order_id=order_id,
                platform="polymarket",
                status="pending",
                submission_latency_ms=submission_latency_ms,
            )
            await self.write_order(leg, pending_result)

            # Poll for fill
            fill_result = await self._poll_for_fill(
                order_id, leg, start_time, submission_latency_ms
            )
            return fill_result

        except Exception as e:
            submission_latency_ms = int((time.time() - start_time) * 1000)
            logger.error("Error submitting to Polymarket: %s", e, exc_info=True)
            result = OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                platform="polymarket",
                status="failed",
                submission_latency_ms=submission_latency_ms,
                error_message=str(e),
            )
            await self.write_order(leg, result)
            return result

    async def _poll_for_fill(
        self,
        order_id: str,
        leg: OrderLeg,
        order_start_time: float,
        submission_latency_ms: int,
        max_polls: int = 30,
        poll_interval_s: float = 1.0,
    ) -> OrderResult:
        """Poll Polymarket API until order fills, cancels, or times out."""
        for _ in range(max_polls):
            await asyncio.sleep(poll_interval_s)
            await self._acquire_rate_limit()

            try:
                self._ensure_client()
                order_data = self._client.get_order(order_id)

                if not order_data:
                    continue

                status = order_data.get("status", "").lower()
                size_matched = float(order_data.get("size_matched", 0))

                if status == "matched" or (status == "trading" and size_matched > 0):
                    fill_latency_ms = int((time.time() - order_start_time) * 1000)

                    filled_price = float(
                        order_data.get("price", leg.limit_price or 0.50)
                    )
                    filled_size = size_matched if size_matched > 0 else leg.size

                    slippage = abs(filled_price - (leg.limit_price or filled_price))
                    fee_paid = round(
                        filled_size * filled_price * POLYMARKET_FEE_RATE, 4
                    )

                    is_partial = filled_size < leg.size * 0.99
                    result = OrderResult(
                        order_id=order_id,
                        platform="polymarket",
                        status="partially_filled" if is_partial else "filled",
                        submission_latency_ms=submission_latency_ms,
                        fill_latency_ms=fill_latency_ms,
                        filled_price=round(filled_price, 4),
                        filled_size=round(filled_size, 2),
                        fee_paid=fee_paid,
                        slippage=round(slippage, 4),
                    )
                    await self.update_order_fill(result)
                    await self.write_fill_event(result)
                    return result

                if status in ("canceled", "cancelled"):
                    result = OrderResult(
                        order_id=order_id,
                        platform="polymarket",
                        status="failed",
                        submission_latency_ms=submission_latency_ms,
                        error_message="Order cancelled",
                    )
                    await self.update_order_fill(result)
                    return result

            except Exception as e:
                logger.warning("Poll error for order %s: %s", order_id, e)

        logger.warning("Fill poll timeout for order %s", order_id)
        return OrderResult(
            order_id=order_id,
            platform="polymarket",
            status="pending",
            submission_latency_ms=submission_latency_ms,
            error_message="Fill poll timeout",
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order on Polymarket."""
        await self._acquire_rate_limit()
        try:
            self._ensure_client()
            self._client.cancel(order_id)
            await self.db.execute(
                "UPDATE orders SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                (int(time.time()), order_id),
            )
            await self.db.commit()
            return True
        except Exception as e:
            logger.error("Error cancelling order: %s", e, exc_info=True)
            return False

    async def get_order_status(self, order_id: str) -> dict | None:
        """Get order status from Polymarket."""
        await self._acquire_rate_limit()
        try:
            self._ensure_client()
            return self._client.get_order(order_id)
        except Exception as e:
            logger.error("Error getting order status: %s", e, exc_info=True)
            return None

    async def get_balance(self) -> float | None:
        """Get USDC balance on Polymarket."""
        await self._acquire_rate_limit()
        try:
            self._ensure_client()
            # py_clob_client doesn't expose balance directly;
            # compute from local DB trade history as fallback
            cursor = await self.db.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN side = 'buy' THEN -filled_size * filled_price ELSE filled_size * filled_price END), 0)
                    + COALESCE(SUM(-fee_paid), 0)
                FROM orders
                WHERE platform LIKE '%polymarket%' AND status = 'filled'
                """)
            row = await cursor.fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.error("Error getting Polymarket balance: %s", e, exc_info=True)
            return None

    async def close(self) -> None:
        """Clean up."""
        self._client = None
        self._initialized = False
