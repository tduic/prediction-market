"""Kalshi API client with polling and rate limiting."""

import hashlib
import hmac
import logging
import os
import time
from datetime import datetime

import httpx

from core.ingestor.polymarket import MarketData, OrderBook, TokenBucket

logger = logging.getLogger(__name__)


class KalshiClient:
    """Kalshi API client with rate limiting and HMAC authentication."""

    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        """
        Args:
            api_key: API key (or from KALSHI_API_KEY env var)
            api_secret: API secret (or from KALSHI_API_SECRET env var)
        """
        self.api_key = api_key or os.getenv("KALSHI_API_KEY", "")
        self.api_secret = api_secret or os.getenv("KALSHI_API_SECRET", "")
        self.rate_limiter = TokenBucket(capacity=10.0, refill_rate=10.0 / 1.0)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            raise RuntimeError(
                "Client not initialized. Use 'async with' context manager."
            )
        return self._client

    def _sign_request(self, method: str, path: str, body: str = "") -> dict[str, str]:
        """Generate HMAC-SHA256 signature for request."""
        timestamp = str(int(time.time()))
        message = method + path + body + timestamp
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "KALSHI-SIGNATURE": signature,
            "KALSHI-TIMESTAMP": timestamp,
            "KALSHI-API-KEY": self.api_key,
        }

    async def poll_markets(
        self,
        status: str | None = None,
        category: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[MarketData]:
        """
        Poll markets from Kalshi API.

        Args:
            status: Filter by status (active, paused, resolved, etc.)
            category: Filter by category
            limit: Results per page
            offset: Pagination offset

        Returns:
            List of MarketData objects
        """
        if not self.rate_limiter.acquire():
            logger.warning("Rate limit exceeded for Kalshi")
            return []

        try:
            params = {"limit": limit, "offset": offset}
            if status:
                params["status"] = status
            if category:
                params["category"] = category

            path = "/markets?" + "&".join(f"{k}={v}" for k, v in params.items())
            headers = self._sign_request("GET", path)

            response = await self.client.get(path, headers=headers)
            response.raise_for_status()
            data = response.json()

            markets = []
            for item in data.get("markets", []):
                market = self._parse_market(item)
                if market:
                    markets.append(market)

            logger.info(f"Polled {len(markets)} markets from Kalshi")
            return markets

        except httpx.HTTPError as e:
            logger.error(f"Kalshi API error: {e}")
            return []

    async def get_market(self, ticker: str) -> MarketData | None:
        """
        Get single market by ticker.

        Args:
            ticker: Market ticker

        Returns:
            MarketData or None if not found
        """
        if not self.rate_limiter.acquire():
            return None

        try:
            path = f"/markets/{ticker}"
            headers = self._sign_request("GET", path)

            response = await self.client.get(path, headers=headers)
            response.raise_for_status()
            item = response.json()

            return self._parse_market(item)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"Market {ticker} not found")
            else:
                logger.error(f"Error fetching market {ticker}: {e}")
            return None
        except httpx.HTTPError as e:
            logger.error(f"Kalshi API error: {e}")
            return None

    async def get_orderbook(self, ticker: str) -> OrderBook | None:
        """
        Get orderbook for a market.

        Args:
            ticker: Market ticker

        Returns:
            OrderBook or None
        """
        if not self.rate_limiter.acquire():
            return None

        try:
            path = f"/markets/{ticker}/orderbook"
            headers = self._sign_request("GET", path)

            response = await self.client.get(path, headers=headers)
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
            logger.error(f"Error fetching orderbook for {ticker}: {e}")
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
            logger.warning(f"Failed to parse market item: {e}")
            return None
