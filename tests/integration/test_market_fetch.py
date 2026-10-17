"""Exchange market-listing pagination: completeness or a loud failure.

A truncated market list is worse than none: callers store it and match on it
as if it were the whole exchange. These tests drive fetch_polymarket_markets
and fetch_kalshi_markets through httpx.MockTransport (offline) and assert
that every page is followed, transient errors are retried with backoff, and
an unrecoverable mid-pagination failure raises MarketFetchError instead of
returning a partial list.

Response shapes mirror live payloads (2026-10):
- gamma ``GET /markets`` returns HTTP 422 "offset too large, use
  /markets/keyset for deeper pagination" past offset 2000.
- gamma ``GET /markets/keyset`` returns ``{"markets": [...], "next_cursor":
  str | null}`` and takes ``after_cursor``; ``limit`` is capped at 100.
- Kalshi ``GET /markets`` returns ``{"markets": [...], "cursor": str}``.
"""

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.ingestor import store
from core.ingestor.store import (
    MarketFetchError,
    fetch_kalshi_markets,
    fetch_polymarket_markets,
)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Record backoff sleeps instead of waiting."""
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    monkeypatch.setattr(store, "_sleep", fake_sleep)
    return slept


def _gamma_market(i: int) -> dict:
    return {
        "id": str(i),
        "conditionId": f"0x{i:064x}",
        "question": f"Market {i}?",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{i}1", "{i}2"]',
        "outcomePrices": '["0.4", "0.6"]',
        "volume": "123.4",
        "description": "x" * 500,  # bulky field the fetcher may drop
        "events": [{"id": "e"}],
    }


def _keyset_handler(total: int, page_size: int = 100, fail: dict | None = None):
    """Serve `total` markets over /markets/keyset; `fail` maps call# -> response."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        n = len(calls)
        if fail and n in fail:
            return fail[n]
        if request.url.path != "/markets/keyset":
            return httpx.Response(
                422,
                json={
                    "type": "validation error",
                    "error": "offset too large, use /markets/keyset for deeper pagination",
                },
            )
        start = int(request.url.params.get("after_cursor") or 0)
        end = min(start + page_size, total)
        body = {
            "markets": [_gamma_market(i) for i in range(start, end)],
            "next_cursor": str(end) if end < total else None,
        }
        return httpx.Response(200, json=body)

    return handler, calls


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestPolymarketKeyset:
    async def test_fetches_beyond_offset_2000_via_keyset(self):
        handler, calls = _keyset_handler(total=2550)
        async with _client(handler) as client:
            markets = await fetch_polymarket_markets(client=client)
        assert len(markets) == 2550
        assert {m["conditionId"] for m in markets} == {
            f"0x{i:064x}" for i in range(2550)
        }
        assert all(c.url.path == "/markets/keyset" for c in calls)
        assert calls[0].url.params["active"] == "true"
        assert calls[0].url.params["closed"] == "false"
        assert "after_cursor" not in calls[0].url.params
        assert calls[1].url.params["after_cursor"] == "100"

    async def test_records_keep_fields_store_markets_needs(self):
        handler, _ = _keyset_handler(total=1)
        async with _client(handler) as client:
            (m,) = await fetch_polymarket_markets(client=client)
        for key in ("conditionId", "question", "clobTokenIds", "outcomePrices"):
            assert key in m
        # Bulky unused fields are dropped (300k markets must fit in memory).
        assert "description" not in m and "events" not in m

    async def test_transient_errors_are_retried_with_backoff(self, no_sleep):
        handler, calls = _keyset_handler(
            total=250,
            fail={
                2: httpx.Response(429, headers={"Retry-After": "7"}),
                3: httpx.Response(503),
            },
        )
        async with _client(handler) as client:
            markets = await fetch_polymarket_markets(client=client)
        assert len(markets) == 250
        assert no_sleep[0] == 7.0  # Retry-After respected
        assert len(no_sleep) == 2

    async def test_persistent_mid_pagination_failure_raises(self, no_sleep):
        always_500 = {n: httpx.Response(500) for n in range(3, 50)}
        handler, _ = _keyset_handler(total=1000, fail=always_500)
        async with _client(handler) as client:
            with pytest.raises(MarketFetchError, match="Polymarket"):
                await fetch_polymarket_markets(client=client)
        # Backoff grows between attempts.
        assert no_sleep == sorted(no_sleep) and no_sleep[-1] > no_sleep[0]

    async def test_non_retryable_client_error_raises(self):
        handler, _ = _keyset_handler(
            total=1000, fail={2: httpx.Response(422, json={"error": "bad"})}
        )
        async with _client(handler) as client:
            with pytest.raises(MarketFetchError):
                await fetch_polymarket_markets(client=client)

    async def test_transport_error_mid_pagination_raises(self):
        n = {"calls": 0}
        inner, _ = _keyset_handler(total=1000)

        def handler(request):
            n["calls"] += 1
            if n["calls"] >= 2:
                raise httpx.ConnectError("boom", request=request)
            return inner(request)

        async with _client(handler) as client:
            with pytest.raises(MarketFetchError):
                await fetch_polymarket_markets(client=client)


