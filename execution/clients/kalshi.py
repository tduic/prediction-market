"""
Kalshi REST API execution client.

Submits orders via Kalshi's REST API with RSA-PSS authentication,
handles rate limiting, and tracks latency metrics.

Auth reference: Kalshi uses RSA-PSS (SHA-256) signatures.
Message format: timestamp_ms + method + path
Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP
"""

import base64
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import aiosqlite
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from execution.models import OrderLeg

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


def _load_rsa_private_key(key_path: str) -> object:
    """
    Load RSA private key from PEM file.

    Args:
        key_path: Path to RSA private key PEM file (supports ~)

    Returns:
        RSA private key object
    """
    expanded = Path(key_path).expanduser()
    if not expanded.exists():
        raise FileNotFoundError(f"RSA key file not found: {expanded}")

    pem_data = expanded.read_bytes()
    return serialization.load_pem_private_key(pem_data, password=None)


@dataclass
class OrderStatus:
    """Status of an order on Kalshi."""

    order_id: str
    status: str  # "resting", "canceled", "executed", "pending"
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
    """Handles order submission to Kalshi via REST API with RSA-PSS auth."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        api_key: Optional[str] = None,
        rsa_key_path: Optional[str] = None,
        api_base: Optional[str] = None,
    ) -> None:
        """
        Initialize the Kalshi execution client.

        Args:
            db_connection: SQLite connection for tracking
            api_key: Kalshi API key ID (UUID)
            rsa_key_path: Path to RSA private key PEM file
            api_base: API base URL (prod or demo)
        """
        self.db_connection = db_connection
        self.api_key = api_key or os.getenv("KALSHI_API_KEY", "")
        self.api_base = api_base or os.getenv(
            "KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"
        )

        # Load RSA key
        key_path = rsa_key_path or os.getenv("KALSHI_RSA_KEY_PATH", "")
        self._private_key = None
        if key_path:
            try:
                self._private_key = _load_rsa_private_key(key_path)
                logger.info("Kalshi RSA key loaded from %s", key_path)
            except Exception as e:
                logger.error("Failed to load Kalshi RSA key: %s", e)

        self.http_client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)

        # Rate limiter: token bucket
        self._rate_limit_tokens = 10.0
        self._rate_limit_max = 10.0
        self._rate_limit_refill_per_sec = 10.0
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

    def _sign_request(self, method: str, path: str) -> dict[str, str]:
        """
        Generate RSA-PSS signature for Kalshi API request.

        Args:
            method: HTTP method (GET, POST, DELETE)
            path: Full API path (e.g., /trade-api/v2/portfolio/orders)

        Returns:
            Dictionary with Kalshi authentication headers
        """
        if not self.api_key or not self._private_key:
            raise ValueError("API key and RSA private key required for authentication")

        timestamp_ms = str(int(time.time() * 1000))

        # Message to sign: timestamp + METHOD + path
        message = (timestamp_ms + method.upper() + path).encode("utf-8")

        # RSA-PSS signature with SHA-256
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
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
        await self._acquire_rate_limit()
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
                "ticker": leg.market_id,
                "action": "buy" if leg.side.upper() == "BUY" else "sell",
                "count": int(leg.size),
                "type": leg.order_type.lower(),
                "side": "yes",  # default to yes side; override if needed
            }

            if leg.order_type.upper() == "LIMIT" and leg.limit_price is not None:
                # Kalshi uses cent prices (1-99)
                body_dict["yes_price"] = int(leg.limit_price * 100)

            body_json = json.dumps(body_dict)

            # Sign request
            path = "/trade-api/v2/portfolio/orders"
            headers = self._sign_request("POST", path)

            # Submit order
            response = await self.http_client.post(
                f"{self.api_base}/portfolio/orders",
                content=body_json,
                headers=headers,
            )

            submission_latency_ms = int((time.time() - start_time) * 1000)

            if response.status_code in (200, 201):
                order_data = response.json()
                order_obj = order_data.get("order", order_data)
                order_id = order_obj.get(
                    "order_id", f"KAL-{leg.market_id}-{int(time.time() * 1000)}"
                )

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
            logger.error("Error submitting order: %s", e, exc_info=True)
            return OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                status="REJECTED",
                submission_latency_ms=submission_latency_ms,
                error_message=str(e),
            )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order via Kalshi REST API."""
        await self._acquire_rate_limit()

        try:
            logger.info("Cancelling order: %s", order_id)

            path = f"/trade-api/v2/portfolio/orders/{order_id}"
            headers = self._sign_request("DELETE", path)

            response = await self.http_client.delete(
                f"{self.api_base}/portfolio/orders/{order_id}",
                headers=headers,
            )

            if response.status_code in (200, 204):
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
            logger.error("Error cancelling order: %s", e, exc_info=True)
            return False

    async def get_order_status(self, order_id: str) -> Optional[OrderStatus]:
        """Get the status of an order via Kalshi REST API."""
        await self._acquire_rate_limit()

        try:
            path = f"/trade-api/v2/portfolio/orders/{order_id}"
            headers = self._sign_request("GET", path)

            response = await self.http_client.get(
                f"{self.api_base}/portfolio/orders/{order_id}",
                headers=headers,
            )

            if response.status_code == 200:
                order_data = response.json()
                order_obj = order_data.get("order", order_data)

                return OrderStatus(
                    order_id=order_id,
                    status=order_obj.get("status", "unknown"),
                    filled_amount=order_obj.get("filled_count", 0),
                    fill_price=order_obj.get("average_fill_price"),
                    timestamp=time.time(),
                )
            else:
                logger.warning(
                    "Failed to fetch order status: HTTP %d", response.status_code
                )
                return None

        except Exception as e:
            logger.error("Error getting order status: %s", e, exc_info=True)
            return None

    async def get_balance(self) -> Optional[float]:
        """Get account balance in dollars."""
        await self._acquire_rate_limit()

        try:
            path = "/trade-api/v2/portfolio/balance"
            headers = self._sign_request("GET", path)

            response = await self.http_client.get(
                f"{self.api_base}/portfolio/balance",
                headers=headers,
            )

            if response.status_code == 200:
                data = response.json()
                # Balance returned in cents
                return data.get("balance", 0) / 100.0
            return None

        except Exception as e:
            logger.error("Error getting balance: %s", e, exc_info=True)
            return None

    async def close(self) -> None:
        """Close HTTP client connection."""
        await self.http_client.aclose()
