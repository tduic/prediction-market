"""Tests for LiveMarketData.fee_params — public fee-metadata discovery."""

from datetime import datetime, timezone

import httpx

from core.engine.fees import kalshi_params, polymarket_params
from execution.market_data import LiveMarketData, StaticMarketData

POLY = "https://clob.test"
KALSHI = "https://kalshi.test/trade-api/v2"


async def _seed(db, market_id, platform, platform_id):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO markets (id, platform, platform_id, title, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (market_id, platform, platform_id, "t", now, now),
    )
    await db.commit()


def _live(handler) -> tuple[LiveMarketData, list[str]]:
    calls: list[str] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return LiveMarketData(http=http, poly_host=POLY, kalshi_base=KALSHI), calls


def _kalshi_handler(series_fee, event_extra=None):
    def handler(request):
        path = request.url.path
        if path.endswith("/markets/KXTEST-1"):
            return httpx.Response(200, json={"market": {"event_ticker": "KXTEST"}})
        if path.endswith("/events/KXTEST"):
            return httpx.Response(
                200,
                json={"event": {"series_ticker": "KXS", **(event_extra or {})}},
            )
        if path.endswith("/series/KXS"):
            return httpx.Response(200, json={"series": series_fee})
        return httpx.Response(404)

    return handler


class TestPolymarketFees:
    async def test_fd_present_uses_condition_id(self, db):
        await _seed(db, "poly_0xabc", "polymarket", "0xabc")

        def handler(request):
            assert request.url.path == "/clob-markets/0xabc"
            return httpx.Response(200, json={"fd": {"r": 0.05, "e": 1, "to": True}})

        md, _ = _live(handler)
        assert await md.fee_params(db, "poly_0xabc") == polymarket_params(0.05, 1.0)

    async def test_fd_null_is_fee_free(self, db):
        await _seed(db, "poly_0xabc", "polymarket", "0xabc")
        md, _ = _live(lambda r: httpx.Response(200, json={"fd": None, "t": []}))
        assert await md.fee_params(db, "poly_0xabc") == polymarket_params(0.0, 1.0)

    async def test_exponent_zero_preserved(self, db):
        await _seed(db, "poly_0xabc", "polymarket", "0xabc")
        md, _ = _live(lambda r: httpx.Response(200, json={"fd": {"r": 0.03, "e": 0}}))
        params = await md.fee_params(db, "poly_0xabc")
        assert params is not None and params.exponent == 0.0

    async def test_http_error_fails_closed(self, db):
        await _seed(db, "poly_0xabc", "polymarket", "0xabc")
        md, _ = _live(lambda r: httpx.Response(500))
        assert await md.fee_params(db, "poly_0xabc") is None

    async def test_cached_after_first_fetch(self, db):
        await _seed(db, "poly_0xabc", "polymarket", "0xabc")
        md, calls = _live(
            lambda r: httpx.Response(200, json={"fd": {"r": 0.05, "e": 1}})
        )
        await md.fee_params(db, "poly_0xabc")
        await md.fee_params(db, "poly_0xabc")
        assert len(calls) == 1


