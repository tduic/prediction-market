"""
Kalshi REST API execution client.

Submits immediate-or-cancel orders through Kalshi's CreateOrderV2 endpoint
(POST /portfolio/events/orders) with RSA-PSS authentication and writes
complete order data to DB via BaseExecutionClient. The create response is
final for IOC orders, so there is no fill polling.
"""

import asyncio
import base64
import json
import logging
import math
import os
import time
import uuid
from pathlib import Path

import aiosqlite
import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from core.secrets import get_secret
from execution.clients.base import BaseExecutionClient, OrderResult
from execution.enums import Side
from execution.models import OrderLeg

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30


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

    async def _ticker(self, market_id: str) -> str:
        """Exchange ticker for an internal market id (``kal_<TICKER>``)."""
        cursor = await self.db.execute(
            "SELECT platform_id FROM markets WHERE id = ?", (market_id,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return str(row[0])
        return market_id.removeprefix("kal_")

    @staticmethod
    def _price(side: Side, limit: float) -> str:
        """Dollar price on a cent level, rounded only in our favour: a buy
        never pays more than its limit, a sell never accepts less."""
        cents = limit * 100
        rounded = (
            math.floor(cents + 1e-9) if side is Side.BUY else math.ceil(cents - 1e-9)
        )
        return f"{rounded / 100:.4f}"

    async def submit_order(
        self,
        leg: OrderLeg,
        signal_id: str | None = None,
        strategy: str | None = None,
    ) -> OrderResult:
        """Immediate-or-cancel order via CreateOrderV2.

        The 201 response is final for IOC (fill_count, average_fill_price,
        average_fee_paid), so there is no polling. A definite rejection (4xx)
        is "failed"; a 5xx or transport error may have reached the exchange
        and is "pending" (fill state unknown), which halts the engine.
        """
        await self._acquire_rate_limit()
        start_time = time.time()
        client_order_id = uuid.uuid4().hex

        def _result(order_id: str, status: str, **kw) -> OrderResult:
            return OrderResult(
                order_id=order_id,
                platform="kalshi",
                status=status,
                submission_latency_ms=int((time.time() - start_time) * 1000),
                **kw,
            )

        if leg.limit_price is None:
            result = _result(
                f"FAILED-{leg.market_id}-{client_order_id[:8]}",
                "failed",
                error_message="Kalshi CreateOrderV2 requires a limit price",
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result

        # Whole contracts only: fractional trading isn't available on every
        # market, and flooring never oversizes.
        contracts = math.floor(leg.size + 1e-9)
        if contracts < 1:
            result = _result(
                f"FAILED-{leg.market_id}-{client_order_id[:8]}",
                "failed",
                error_message=f"size {leg.size} is below one whole contract",
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result

        ticker = await self._ticker(leg.market_id)
        body = {
            "ticker": ticker,
            "side": "bid" if leg.side is Side.BUY else "ask",
            "count": str(contracts),
            "price": self._price(leg.side, leg.limit_price),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id,
        }
        logger.info(
            "Submitting to Kalshi: ticker=%s side=%s count=%s price=%s coid=%s",
            ticker,
            body["side"],
            body["count"],
            body["price"],
            client_order_id,
        )
        path = "/trade-api/v2/portfolio/events/orders"
        try:
            headers = self._sign_request("POST", path)
        except ValueError as exc:  # missing API key / RSA key: nothing sent
            result = _result(
                f"FAILED-{leg.market_id}-{client_order_id[:8]}",
                "failed",
                error_message=f"cannot sign Kalshi request: {exc}",
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result
        try:
            response = await self.http_client.post(
                f"{self.api_base}/portfolio/events/orders",
                content=json.dumps(body),
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.error("Kalshi order outcome UNKNOWN (%s): %r", client_order_id, exc)
            result = _result(
                f"UNKNOWN-kalshi-{client_order_id}",
                "pending",
                error_message=f"transport error, reconcile by client_order_id: {exc!r}",
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result

        if response.status_code != 201:
            # 408 (timeout) and 409 (conflict) don't prove nothing executed.
            definite = (
                400 <= response.status_code < 500
                and response.status_code
                not in (
                    408,
                    409,
                )
            )
            result = _result(
                f"{'FAILED' if definite else 'UNKNOWN'}-kalshi-{client_order_id}",
                "failed" if definite else "pending",
                error_message=f"HTTP {response.status_code}: {response.text[:500]}",
            )
            logger.error(
                "Kalshi order %s: %s",
                "rejected" if definite else "outcome UNKNOWN",
                result.error_message,
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result

        # Accepted (201). From here an unreadable body must not become an
        # exception or a "failed": something may have executed.
        order_id = f"UNKNOWN-kalshi-{client_order_id}"
        try:
            data = response.json()
            order_id = str(data.get("order_id") or order_id)
            # fill_count is required in the 201 body; never default it to 0.
            filled = float(data["fill_count"])
            if not math.isfinite(filled) or not 0 <= filled <= contracts + 1e-9:
                raise ValueError(f"implausible fill_count {data['fill_count']!r}")
            price = float(data["average_fill_price"]) if filled > 0 else 0.0
            if filled > 0 and not 0.0 < price < 1.0:
                raise ValueError(f"invalid average_fill_price {price!r}")
            raw_fee = data.get("average_fee_paid")
            fee_paid = (
                round(filled * float(raw_fee), 6)
                if filled > 0 and raw_fee is not None
                else None
            )
            if fee_paid is not None and not (math.isfinite(fee_paid) and fee_paid >= 0):
                raise ValueError(f"implausible average_fee_paid {raw_fee!r}")
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.error(
                "Kalshi order %s accepted but response unreadable: %r", order_id, exc
            )
            result = _result(
                order_id,
                "pending",
                error_message=f"accepted, unreadable response: {exc!r}",
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result
        if filled <= 0:
            result = _result(
                order_id, "failed", error_message="no fill (IOC)", no_fill=True
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result

        result = _result(
            order_id,
            "filled" if filled >= contracts - 1e-9 else "partially_filled",
            fill_latency_ms=int((time.time() - start_time) * 1000),
            filled_price=round(price, 6),
            filled_size=filled,
            fee_paid=fee_paid,
            slippage=round(abs(price - leg.limit_price), 6),
            error_message=None if fee_paid is not None else "fee unreported",
        )
        await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
        await self.write_fill_event(result)
        return result

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel via CancelOrderV2. Signs the path without the query string."""
        await self._acquire_rate_limit()
        try:
            cursor = await self.db.execute(
                "SELECT market_id FROM orders WHERE id = ?", (order_id,)
            )
            row = await cursor.fetchone()
            params = {"market_ticker": await self._ticker(row[0])} if row else None
            path = f"/trade-api/v2/portfolio/events/orders/{order_id}"
            response = await self.http_client.delete(
                f"{self.api_base}/portfolio/events/orders/{order_id}",
                params=params,
                headers=self._sign_request("DELETE", path),
            )
            if response.status_code == 200:
                await self.db.execute(
                    "UPDATE orders SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                    (int(time.time()), order_id),
                )
                await self.db.commit()
                return True
            return False
        except Exception:
            logger.exception("Error cancelling order")
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
        except Exception:
            logger.exception("Error getting order status")
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
        except Exception:
            logger.exception("Error getting balance")
            return None

    async def close(self) -> None:
        """Close HTTP client."""
        await self.http_client.aclose()
