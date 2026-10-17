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
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from core.ingestor.polymarket import MarketData, TokenBucket
from core.ingestor.quotes import two_sided_mid
from core.secrets import get_secret

logger = logging.getLogger(__name__)

# Max time to wait for a rate-limit token before giving up on a request.
_RATE_LIMIT_WAIT_S = 5.0
_RATE_LIMIT_POLL_S = 0.1


def _load_rsa_private_key(key_path: str) -> RSAPrivateKey | None:
    """Load an RSA PEM key, returning None when it is unavailable or invalid."""
    try:
        expanded = Path(key_path).expanduser()
        if not expanded.exists():
            logger.warning("Kalshi RSA key not found at %s", expanded)
            return None
        key = serialization.load_pem_private_key(expanded.read_bytes(), password=None)
        if not isinstance(key, RSAPrivateKey):
            logger.warning(
                "Kalshi key at %s is not an RSA private key (got %s)",
                expanded,
                type(key).__name__,
            )
            return None
        return key
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
        self.api_key: str = api_key or get_secret("KALSHI_API_KEY", "") or ""
        self.api_base: str = (
            api_base
            or os.getenv("KALSHI_API_BASE")
            or "https://api.elections.kalshi.com/trade-api/v2"
        )
        self.rate_limiter = TokenBucket(capacity=10.0, refill_rate=10.0 / 1.0)
        self._client: httpx.AsyncClient | None = None

        key_path = rsa_key_path or get_secret("KALSHI_RSA_KEY_PATH", "") or ""
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
        if not self.is_authenticated or self._private_key is None:
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

    def _parse_market(self, item: dict) -> MarketData | None:
        """Parse a Kalshi market (``/markets/{ticker}`` body) into MarketData.

        Current schema: the market is wrapped as ``{"market": {...}}`` and
        prices/quantities are decimal strings in ``*_dollars`` (0-1 scale) and
        ``*_fp`` fields. Legacy integer-cent fields are still accepted.

        ``last_price`` is the live reference price paper fills use: the mid of
        the YES book when both sides are quoted, else the last trade, else
        0.0 (which callers treat as "no price").
        """
        if isinstance(item.get("market"), dict):
            item = item["market"]
        try:
            yes_bid = _dollars(item, "yes_bid")
            yes_ask = _dollars(item, "yes_ask")
            no_bid = _dollars(item, "no_bid")
            no_ask = _dollars(item, "no_ask")
            last_trade = _dollars(item, "last_price")
            yes_mid = _two_sided_mid(yes_bid, yes_ask)
            if yes_mid is not None:
                last_price = yes_mid
            elif last_trade is not None and 0.0 < last_trade < 1.0:
                last_price = last_trade
            else:
                last_price = 0.0

            resolution_raw = (
                item.get("expected_expiration_time")
                or item.get("close_time")
                or item.get("resolution_date")
            )
            return MarketData(
                market_id=item.get("ticker", ""),
                platform="kalshi",
                symbol=item.get("ticker", ""),
                question=item.get("title", ""),
                description=item.get("rules_primary", item.get("description", "")),
                resolution_date=(
                    datetime.fromisoformat(resolution_raw) if resolution_raw else None
                ),
                last_price=last_price,
                last_price_no=_two_sided_mid(no_bid, no_ask),
                is_active=item.get("status") in ("active", "open"),
                metadata={
                    "category": item.get("category"),
                    "status": item.get("status"),
                    "event_ticker": item.get("event_ticker"),
                    "yes_sub_title": item.get("yes_sub_title"),
                    "volume": _fp(item, "volume"),
                    "volume_24h": _fp(item, "volume_24h"),
                    "open_interest": _fp(item, "open_interest"),
                    "liquidity": _dollars(item, "liquidity"),
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "last_trade_price": last_trade,
                },
            )
        except (KeyError, ValueError, TypeError) as e:
            logger.warning("Failed to parse market item: %s", e)
            return None


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _dollars(item: dict, name: str) -> float | None:
    """Read ``<name>_dollars`` (0-1 string), falling back to legacy cents."""
    v = _num(item.get(f"{name}_dollars"))
    if v is not None:
        return v
    cents = _num(item.get(name))
    return cents / 100.0 if cents is not None else None


def _fp(item: dict, name: str) -> float | None:
    """Read ``<name>_fp`` (decimal string), falling back to the legacy int."""
    v = _num(item.get(f"{name}_fp"))
    return v if v is not None else _num(item.get(name))


_two_sided_mid = two_sided_mid