class TestKalshiFees:
    async def test_series_multiplier(self, db):
        await _seed(db, "kal_KXTEST-1", "kalshi", "KXTEST-1")
        md, _ = _live(_kalshi_handler({"fee_type": "quadratic", "fee_multiplier": 0.5}))
        assert await md.fee_params(db, "kal_KXTEST-1") == kalshi_params(0.5)

    async def test_event_override_beats_series(self, db):
        await _seed(db, "kal_KXTEST-1", "kalshi", "KXTEST-1")
        md, _ = _live(
            _kalshi_handler(
                {"fee_type": "quadratic", "fee_multiplier": 1},
                event_extra={"fee_multiplier_override": 2},
            )
        )
        assert await md.fee_params(db, "kal_KXTEST-1") == kalshi_params(2.0)

    async def test_maker_fee_type_uses_taker_curve(self, db):
        await _seed(db, "kal_KXTEST-1", "kalshi", "KXTEST-1")
        md, _ = _live(
            _kalshi_handler(
                {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
            )
        )
        assert await md.fee_params(db, "kal_KXTEST-1") == kalshi_params(1.0)

    async def test_unsupported_fee_type_fails_closed(self, db):
        await _seed(db, "kal_KXTEST-1", "kalshi", "KXTEST-1")
        md, _ = _live(_kalshi_handler({"fee_type": "flat", "fee_multiplier": 1}))
        assert await md.fee_params(db, "kal_KXTEST-1") is None

    async def test_missing_multiplier_fails_closed(self, db):
        await _seed(db, "kal_KXTEST-1", "kalshi", "KXTEST-1")
        md, _ = _live(_kalshi_handler({"fee_type": "quadratic"}))
        assert await md.fee_params(db, "kal_KXTEST-1") is None


async def test_unknown_market_fails_closed(db):
    md, calls = _live(lambda r: httpx.Response(200, json={}))
    assert await md.fee_params(db, "poly_missing") is None
    assert calls == []


class TestStaticMarketData:
    async def test_defaults_are_zero_fee_and_unlimited_depth(self, db):
        md = StaticMarketData()
        await _seed(db, "kal_X", "kalshi", "X")
        params = await md.fee_params(db, "kal_X")
        assert params is not None and params.rate == 0.0
        assert await md.executable_depth(db, None) == float("inf")

    async def test_explicit_params_by_platform(self, db):
        md = StaticMarketData(fees={"kalshi": kalshi_params()})
        await _seed(db, "kal_X", "kalshi", "X")
        assert await md.fee_params(db, "kal_X") == kalshi_params()


class TestHotPathSafety:
    async def test_non_dict_json_fails_closed(self, db):
        await _seed(db, "poly_0xabc", "polymarket", "0xabc")
        md, _ = _live(lambda r: httpx.Response(200, json=["not", "a", "dict"]))
        assert await md.fee_params(db, "poly_0xabc") is None

    async def test_expired_entry_served_while_refreshing(self, db):
        import asyncio

        await _seed(db, "poly_0xabc", "polymarket", "0xabc")
        rates = iter([0.05, 0.03])
        md, calls = _live(
            lambda r: httpx.Response(200, json={"fd": {"r": next(rates), "e": 1}})
        )
        md._ttl_s = 0.0
        first = await md.fee_params(db, "poly_0xabc")
        stale = await md.fee_params(db, "poly_0xabc")
        assert first == stale == polymarket_params(0.05, 1.0)
        await asyncio.gather(*md._refreshing.values())
        assert len(calls) == 2
        assert md._fee_cache["poly_0xabc"][1] == polymarket_params(0.03, 1.0)

    async def test_kalshi_event_and_series_cached_across_markets(self, db):
        await _seed(db, "kal_KXTEST-1", "kalshi", "KXTEST-1")
        await _seed(db, "kal_KXTEST-2", "kalshi", "KXTEST-2")

        def handler(request):
            path = request.url.path
            if "/markets/KXTEST-" in path:
                return httpx.Response(200, json={"market": {"event_ticker": "KXTEST"}})
            if path.endswith("/events/KXTEST"):
                return httpx.Response(200, json={"event": {"series_ticker": "KXS"}})
            if path.endswith("/series/KXS"):
                return httpx.Response(
                    200, json={"series": {"fee_type": "quadratic", "fee_multiplier": 1}}
                )
            return httpx.Response(404)

        md, calls = _live(handler)
        await md.fee_params(db, "kal_KXTEST-1")
        await md.fee_params(db, "kal_KXTEST-2")
        assert sum(p.endswith("/events/KXTEST") for p in calls) == 1
        assert sum(p.endswith("/series/KXS") for p in calls) == 1

    async def test_prefetch_warms_cache(self, db):
        await _seed(db, "poly_0xa", "polymarket", "0xa")
        await _seed(db, "poly_0xb", "polymarket", "0xb")
        md, calls = _live(lambda r: httpx.Response(200, json={"fd": None}))
        await md.prefetch_fees(db, ["poly_0xa", "poly_0xb", "poly_missing"])
        assert set(md._fee_cache) == {"poly_0xa", "poly_0xb"}
        await md.fee_params(db, "poly_0xa")
        assert len(calls) == 2
