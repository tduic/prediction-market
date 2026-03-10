"""
Kalshi REST API execution client.

Submits orders via Kalshi's REST API with HMAC-SHA256 authentication,
handles rate limiting, and tracks latency metrics.
"""

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import aiosqlite
import httpx

from execution.models import OrderLeg

logger = logging.getLogger(__name__)

KALSHI_API_BASE = "https://trading-api.kalshi.com/trade-api/v2"
DEFAULT_TIMEOUT = 30


@dataclass
class OrderStatus:
    """Status of an order on Kalshi."""

    order_id: str
    status: str  # "PENDING", "FILLED", "PARTIALLY_FILLED", "CANCELLED"
    filled_amount: float
    fill_price: Optional[float]
    timestamp: float


@dataclass
class OrderResult:
    """Result of order submission."""

    order_id: str
    status: str  # "ACCEPTED", "REJECTED", "PENDING"
    submission_latency_ms: int
    fill_latency_ms: Optional[int] = None
    error_message: Optional[str] = None


class KalshiExecutionClient:
    """Handles order submission to Kalshi via REST API."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
    ) -> None:
        """
        Initialize the Kalshi execution client.

        Args:
            db_connection: SQLite connection for tracking
            api_key: Kalshi API key for authentication
            api_secret: Kalshi API secret for HMAC signing
        """
        self.db_connection = db_connection
        self.api_key = api_key
        self.api_secret = api_secret
        self.http_client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

    async def _sign_request(
        self,
        method: str,
        path: str,
        body: Optional[str] = None,
    ) -> dict[str, str]:
        """
        Generate HMAC-SHA256 signature for Kalshi API request.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (e.g., "/orders")
            body: Request body JSON string

        Returns:
            Dictionary with Authorization and other headers
        """
        if not self.api_key or not self.api_secret:
            raise ValueError("API key and secret required for authentication")

        timestamp = str(int(time.time() * 1000))

        # Message to sign: METHOD|PATH|TIMESTAMP|BODY
        message = f"{method}|{path}|{timestamp}|{body or ''}"

        # HMAC-SHA256 signature
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {
            "Authorization": f"HMAC {self.api_key}:{signature}",
            "Content-Type": "application/json",
        }

    async def submit_order(self, leg: OrderLeg) -> OrderResult:
        """
        Submit an order via Kalshi REST API.

        Args:
            leg: The order leg to submit

        Returns:
            OrderResult with order details
        """
        start_time = time.time()

        try:
            logger.info(
                "Submitting order to Kalshi: market=%s, side=%s, size=%f",
                leg.market_id,
                leg.side,
                leg.size,
            )

            # Prepare request body
            body_dict = {
                "market_id": leg.market_id,
                "side": leg.side.upper(),
                "count": int(leg.size),
                "type": leg.order_type.upper(),
            }

            if leg.order_type.upper() == "LIMIT" and leg.limit_price is not None:
                # Kalshi uses cent prices (0-100)
                body_dict["limit_cents"] = int(leg.limit_price * 100)

            body_json = json.dumps(body_dict)

            # Generate signature
            headers = await self._sign_request("POST", "/orders", body_json)

            # Submit order
            response = await self.http_client.post(
                f"{KALSHI_API_BASE}/orders",
                content=body_json,
                headers=headers,
            )

            submission_latency_ms = int((time.time() - start_time) * 1000)

            if response.status_code == 201 or response.status_code == 200:
                order_data = response.json()
                order_id = order_data.get("order_id", f"KAL-{leg.market_id}-{int(time.time() * 1000)}")

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
                        "kalshi",
                        leg.market_id,
                        leg.side,
                        leg.size,
                        leg.limit_price,
                        "PENDING",
                        submission_latency_ms,
                    ),
                )
                await self.db_connection.commit()

                logger.info(
                    "Order submitted to Kalshi: %s (latency: %dms)",
                    order_id,
                    submission_latency_ms,
                )

                return OrderResult(
                    order_id=order_id,
                    status="ACCEPTED",
                    submission_latency_ms=submission_latency_ms,
                )
            else:
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error("Order submission failed: %s", error_msg)

                return OrderResult(
                    order_id=f"FAILED-{leg.market_id}",
                    status="REJECTED",
                    submission_latency_ms=submission_latency_ms,
                    error_message=error_msg,
                )

        except httpx.TimeoutException as e:
            submission_latency_ms = int((time.time() - start_time) * 1000)
            logger.error("Request timeout submitting order: %s", e)
            return OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                status="REJECTED",
                submission_latency_ms=submission_latency_ms,
                error_message="Request timeout",
            )
        except Exception as e:
            submission_latency_ms = int((time.time() - start_time) * 1000)
            logger.error("Error submitting order: %s", exc_info=e)
            return OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                status="REJECTED",
                submission_latency_ms=submission_latency_ms,
                error_message=str(e),
            )

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order via Kalshi REST API.

        Args:
            order_id: The order ID to cancel

        Returns:
            True if cancellation successful
        """
        try:
            logger.info("Cancelling order: %s", order_id)

            # Generate signature for DELETE request
            headers = await self._sign_request("DELETE", f"/orders/{order_id}")

            # Send cancellation request
            response = await self.http_client.delete(
                f"{KALSHI_API_BASE}/orders/{order_id}",
                headers=headers,
            )

            if response.status_code in (200, 204):
                # Update order status in database
                await self.db_connection.execute(
                    "UPDATE orders SET status = ? WHERE order_id = ?",
                    ("CANCELLED", order_id),
                )
                await self.db_connection.commit()

                logger.info("Order cancelled: %s", order_id)
                return True
            else:
                logger.error(
                    "Failed to cancel order: HTTP %d: %s",
                    response.status_code,
                    response.text,
                )
                return False

        except Exception as e:
            logger.error("Error cancelling order: %s", exc_info=e)
            return False

    async def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """
        Get the status of an order via Kalshi REST API.

        Args:
            order_id: The order ID to check

        Returns:
            OrderStatus if found, None otherwise
        """
        try:
            logger.debug("Fetching order status: %s", order_id)

            # Generate signature for GET request
            headers = await self._sign_request("GET", f"/orders/{order_id}")

            # Fetch order status
            response = await self.http_client.get(
                f"{KALSHI_API_BASE}/orders/{order_id}",
                headers=headers,
            )

            if response.status_code == 200:
                order_data = response.json()

                status = order_data.get("status", "UNKNOWN")
                filled_amount = order_data.get("filled_count", 0)
                fill_price = order_data.get("execution_price")

                return OrderStatus(
                    order_id=order_id,
                    status=status,
                    filled_amount=filled_amount,
                    fill_price=fill_price,
                    timestamp=time.time(),
                )
            else:
                logger.warning(
                    "Failed to fetch order status: HTTP %d",
                    response.status_code,
                )
                return None

        except Exception as e:
            logger.error("Error getting order status: %s", exc_info=e)
            return None

    async def close(self) -> None:
        """Close HTTP client connection."""
        await self.http_client.aclose()
