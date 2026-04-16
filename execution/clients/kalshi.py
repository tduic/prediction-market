"""
Kalshi REST API execution client.

Submits orders via Kalshi's REST API with RSA-PSS authentication,
polls for fill status, and writes complete order data to DB via BaseExecutionClient.
"""

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path

import aiosqlite
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from core.secrets import get_secret
from execution.clients.base import BaseExecutionClient, OrderResult
from execution.models import OrderLeg

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30
KALSHI_FEE_RATE = 0.07  # Kalshi charges ~7% of profit on winning contracts


def _load_rsa_private_key(key_path: str) -> RSAPrivateKey:
    """Load RSA private key from PEM file."""
    expanded = Path(key_path).expanduser()
    if not expanded.exists():
        raise FileNotFoundError(f"RSA key file not found: {expanded}")
    key = serialization.load_pem_private_key(expanded.read_bytes(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError(
            f"Kalshi key at {expanded} is not an RSA private key "
            f"(got {type(key).__name__})"
        )
    return key


class KalshiExecutionClient(BaseExecutionClient):
    """Handles order submission to Kalshi via REST API with RSA-PSS auth."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        api_key: str | None = None,
        rsa_key_path: str | None = None,
        api_base: str | None = None,
    ) -> None:
        super().__init__(db_connection, platform_label="kalshi")

        self.api_key: str = api_key or get_secret("KALSHI_API_KEY", "") or ""
        self.api_base: str = (
            api_base
            or os.getenv("KALSHI_API_BASE")
            or "https://api.elections.kalshi.com/trade-api/v2"
        )

        # Load RSA key. Key path is a filesystem pointer, not a secret, so
        # it stays in env vars. The key material itself lives on disk with
        # mode 0600 and is optionally mounted from a Secret Manager volume.
        key_path = rsa_key_path or get_secret("KALSHI_RSA_KEY_PATH", "") or ""
        self._private_key: RSAPrivateKey | None = None
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
        """Generate RSA-PSS signature headers for Kalshi API."""
        if not self.api_key or not self._private_key:
            raise ValueError("API key and RSA private key required")

        timestamp_ms = str(int(time.time() * 1000))
        message = (timestamp_ms + method.upper() + path).encode("utf-8")

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

    async def submit_order(
        self,
        leg: OrderLeg,
        signal_id: str | None = None,
        strategy: str | None = None,
    ) -> OrderResult:
        """Submit order to Kalshi, then poll for fill. Returns unified OrderResult."""
        await self._acquire_rate_limit()
        start_time = time.time()

        try:
            logger.info(
                "Submitting to Kalshi: market=%s side=%s size=%f price=%s",
                leg.market_id,
                leg.side,
                leg.size,
                leg.limit_price,
            )

            body_dict = {
                "ticker": leg.market_id,
                "action": "buy" if leg.side.upper() == "BUY" else "sell",
                "count": int(leg.size),
                "type": leg.order_type.lower(),
                "side": "yes",
            }

            if leg.order_type.upper() == "LIMIT" and leg.limit_price is not None:
                body_dict["yes_price"] = int(leg.limit_price * 100)

            body_json = json.dumps(body_dict)
            path = "/trade-api/v2/portfolio/orders"
            headers = self._sign_request("POST", path)

            response = await self.http_client.post(
                f"{self.api_base}/portfolio/orders",
                content=body_json,
                headers=headers,
            )

            submission_latency_ms = int((time.time() - start_time) * 1000)

            if response.status_code not in (200, 201):
                error_msg = f"HTTP {response.status_code}: {response.text}"
                logger.error("Order submission failed: %s", error_msg)
                result = OrderResult(
                    order_id=f"FAILED-{leg.market_id}",
                    platform="kalshi",
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message=error_msg,
                )
                await self.write_order(
                    leg, result, signal_id=signal_id, strategy=strategy
                )
                return result

            order_data = response.json()
            order_obj = order_data.get("order", order_data)
            order_id = order_obj.get("order_id", f"KAL-{int(time.time() * 1000)}")

            # Write initial pending order
            pending_result = OrderResult(
                order_id=order_id,
                platform="kalshi",
                status="pending",
                submission_latency_ms=submission_latency_ms,
            )
            await self.write_order(
                leg, pending_result, signal_id=signal_id, strategy=strategy
            )

            # Poll for fill
            fill_result = await self._poll_for_fill(
                order_id, leg, start_time, submission_latency_ms
            )
            return fill_result

        except httpx.TimeoutException:
            submission_latency_ms = int((time.time() - start_time) * 1000)
            result = OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                platform="kalshi",
                status="failed",
                submission_latency_ms=submission_latency_ms,
                error_message="Request timeout",
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result
        except Exception as e:
            submission_latency_ms = int((time.time() - start_time) * 1000)
            logger.error("Error submitting order: %s", e, exc_info=True)
            result = OrderResult(
                order_id=f"FAILED-{leg.market_id}",
                platform="kalshi",
                status="failed",
                submission_latency_ms=submission_latency_ms,
                error_message=str(e),
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
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
        """Poll Kalshi API until order fills, cancels, or times out."""
        for _ in range(max_polls):
            await asyncio.sleep(poll_interval_s)
            await self._acquire_rate_limit()

            try:
                path = f"/trade-api/v2/portfolio/orders/{order_id}"
                headers = self._sign_request("GET", path)
                response = await self.http_client.get(
                    f"{self.api_base}/portfolio/orders/{order_id}",
                    headers=headers,
                )

                if response.status_code != 200:
                    continue

                order_data = response.json()
                order_obj = order_data.get("order", order_data)
                status = order_obj.get("status", "").lower()

                if status in ("executed", "filled"):
                    fill_latency_ms = int((time.time() - order_start_time) * 1000)
                    filled_count = order_obj.get("filled_count", int(leg.size))
                    avg_price = order_obj.get("average_fill_price")
                    if avg_price is not None:
                        filled_price = avg_price / 100.0  # cents to dollars
                    else:
                        filled_price = leg.limit_price or 0.50

                    slippage = abs(filled_price - (leg.limit_price or filled_price))
                    fee_paid = round(filled_count * filled_price * KALSHI_FEE_RATE, 4)

                    result = OrderResult(
                        order_id=order_id,
                        platform="kalshi",
                        status="filled",
                        submission_latency_ms=submission_latency_ms,
                        fill_latency_ms=fill_latency_ms,
                        filled_price=round(filled_price, 4),
                        filled_size=float(filled_count),
                        fee_paid=fee_paid,
                        slippage=round(slippage, 4),
                    )
                    await self.update_order_fill(result)
                    await self.write_fill_event(result)
                    return result

                if status in ("canceled", "cancelled"):
                    result = OrderResult(
                        order_id=order_id,
                        platform="kalshi",
                        status="failed",
                        submission_latency_ms=submission_latency_ms,
                        error_message="Order cancelled by exchange",
                    )
                    await self.update_order_fill(result)
                    return result

                # Still resting/pending — keep polling

            except Exception as e:
                logger.warning("Poll error for order %s: %s", order_id, e)

        # Timed out polling — order may still be resting
        logger.warning("Fill poll timeout for order %s", order_id)
        return OrderResult(
            order_id=order_id,
            platform="kalshi",
            status="pending",
            submission_latency_ms=submission_latency_ms,
            error_message="Fill poll timeout — order may still be resting",
        )

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an order via Kalshi REST API."""
        await self._acquire_rate_limit()
        try:
            path = f"/trade-api/v2/portfolio/orders/{order_id}"
            headers = self._sign_request("DELETE", path)
            response = await self.http_client.delete(
                f"{self.api_base}/portfolio/orders/{order_id}",
                headers=headers,
            )
            if response.status_code in (200, 204):
                await self.db.execute(
                    "UPDATE orders SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                    (int(time.time()), order_id),
                )
                await self.db.commit()
                return True
            return False
        except Exception as e:
            logger.error("Error cancelling order: %s", e, exc_info=True)
            return False

    async def get_order_status(self, order_id: str) -> dict | None:
        """Get order status from Kalshi API."""
        await self._acquire_rate_limit()
        try:
            path = f"/trade-api/v2/portfolio/orders/{order_id}"
            headers = self._sign_request("GET", path)
            response = await self.http_client.get(
                f"{self.api_base}/portfolio/orders/{order_id}",
                headers=headers,
            )
            if response.status_code == 200:
                return response.json().get("order", response.json())
            return None
        except Exception as e:
            logger.error("Error getting order status: %s", e, exc_info=True)
            return None

    async def get_balance(self) -> float | None:
        """Get account balance in dollars."""
        await self._acquire_rate_limit()
        try:
            path = "/trade-api/v2/portfolio/balance"
            headers = self._sign_request("GET", path)
            response = await self.http_client.get(
                f"{self.api_base}/portfolio/balance", headers=headers
            )
            if response.status_code == 200:
                return response.json().get("balance", 0) / 100.0
            return None
        except Exception as e:
            logger.error("Error getting balance: %s", e, exc_info=True)
            return None

    async def close(self) -> None:
        """Close HTTP client."""
        await self.http_client.aclose()