# ── Kalshi ──────────────────────────────────────────────────────────────


@pytest.fixture
def rsa_key_path(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "kalshi.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(path)


def _kalshi_market(i: int) -> dict:
    return {
        "ticker": f"KXTEST-{i}",
        "event_ticker": "KXTEST",
        "title": f"Test {i}",
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.42",
    }


def _kalshi_handler(total: int, page_size: int = 1000, fail: dict | None = None):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        n = len(calls)
        if fail and n in fail:
            return fail[n]
        start = int(request.url.params.get("cursor") or 0)
        end = min(start + page_size, total)
        return httpx.Response(
            200,
            json={
                "markets": [_kalshi_market(i) for i in range(start, end)],
                "cursor": str(end) if end < total else "",
            },
        )

    return handler, calls


KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class TestKalshiPagination:
    async def test_fetches_all_pages(self, rsa_key_path):
        handler, calls = _kalshi_handler(total=2500)
        async with _client(handler) as client:
            markets = await fetch_kalshi_markets(
                "key", rsa_key_path, KALSHI_BASE, client=client
            )
        assert len(markets) == 2500
        assert len(calls) == 3

    async def test_429_is_retried_respecting_retry_after(self, rsa_key_path, no_sleep):
        handler, calls = _kalshi_handler(
            total=2500,
            fail={2: httpx.Response(429, headers={"Retry-After": "3"})},
        )
        async with _client(handler) as client:
            markets = await fetch_kalshi_markets(
                "key", rsa_key_path, KALSHI_BASE, client=client
            )
        assert len(markets) == 2500
        assert no_sleep == [3.0]
        # The retried request is re-signed (fresh timestamp header present).
        assert "KALSHI-ACCESS-TIMESTAMP" in calls[2].headers

    async def test_429_without_retry_after_uses_exponential_backoff(
        self, rsa_key_path, no_sleep
    ):
        handler, _ = _kalshi_handler(
            total=2500,
            fail={2: httpx.Response(429), 3: httpx.Response(429)},
        )
        async with _client(handler) as client:
            markets = await fetch_kalshi_markets(
                "key", rsa_key_path, KALSHI_BASE, client=client
            )
        assert len(markets) == 2500
        assert len(no_sleep) == 2 and no_sleep[1] > no_sleep[0]

    async def test_persistent_failure_mid_pagination_raises(self, rsa_key_path):
        handler, _ = _kalshi_handler(
            total=5000, fail={n: httpx.Response(429) for n in range(2, 50)}
        )
        async with _client(handler) as client:
            with pytest.raises(MarketFetchError, match="Kalshi"):
                await fetch_kalshi_markets(
                    "key", rsa_key_path, KALSHI_BASE, client=client
                )


class TestKalshiCursorCycle:
    async def test_page_with_no_new_markets_raises(self, rsa_key_path):
        # Cursor keeps advancing but the server re-serves page 1: the old
        # code logged "cursor cycled" and returned 1000 of N markets.
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "markets": [_kalshi_market(i) for i in range(1000)],
                    "cursor": f"c{calls['n']}",
                },
            )

        async with _client(handler) as client:
            with pytest.raises(MarketFetchError, match="cycl"):
                await fetch_kalshi_markets(
                    "key", rsa_key_path, KALSHI_BASE, client=client
                )

    async def test_repeated_cursor_raises(self, rsa_key_path):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            start = calls["n"] * 1000
            return httpx.Response(
                200,
                json={
                    "markets": [_kalshi_market(i) for i in range(start, start + 1000)],
                    "cursor": "same",
                },
            )

        async with _client(handler) as client:
            with pytest.raises(MarketFetchError, match="cycl"):
                await fetch_kalshi_markets(
                    "key", rsa_key_path, KALSHI_BASE, client=client
                )
