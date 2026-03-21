"""Polymarket API client with polling and rate limiting."""

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


import httpx

logger = logging.getLogger(__name__)


@dataclass
class OrderBook:
    """Order book for a single market token."""

    token_id: str
    bids: list[dict] = field(default_factory=list)
    asks: list[dict] = field(default_factory=list)
    mid_price: float | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def compute_mid_price(self) -> None:
        """Compute mid price from top bid/ask."""
        if self.bids and self.asks:
            top_bid = max(b.get("price", 0) for b in self.bids)
            top_ask = min(a.get("price", 1) for a in self.asks)
            self.mid_price = (top_bid + top_ask) / 2


@dataclass
class MarketData:
    """Internal market data representation."""

    market_id: str
    platform: str
    symbol: str
    question: str
    description: str
    resolution_date: datetime | None
    last_price: float
    order_book: OrderBook | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True
    metadata: dict = field(default_factory=dict)


class TokenBucket:
    """Token bucket rate limiter."""

    def __init__(self, capacity: float, refill_rate: float):
        """
        Args:
            capacity: Maximum tokens in bucket
            refill_rate: Tokens added per second
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity
        self.last_refill = time.monotonic()

    def acquire(self, tokens: float = 1.0) -> bool:
        """Try to acquire tokens. Returns True if successful."""
        self._refill()
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False

    def _refill(self) -> None:
        """Refill bucket based on elapsed time."""
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now


class PolymarketClient:
    """Polymarket API client with rate limiting and HMAC signing."""

    BASE_URL = "https://clob.polymarket.com"

    def __init__(self, api_key: str | None = None, api_secret: str | None = None):
        """
        Args:
            api_key: API key (or from POLYMARKET_API_KEY env var)
            api_secret: API secret (or from POLYMARKET_API_SECRET env var)
        """
        self.api_key = api_key or os.getenv("POLYMARKET_API_KEY", "")
        self.api_secret = api_secret or os.getenv("POLYMARKET_API_SECRET", "")
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

    def _sign_request(self, method: str, path: str, body: str = "") -> dict:
        """Generate HMAC signature for request."""
        timestamp = str(int(time.time() * 1000))
        message = method + path + body + timestamp
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {
            "POLY-SIGNATURE": signature,
            "POLY-TIMESTAMP": timestamp,
            "POLY-API-KEY": self.api_key,
        }

    async def poll_markets(self, limit: int = 100, offset: int = 0) -> list[MarketData]:
        """
        Poll all markets from Polymarket API.

        Args:
            limit: Results per page
            offset: Pagination offset

        Returns:
            List of MarketData objects
        """
        if not self.rate_limiter.acquire():
            logger.warning("Rate limit exceeded for Polymarket")
            return []

        try:
            path = f"/markets?limit={limit}&offset={offset}"
            headers = self._sign_request("GET", path)

            response = await self.client.get(path, headers=headers)
            response.raise_for_status()
            data = response.json()

            markets = []
            for item in data.get("data", []):
                market = self._parse_market(item)
                if market:
                    markets.append(market)

            logger.info(f"Polled {len(markets)} markets from Polymarket")
            return markets

        except httpx.HTTPError as e:
            logger.error(f"Polymarket API error: {e}")
            return []

    async def get_market(self, condition_id: str) -> MarketData | None:
        """
        Get single market by condition ID.

        Args:
            condition_id: Polymarket condition ID

        Returns:
            MarketData or None if not found
        """
        if not self.rate_limiter.acquire():
            return None

        try:
            path = f"/markets/{condition_id}"
            headers = self._sign_request("GET", path)

            response = await self.client.get(path, headers=headers)
            response.raise_for_status()
            item = response.json()

            return self._parse_market(item)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"Market {condition_id} not found")
            else:
                logger.error(f"Error fetching market {condition_id}: {e}")
            return None
        except httpx.HTTPError as e:
            logger.error(f"Polymarket API error: {e}")
            return None

    async def get_orderbook(self, token_id: str) -> OrderBook | None:
        """
        Get orderbook for a token.

        Args:
            token_id: Token ID

        Returns:
            OrderBook or None
        """
        if not self.rate_limiter.acquire():
            return None

        try:
            path = f"/book?token_id={token_id}"
            headers = self._sign_request("GET", path)

            response = await self.client.get(path, headers=headers)
            response.raise_for_status()
            data = response.json()

            book = OrderBook(
                token_id=token_id,
                bids=data.get("bids", []),
                asks=data.get("asks", []),
            )
            book.compute_mid_price()
            return book

        except httpx.HTTPError as e:
            logger.error(f"Error fetching orderbook for {token_id}: {e}")
            return None

    def _parse_market(self, item: dict) -> MarketData | None:
        """Parse Polymarket API response into MarketData."""
        try:
            return MarketData(
                market_id=item.get("conditionId", ""),
                platform="polymarket",
                symbol=item.get("slug", ""),
                question=item.get("question", ""),
                description=item.get("description", ""),
                resolution_date=(
                    datetime.fromisoformat(item["resolutionDate"])
                    if item.get("resolutionDate")
                    else None
                ),
                last_price=float(item.get("lastPrice", 0)),
                is_active=item.get("active", True),
                metadata={
                    "liquidity": item.get("liquidity"),
                    "volume": item.get("volume"),
                    "outcomeTokenIds": item.get("outcomeTokenIds", []),
                },
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse market item: {e}")
            return None
