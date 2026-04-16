"""Kalshi API client with polling and rate limiting."""

import asyncio
import base64
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from core.ingestor.polymarket import MarketData, OrderBook, TokenBucket

logger = logging.getLogger(__name__)

# Max time to wait for a rate-limit token before giving up on a request.
_RATE_LIMIT_WAIT_S = 5.0
_RATE_LIMIT_POLL_S = 0.1


def _load_rsa_private_key(key_path: str):
    """Load RSA private key from PEM file. Returns None if path missing or unreadable."""
    try:
        expanded = Path(key_path).expanduser()
        if not expanded.exists():
            logger.warning("Kalshi RSA key not found at %s", expanded)
            return None
        return serialization.load_pem_private_key(expanded.read_bytes(), password=None)
    except Exception as e:
        logger.warning("Failed to load Kalshi RSA key from %s: %s", key_path, e)
        return None


class KalshiClient:
    """Kalshi API client with rate limiting and RSA-PSS authentication."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        rsa_key_path: str | None = None,
        api_base: str | None = None,
    ):
        self.api_key = api_key or os.getenv("KALSHI_API_KEY", "")
        self.api_base = api_base or os.getenv(
            "KALSHI_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"
        )
        self.rate_limiter = TokenBucket(capacity=10.0, refill_rate=10.0 / 1.0)
        self._client: httpx.AsyncClient | None = None

        key_path = rsa_key_path or os.getenv("KALSHI_RSA_KEY_PATH", "")
        self._private_key = _load_rsa_private_key(key_path) if key_path else None

    async def _wait_for_token(self) -> bool:
        """Await until a rate-limit token is available, up to the wait budget.

        Returns True if a token was acquired, False on timeout. Prevents the
        silent data drop of plain ``acquire()`` while capping the wait so
        sustained overload surfaces as a visible warning.
        """
        if self.rate_limiter.acquire():
            return True
        deadline = time.monotonic() + _RATE_LIMIT_WAIT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(_RATE_LIMIT_POLL_S)
            if self.rate_limiter.acquire():
                return True
        return False

    async def __aenter__(self):
        self._client = httpx.AsyncClient(base_url=self.api_base, timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                "Client not initialized. Use 'async with' context manager."
            )
        return self._client

    @property
    def is_authenticated(self) -> bool:
        """True when both API key and RSA private key are configured."""
        return bool(self.api_key and self._private_key)

    def _sign_request(self, method: str, path: str) -> dict[str, str]:
        """Generate RSA-PSS signature headers for Kalshi API.

        Returns an empty dict when credentials are not configured. Kalshi's
        read endpoints (/markets, /markets/{ticker}, /markets/{ticker}/orderbook)
        are public, so unsigned requests are still valid. Callers that hit
        authenticated endpoints (orders, portfolio) must gate on
        ``is_authenticated`` first.
        """
        if not self.is_authenticated:
            return {}
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

    async def poll_markets(
        self,
        status: str | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MarketData]:
        """Poll markets from Kalshi API."""
        if not await self._wait_for_token():
            logger.warning(
                "Kalshi rate limit: no token available after %.1fs — skipping poll",
                _RATE_LIMIT_WAIT_S,
            )
            return []

        try:
            params: dict[str, str | int] = {"limit": limit, "offset": offset}
            if status:
                params["status"] = status
            if category:
                params["category"] = category

            # Sign the canonical full API path without the query string. Kalshi's
            # signature covers method + path only; query params must be sent as
            # a separate dict to httpx. See scripts/verify_api_auth.py and
            # core/ingestor/store.py for the canonical pattern.
            headers = self._sign_request("GET", "/trade-api/v2/markets")

            response = await self.client.get("/markets", headers=headers, params=params)
            response.raise_for_status()
            data = response.json()

            markets = []
            for item in data.get("markets", []):
                market = self._parse_market(item)
                if market:
                    markets.append(market)

            logger.info("Polled %d markets from Kalshi", len(markets))
            return markets

        except httpx.HTTPError as e:
            logger.error("Kalshi API error: %s", e)
            return []

    async def get_market(self, ticker: str) -> MarketData | None:
        """Get single market by ticker."""
        if not await self._wait_for_token():
            logger.warning(
                "Kalshi rate limit: no token available after %.1fs — "
                "skipping get_market(%s)",
                _RATE_LIMIT_WAIT_S,
                ticker,
            )
            return None

        try:
            # Sign the canonical full API path (includes /trade-api/v2 prefix);
            # the request URL stays relative to api_base which already has the
            # prefix baked in.
            headers = self._sign_request("GET", f"/trade-api/v2/markets/{ticker}")

            response = await self.client.get(f"/markets/{ticker}", headers=headers)
            response.raise_for_status()
            item = response.json()

            return self._parse_market(item)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug("Market %s not found", ticker)
            else:
                logger.error("Error fetching market %s: %s", ticker, e)
            return None
        except httpx.HTTPError as e:
            logger.error("Kalshi API error: %s", e)
            return None

    async def get_orderbook(self, ticker: str) -> OrderBook | None:
        """Get orderbook for a market."""
        if not await self._wait_for_token():
            logger.warning(
                "Kalshi rate limit: no token available after %.1fs — "
                "skipping get_orderbook(%s)",
                _RATE_LIMIT_WAIT_S,
                ticker,
            )
            return None

        try:
            # Sign the canonical full API path (with /trade-api/v2 prefix);
            # request URL stays relative to api_base.
            headers = self._sign_request(
                "GET", f"/trade-api/v2/markets/{ticker}/orderbook"
            )

            response = await self.client.get(
                f"/markets/{ticker}/orderbook", headers=headers
            )
            response.raise_for_status()
            data = response.json()

            book = OrderBook(
                token_id=ticker,
                bids=data.get("bids", []),
                asks=data.get("asks", []),
            )
            book.compute_mid_price()
            return book

        except httpx.HTTPError as e:
            logger.error("Error fetching orderbook for %s: %s", ticker, e)
            return None

    def _parse_market(self, item: dict) -> MarketData | None:
        """Parse Kalshi API response into MarketData."""
        try:
            return MarketData(
                market_id=item.get("ticker", ""),
                platform="kalshi",
                symbol=item.get("ticker", ""),
                question=item.get("title", ""),
                description=item.get("description", ""),
                resolution_date=(
                    datetime.fromisoformat(item["resolution_date"])
                    if item.get("resolution_date")
                    else None
                ),
                last_price=float(item.get("last_price", 0)),
                is_active=item.get("status") == "active",
                metadata={
                    "category": item.get("category"),
                    "status": item.get("status"),
                    "volume": item.get("volume"),
                    "liquidity": item.get("liquidity"),
                    "yes_bid": item.get("yes_bid"),
                    "yes_ask": item.get("yes_ask"),
                },
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Failed to parse market item: %s", e)
            return None
