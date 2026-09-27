"""PolymarketExecutionClientV2 against a fake py-clob-client-v2 SDK client."""

from datetime import datetime, timezone

import httpx
import pytest
from py_clob_client_v2.exceptions import PolyApiException

from core.engine.fees import polymarket_params, taker_fee
from execution.clients.polymarket_v2 import PolymarketExecutionClientV2
from execution.market_data import StaticMarketData, set_market_data
from execution.models import OrderLeg


class FakeClob:
    """Scripted stand-in for py_clob_client_v2.ClobClient (sync methods)."""

    def __init__(self, post=None, orders=None, post_exc=None, get_exc=None):
        self._post = post or {"success": True, "orderID": "0xorder"}
        self._orders = list(orders or [])
        self._post_exc = post_exc
        self._get_exc = get_exc
        self.posted: list[tuple] = []
        self.cancelled: list[str] = []

    def get_tick_size(self, token_id):
        return "0.01"

    def create_and_post_order(self, order_args, options, order_type):
        self.posted.append((order_args, options, order_type))
        if self._post_exc is not None:
            raise self._post_exc
        return self._post

    def get_order(self, order_id):
        if self._get_exc is not None:
            raise self._get_exc
        return self._orders.pop(0) if len(self._orders) > 1 else self._orders[0]

    def cancel_order(self, payload):
        self.cancelled.append(payload.orderID)
        return {"canceled": [payload.orderID]}


def _order(status, matched, price="0.40"):
    return {"status": status, "size_matched": str(matched), "price": price}


async def _seed(db, market_id="poly_0xabc"):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO markets (id, platform, platform_id, title, yes_token_id,"
        " no_token_id, created_at, updated_at)"
        " VALUES (?, 'polymarket', '0xabc', 't', 'tok_yes', 'tok_no', ?, ?)",
        (market_id, now, now),
    )
    await db.commit()


def _client(db, fake: FakeClob) -> PolymarketExecutionClientV2:
    client = PolymarketExecutionClientV2(
        db, private_key="0x" + "1" * 64, poll_interval_s=0, max_polls=3
    )
    client._client = fake
    client._initialized = True
    return client


def _leg(side="BUY", price=0.40, size=10.0, order_type="LIMIT"):
    return OrderLeg(
        market_id="poly_0xabc",
        platform="polymarket",
        side=side,
        size=size,
        limit_price=price,
        order_type=order_type,
    )


async def test_full_fill_is_fak_and_charges_curve_fee(db):
    await _seed(db)
    fake = FakeClob(orders=[_order("MATCHED", 10)])
    result = await _client(db, fake).submit_order(_leg())
    assert result.status == "filled"
    assert result.filled_size == 10.0 and result.filled_price == 0.40
    assert result.book == "YES"
    assert result.fee_paid == taker_fee(polymarket_params(0.03, 1.0), 10.0, 0.40)
    args, options, order_type = fake.posted[0]
    assert (args.token_id, args.side, args.price, args.size) == (
        "tok_yes",
        "BUY",
        0.40,
        10.0,
    )
    assert options.tick_size == "0.01"
    assert order_type == "FAK"


async def test_partial_fill_then_kill(db):
    await _seed(db)
    fake = FakeClob(orders=[_order("CANCELED", 4)])
    result = await _client(db, fake).submit_order(_leg())
    assert result.status == "partially_filled"
    assert result.filled_size == 4.0


async def test_translated_sell_reports_no_book(db):
    await _seed(db)  # no YES inventory → SELL YES @0.60 becomes BUY NO @0.40
    fake = FakeClob(orders=[_order("MATCHED", 10, price="0.40")])
    result = await _client(db, fake).submit_order(_leg(side="SELL", price=0.60))
    args = fake.posted[0][0]
    assert (args.token_id, args.side, args.price) == ("tok_no", "BUY", 0.40)
    assert result.book == "NO"
    assert result.filled_price == 0.40  # NO space; the engine converts


async def test_market_order_is_fok(db):
    await _seed(db)
    fake = FakeClob(orders=[_order("MATCHED", 10)])
    await _client(db, fake).submit_order(_leg(order_type="MARKET"))
    assert fake.posted[0][2] == "FOK"


