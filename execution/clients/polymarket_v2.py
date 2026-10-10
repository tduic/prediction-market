"""Polymarket CLOB V2 execution client (py-clob-client-v2).

Replaces the archived py-clob-client path for live trading. Differences from
the legacy client that matter for money:

- LIMIT orders are FAK (fill-and-kill), so nothing is left resting on the
  book; MARKET orders are FOK.
- Fill state is never guessed. A post that fails ambiguously (transport
  error, 5xx, timeout) or a poll that cannot confirm the outcome returns
  ``status="pending"``, which halts the engine, instead of "failed" with a
  zero fill that a retry could double.
- Fees come from the market's live fee curve (``fd`` r, e) via the shared
  ``execution.market_data`` discovery, keyed by the condition id in
  ``markets.platform_id``.
- ``OrderResult.book`` reports "NO" when a SELL-YES intent was translated to
  BUY NO (no-naked-shorts), so the engine can convert the fill to YES space.
- The SDK is synchronous; every call runs in a worker thread so it never
  blocks the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from typing import Any

import aiosqlite

from core.engine.fees import taker_fee
from core.secrets import get_secret
from execution.clients.base import BaseExecutionClient, OrderResult
from execution.clients.polymarket_book import BookResolver, ResolvedOrder
from execution.market_data import get_market_data
from execution.models import OrderLeg

logger = logging.getLogger(__name__)

_TERMINAL = {"matched", "canceled", "cancelled", "unmatched", "expired"}


class PolymarketExecutionClientV2(BaseExecutionClient):
    """Order submission to Polymarket via the CLOB V2 API."""

    def __init__(
        self,
        db_connection: aiosqlite.Connection,
        private_key: str | None = None,
        funder: str | None = None,
        chain_id: int = 137,
        proxy_url: str | None = None,
        poll_interval_s: float = 0.25,
        max_polls: int = 40,
    ) -> None:
        super().__init__(db_connection, platform_label="polymarket")
        self._book_resolver = BookResolver(db_connection)
        self.private_key = private_key or get_secret("POLYMARKET_PRIVATE_KEY", "") or ""
        self.funder = funder or get_secret("POLYMARKET_WALLET_ADDRESS", "") or ""
        self.chain_id = chain_id
        self.host = os.getenv("POLYMARKET_API_BASE", "https://clob.polymarket.com")
        self.signature_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
        # SOCKS5 proxy for EU routing (e.g. "socks5://10.164.0.2:1080")
        self.proxy_url: str = proxy_url or os.getenv("POLYMARKET_PROXY") or ""
        self.poll_interval_s = poll_interval_s
        self.max_polls = max_polls
        self._client: Any = None
        self._initialized = False
        # _ensure_client runs in worker threads; concurrent pairs could race
        # to derive keys and swap the SDK's module-level HTTP client.
        self._init_lock = threading.Lock()

        self._rate_limit_tokens = 10.0
        self._rate_limit_max = 10.0
        self._rate_limit_refill_per_sec = 5.0
        self._rate_limit_last_refill = time.monotonic()

    async def _acquire_rate_limit(self) -> None:
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

    async def _call(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def _ensure_client(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if not self._initialized:
                self._build_client()

    def _build_client(self) -> None:
        from py_clob_client_v2 import ApiCreds, ClobClient

        if not self.private_key:
            raise ValueError("Polymarket private key required")
        # Route through the proxy before any request, including key derivation.
        if self.proxy_url:
            self._install_proxy()

        api_key = get_secret("POLYMARKET_API_KEY", "") or ""
        api_secret = get_secret("POLYMARKET_API_SECRET", "") or ""
        passphrase = get_secret("POLYMARKET_API_PASSPHRASE", "") or ""
        if api_key and api_secret and passphrase:
            creds = ApiCreds(
                api_key=api_key, api_secret=api_secret, api_passphrase=passphrase
            )
        else:
            creds = ClobClient(
                host=self.host, chain_id=self.chain_id, key=self.private_key
            ).create_or_derive_api_key()

        self._client = ClobClient(
            host=self.host,
            chain_id=self.chain_id,
            key=self.private_key,
            creds=creds,
            signature_type=self.signature_type,
            funder=self.funder or None,
        )
        self._initialized = True
        logger.info("Polymarket CLOB V2 client initialized")

    def _install_proxy(self) -> None:
        """Swap the SDK's module-level httpx client for a SOCKS5-routed one."""
        import httpx
        from httpx_socks import SyncProxyTransport
        from py_clob_client_v2.http_helpers import helpers

        transport = SyncProxyTransport.from_url(self.proxy_url)
        helpers._http_client = httpx.Client(transport=transport, http2=False)
        logger.info(
            "Polymarket V2 traffic routed through proxy: %s",
            self.proxy_url.split("@")[-1],  # host:port only, never credentials
        )

    @staticmethod
    def _local_id(prefix: str, leg: OrderLeg) -> str:
        """Unique id for an order the exchange never acknowledged.

        orders.id is the primary key, so reusing an id would drop the audit
        row. For UNKNOWN ids, reconcile by market and time, not by id.
        """
        return f"{prefix}-{leg.market_id}-{uuid.uuid4().hex[:8]}"

    def _result(self, order_id: str, status: str, start: float, **kw) -> OrderResult:
        return OrderResult(
            order_id=order_id,
            platform="polymarket",
            status=status,
            submission_latency_ms=int((time.time() - start) * 1000),
            **kw,
        )

    async def submit_order(
        self,
        leg: OrderLeg,
        signal_id: str | None = None,
        strategy: str | None = None,
    ) -> OrderResult:
        await self._acquire_rate_limit()
        start = time.time()
        resolved: ResolvedOrder | None = None

        # Phase 1 — build the order. Nothing has been sent yet, so any
        # failure here is a clean "failed".
        try:
            resolved = await self._book_resolver.resolve(
                leg.market_id, leg.side, leg.size, leg.limit_price
            )
            if resolved is None:
                raise ValueError(
                    f"BookResolver rejected order for {leg.market_id} "
                    f"(side={leg.side.value}, size={leg.size}, "
                    f"price={leg.limit_price})"
                )
            await self._call(self._ensure_client)
            from py_clob_client_v2 import (
                OrderArgs,
                OrderType,
                PartialCreateOrderOptions,
            )

            tick = await self._call(self._client.get_tick_size, resolved.token_id)
            order_args = OrderArgs(
                token_id=resolved.token_id,
                price=resolved.limit_price,
                size=resolved.size,
                side=resolved.side.value,
            )
            options = PartialCreateOrderOptions(tick_size=str(tick))
            order_type = OrderType.FOK if leg.order_type == "MARKET" else OrderType.FAK
        except Exception as exc:
            logger.exception("Polymarket V2 order could not be built")
            result = self._result(
                self._local_id("FAILED", leg),
                "failed",
                start,
                error_message=str(exc),
            )
            await self.write_order(leg, result, signal_id=signal_id, strategy=strategy)
            return result

        book = resolved.book.value
        logger.info(
            "Submitting to Polymarket V2: market=%s token=%s side=%s size=%s "
            "price=%s book=%s type=%s",
            leg.market_id,
            resolved.token_id,
            resolved.side.value,
            resolved.size,
            resolved.limit_price,
            book,
            order_type,
        )

        # Phase 2 — post. A definite rejection (4xx) is "failed"; anything
        # ambiguous may have reached the exchange and is "pending".
        try:
            response = await self._call(
                self._client.create_and_post_order, order_args, options, order_type
            )
        # Every exception must be classified here: an unclassified error after
        # the post could hide a live order.
        except Exception as exc:  # noqa: BLE001
            status_code = getattr(exc, "status_code", None)
            rejected = isinstance(status_code, int) and 400 <= status_code < 500
            logger.error(
                "Polymarket V2 post %s: %r",
                "rejected" if rejected else "outcome UNKNOWN",
                exc,
            )
            result = self._result(
                self._local_id("FAILED" if rejected else "UNKNOWN", leg),
                "failed" if rejected else "pending",
                start,
                error_message=f"post error: {exc!r}",
                book=book,
            )
            await self.write_order(
                leg, result, signal_id=signal_id, strategy=strategy, resolved=resolved
            )
            return result

        order_id = ""
        if isinstance(response, dict):
            order_id = str(
                response.get("orderID")
                or response.get("orderId")
                or response.get("id")
                or ""
            )
        if (
            not isinstance(response, dict)
            or response.get("success") is False
            or not order_id
        ):
            error = (
                response.get("errorMsg") if isinstance(response, dict) else response
            ) or "order rejected"
            result = self._result(
                order_id or self._local_id("FAILED", leg),
                "failed",
                start,
                error_message=str(error),
                book=book,
            )
            await self.write_order(
                leg, result, signal_id=signal_id, strategy=strategy, resolved=resolved
            )
            return result

        submission_latency_ms = int((time.time() - start) * 1000)
        await self.write_order(
            leg,
            self._result(order_id, "pending", start, book=book),
            signal_id=signal_id,
            strategy=strategy,
            resolved=resolved,
        )
        return await self._poll_for_fill(
            order_id, leg, resolved, start, submission_latency_ms
        )

    async def _get_order(self, order_id: str) -> dict | None:
        try:
            await self._acquire_rate_limit()
            order = await self._call(self._client.get_order, order_id)
            return order if isinstance(order, dict) else None
        # A failed poll is retried; the caller treats a final failure as unknown.
        except Exception as exc:  # noqa: BLE001
            logger.warning("Polymarket V2 poll error for %s: %r", order_id, exc)
            return None

    async def _poll_for_fill(
        self,
        order_id: str,
        leg: OrderLeg,
        resolved: ResolvedOrder,
        start: float,
        submission_latency_ms: int,
    ) -> OrderResult:
        for _ in range(self.max_polls):
            await asyncio.sleep(self.poll_interval_s)
            order = await self._get_order(order_id)
            if order is None:
                continue
            status = str(order.get("status", "")).lower()
            matched = float(order.get("size_matched") or 0)
            if status in _TERMINAL or matched >= resolved.size - 1e-9:
                return await self._finalize(order_id, leg, resolved, order, start)

        # Not terminal in time: cancel whatever is left, then read once more.
        logger.warning("Polymarket V2 fill poll timeout for %s; cancelling", order_id)
        await self.cancel_order(order_id)
        order = await self._get_order(order_id)
        if order is not None and (
            str(order.get("status", "")).lower() in _TERMINAL
            or float(order.get("size_matched") or 0) > 0
        ):
            return await self._finalize(order_id, leg, resolved, order, start)
        result = self._result(
            order_id,
            "pending",
            start,
            error_message="fill poll timeout; fill state unknown",
            book=resolved.book.value,
        )
        result.submission_latency_ms = submission_latency_ms
        return result

    async def _finalize(
        self,
        order_id: str,
        leg: OrderLeg,
        resolved: ResolvedOrder,
        order: dict,
        start: float,
    ) -> OrderResult:
        matched = float(order.get("size_matched") or 0)
        book = resolved.book.value
        if matched <= 0:
            result = self._result(
                order_id,
                "failed",
                start,
                error_message=f"no fill (status={order.get('status')})",
                book=book,
            )
            await self.update_order_fill(result)
            return result

        # The order's limit price: for a taker this is the worst price paid,
        # so P&L is never overstated.
        price = float(order.get("price") or resolved.limit_price)
        fee: float | None = None
        fee_error = None
        try:
            params = await asyncio.wait_for(
                get_market_data().fee_params(self.db, leg.market_id), timeout=2.0
            )
            if params is None:
                raise ValueError("fee metadata unavailable")
            fee = taker_fee(params, matched, price)
        except (TimeoutError, ValueError) as exc:
            fee_error = str(exc) or type(exc).__name__
            logger.error(
                "Polymarket V2 fill %s confirmed but fee unverified: %s",
                order_id,
                fee_error,
            )

        result = OrderResult(
            order_id=order_id,
            platform="polymarket",
            status="filled" if matched >= resolved.size - 1e-9 else "partially_filled",
            submission_latency_ms=int((time.time() - start) * 1000),
            fill_latency_ms=int((time.time() - start) * 1000),
            filled_price=round(price, 6),
            filled_size=matched,
            fee_paid=fee,
            slippage=round(abs(price - resolved.limit_price), 6),
            error_message=f"fee unverified: {fee_error}" if fee_error else None,
            book=book,
        )
        await self.update_order_fill(result)
        await self.write_fill_event(result)
        return result

    async def cancel_order(self, order_id: str) -> bool:
        try:
            from py_clob_client_v2 import OrderPayload

            await self._acquire_rate_limit()
            await self._call(self._ensure_client)
            await self._call(self._client.cancel_order, OrderPayload(orderID=order_id))
            return True
        except Exception:
            logger.exception("Failed to cancel Polymarket V2 order %s", order_id)
            return False

    async def get_order_status(self, order_id: str) -> dict | None:
        try:
            await self._call(self._ensure_client)
        except Exception:
            logger.exception("Polymarket V2 client init failed")
            return None
        return await self._get_order(order_id)

    async def get_balance(self) -> float | None:
        """Collateral (pUSD/USDC) balance in dollars."""
        try:
            from py_clob_client_v2 import AssetType, BalanceAllowanceParams

            await self._call(self._ensure_client)
            response = await self._call(
                self._client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL),
            )
            raw = response.get("balance")
            if raw is None:
                raise ValueError("collateral balance missing")
            scale = float(os.getenv("POLYMARKET_BALANCE_SCALE", "1000000"))
            return float(raw) / scale
        except Exception:
            logger.exception("Polymarket V2 balance lookup failed")
            return None

    async def close(self) -> None:
        self._client = None
        self._initialized = False
