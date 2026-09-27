"""Pre-trade market data from public venue endpoints: fees and book depth.

The arb engine asks a ``MarketData`` provider for each leg's fee parameters
(and, later, executable depth) right before trading. ``LiveMarketData`` reads
the venues' public, unauthenticated endpoints; ``StaticMarketData`` returns
fixed values for tests and offline runs.

Every lookup fails closed: any HTTP, parse, or metadata problem returns None
and the caller must skip the trade rather than assume zero fees.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Protocol

import aiosqlite
import httpx

from core.engine.fees import FeeParams, kalshi_params, polymarket_params
from execution.models import OrderLeg

logger = logging.getLogger(__name__)

DEFAULT_POLY_HOST = "https://clob.polymarket.com"
DEFAULT_KALSHI_PUBLIC_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_KALSHI_TAKER_FEE_TYPES = {"quadratic", "quadratic_with_maker_fees"}


class MarketData(Protocol):
    async def fee_params(
        self, db: aiosqlite.Connection, market_id: str
    ) -> FeeParams | None: ...

    async def executable_depth(
        self, db: aiosqlite.Connection, leg: OrderLeg
    ) -> float | None: ...


async def _platform_id(
    db: aiosqlite.Connection, market_id: str
) -> tuple[str, str] | None:
    cursor = await db.execute(
        "SELECT platform, platform_id FROM markets WHERE id = ?", (market_id,)
    )
    row = await cursor.fetchone()
    if row is None or not row[1]:
        return None
    return str(row[0]), str(row[1])


class LiveMarketData:
    """Reads fee metadata and order books from public venue endpoints."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        poly_host: str | None = None,
        kalshi_base: str | None = None,
        ttl_s: float = 3600.0,
    ) -> None:
        self._http = http or httpx.AsyncClient(timeout=5.0)
        self._poly_host = (
            poly_host or os.getenv("POLYMARKET_API_BASE") or DEFAULT_POLY_HOST
        ).rstrip("/")
        self._kalshi_base = (
            kalshi_base
            or os.getenv("KALSHI_PUBLIC_API_BASE")
            or DEFAULT_KALSHI_PUBLIC_BASE
        ).rstrip("/")
        self._ttl_s = ttl_s
        self._fee_cache: dict[str, tuple[float, FeeParams]] = {}

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        response = await self._http.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def fee_params(
        self, db: aiosqlite.Connection, market_id: str
    ) -> FeeParams | None:
        cached = self._fee_cache.get(market_id)
        if cached is not None and time.monotonic() - cached[0] < self._ttl_s:
            return cached[1]
        ident = await _platform_id(db, market_id)
        if ident is None:
            logger.warning("fee_params: no platform_id for market %s", market_id)
            return None
        platform, platform_id = ident
        params: FeeParams | None
        try:
            if platform == "polymarket":
                params = await self._polymarket_fees(platform_id)
            elif platform == "kalshi":
                params = await self._kalshi_fees(platform_id)
            else:
                return None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            logger.warning(
                "fee_params: lookup failed for %s (%s): %s",
                market_id,
                type(exc).__name__,
                exc,
            )
            return None
        if params is not None:
            self._fee_cache[market_id] = (time.monotonic(), params)
        return params

    async def _polymarket_fees(self, condition_id: str) -> FeeParams:
        info = await self._get_json(f"{self._poly_host}/clob-markets/{condition_id}")
        fd = info.get("fd")
        if not fd:
            # No fee data: the market is fee-free (SDK default rate 0).
            return polymarket_params(0.0, 1.0)
        return polymarket_params(float(fd.get("r") or 0.0), float(fd.get("e", 1.0)))

    async def _kalshi_fees(self, ticker: str) -> FeeParams | None:
        market = (await self._get_json(f"{self._kalshi_base}/markets/{ticker}"))[
            "market"
        ]
        event = (
            await self._get_json(f"{self._kalshi_base}/events/{market['event_ticker']}")
        )["event"]
        series = (
            await self._get_json(f"{self._kalshi_base}/series/{event['series_ticker']}")
        )["series"]
        fee_type = event.get("fee_type_override") or series.get("fee_type")
        multiplier = event.get("fee_multiplier_override")
        if multiplier is None:
            multiplier = series.get("fee_multiplier")
        if fee_type not in _KALSHI_TAKER_FEE_TYPES or multiplier is None:
            logger.warning(
                "fee_params: unsupported Kalshi fee metadata for %s "
                "(fee_type=%r multiplier=%r)",
                ticker,
                fee_type,
                multiplier,
            )
            return None
        return kalshi_params(float(multiplier))

    async def executable_depth(
        self, db: aiosqlite.Connection, leg: OrderLeg
    ) -> float | None:
        # Implemented with the depth-sizing work; unknown depth fails closed.
        return None


class StaticMarketData:
    """Fixed fee parameters and depth — for tests and offline runs."""

    def __init__(
        self,
        fees: dict[str, FeeParams] | None = None,
        depth: float | None = float("inf"),
    ) -> None:
        self._fees = {
            "polymarket": polymarket_params(0.0, 1.0),
            "kalshi": kalshi_params(0.0),
            **(fees or {}),
        }
        self._depth = depth

    async def fee_params(
        self, db: aiosqlite.Connection, market_id: str
    ) -> FeeParams | None:
        ident = await _platform_id(db, market_id)
        if ident is None:
            return None
        return self._fees.get(ident[0])

    async def executable_depth(
        self, db: aiosqlite.Connection, leg: OrderLeg | None
    ) -> float | None:
        return self._depth


_market_data: MarketData | None = None


def get_market_data() -> MarketData:
    global _market_data
    if _market_data is None:
        _market_data = LiveMarketData()
    return _market_data


def set_market_data(md: MarketData | None) -> None:
    global _market_data
    _market_data = md