async def test_rejected_response_is_failed(db):
    await _seed(db)
    fake = FakeClob(post={"success": False, "errorMsg": "not enough balance"})
    result = await _client(db, fake).submit_order(_leg())
    assert result.status == "failed"
    assert "not enough balance" in result.error_message


async def test_4xx_on_post_is_failed(db):
    await _seed(db)
    exc = PolyApiException(resp=httpx.Response(400, json={"error": "invalid"}))
    result = await _client(db, FakeClob(post_exc=exc)).submit_order(_leg())
    assert result.status == "failed"


@pytest.mark.parametrize(
    "exc",
    [
        PolyApiException(error_msg="Request exception!"),  # transport failure
        PolyApiException(resp=httpx.Response(503)),
        TimeoutError("read timed out"),
    ],
)
async def test_ambiguous_post_failure_is_unknown(db, exc):
    await _seed(db)
    result = await _client(db, FakeClob(post_exc=exc)).submit_order(_leg())
    assert result.status == "pending"  # may have reached the exchange


async def test_no_fill_is_failed(db):
    await _seed(db)
    result = await _client(db, FakeClob(orders=[_order("UNMATCHED", 0)])).submit_order(
        _leg()
    )
    assert result.status == "failed"


async def test_poll_errors_end_pending_after_cancel_attempt(db):
    await _seed(db)
    fake = FakeClob(get_exc=RuntimeError("503"))
    result = await _client(db, fake).submit_order(_leg())
    assert result.status == "pending"
    assert fake.cancelled == ["0xorder"]


async def test_timeout_then_cancel_reveals_late_partial_fill(db):
    await _seed(db)
    fake = FakeClob(
        orders=[_order("LIVE", 0), _order("LIVE", 0), _order("LIVE", 0)]
        + [_order("CANCELED", 3)]
    )
    result = await _client(db, fake).submit_order(_leg())
    assert fake.cancelled == ["0xorder"]
    assert result.status == "partially_filled"
    assert result.filled_size == 3.0


async def test_unknown_fee_keeps_the_fill(db):
    class _NoFees(StaticMarketData):
        async def fee_params(self, db, market_id):
            return None

    set_market_data(_NoFees())
    await _seed(db)
    result = await _client(db, FakeClob(orders=[_order("MATCHED", 10)])).submit_order(
        _leg()
    )
    assert result.status == "filled"
    assert result.fee_paid is None
    assert "fee unverified" in result.error_message


async def test_unresolvable_market_never_reaches_sdk(db):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO markets (id, platform, platform_id, title, created_at, updated_at)"
        " VALUES ('poly_0xabc', 'polymarket', '0xabc', 't', ?, ?)",
        (now, now),
    )
    await db.commit()
    fake = FakeClob()
    result = await _client(db, fake).submit_order(_leg())
    assert result.status == "failed"
    assert fake.posted == []


def test_proxy_swaps_sdk_http_client(monkeypatch):
    from py_clob_client_v2.http_helpers import helpers

    original = helpers._http_client
    try:
        client = PolymarketExecutionClientV2(
            None, private_key="0x" + "1" * 64, proxy_url="socks5://10.0.0.1:1080"
        )
        client._install_proxy()
        assert helpers._http_client is not original
    finally:
        helpers._http_client = original


async def test_repeated_failures_get_distinct_order_ids(db):
    await _seed(db)
    fake = FakeClob(post={"success": False, "errorMsg": "not enough balance"})
    client = _client(db, fake)
    first = await client.submit_order(_leg())
    second = await client.submit_order(_leg())
    # orders.id is the primary key: a collision would drop the audit row.
    assert first.order_id != second.order_id


def test_concurrent_init_builds_one_client(monkeypatch):
    import threading
    import time

    import py_clob_client_v2

    built: list[int] = []
    gate = threading.Barrier(2)

    class _Clob:
        def __init__(self, **kwargs):
            built.append(1)
            time.sleep(0.05)  # widen the race window

        def create_or_derive_api_key(self):
            return None

    monkeypatch.setattr(py_clob_client_v2, "ClobClient", _Clob)
    client = PolymarketExecutionClientV2(None, private_key="0x" + "1" * 64)

    def _init():
        gate.wait()
        client._ensure_client()

    threads = [threading.Thread(target=_init) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 2  # one L1 client for key derivation + one L2 client
