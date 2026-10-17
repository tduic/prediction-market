"""KalshiExecutionClient against Kalshi's documented V2 order API.

Docs (checked 2026-10-17): POST /portfolio/events/orders (CreateOrderV2) takes
ticker, side bid|ask, count as a fixed-point string, price as a dollar string,
time_in_force; the 201 response carries fill_count, remaining_count,
average_fill_price and average_fee_paid. Cancel is
DELETE /portfolio/events/orders/{order_id}?market_ticker=...
"""

import base64
import json
from datetime import datetime, timezone

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from execution.clients.kalshi import KalshiExecutionClient
from execution.models import OrderLeg

BASE = "https://kalshi.test/trade-api/v2"


@pytest.fixture
def rsa_key(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "kalshi.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return key, str(path)


def _verify_signature(key, request: httpx.Request, expected_path: str) -> None:
    ts = request.headers["KALSHI-ACCESS-TIMESTAMP"]
    signature = base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(
        signature,
        (ts + request.method + expected_path).encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256(),
    )


async def _seed(db, market_id="kal_KXTEST-1", ticker="KXTEST-1"):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO markets (id, platform, platform_id, title, created_at, updated_at)"
        " VALUES (?, 'kalshi', ?, 't', ?, ?)",
        (market_id, ticker, now, now),
    )
    await db.commit()


def _client(db, rsa_key, handler):
    seen: list[httpx.Request] = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    client = KalshiExecutionClient(
        db, api_key="key-id", rsa_key_path=rsa_key[1], api_base=BASE
    )
    client.http_client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return client, seen


def _leg(side="BUY", price=0.55, size=10.0, market_id="kal_KXTEST-1"):
    return OrderLeg(
        market_id=market_id,
        platform="kalshi",
        side=side,
        size=size,
        limit_price=price,
        order_type="LIMIT",
    )


def _created(fill="10.00", remaining="0.00", price="0.5500", fee="0.0173"):
    body = {
        "order_id": "o-1",
        "fill_count": fill,
        "remaining_count": remaining,
        "ts_ms": 1,
    }
    if float(fill) > 0:
        body |= {"average_fill_price": price, "average_fee_paid": fee}
    return httpx.Response(201, json=body)


async def test_full_fill_uses_v2_endpoint_ticker_and_reported_fee(db, rsa_key):
    await _seed(db)
    client, seen = _client(db, rsa_key, lambda r: _created())
    result = await client.submit_order(_leg())

    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/trade-api/v2/portfolio/events/orders"
    _verify_signature(rsa_key[0], request, "/trade-api/v2/portfolio/events/orders")
    body = json.loads(request.content)
    assert body["ticker"] == "KXTEST-1"  # exchange ticker, never "kal_..."
    assert body["side"] == "bid"
    assert body["count"] == "10"
    assert body["price"] == "0.5500"
    assert body["time_in_force"] == "immediate_or_cancel"
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    assert body["client_order_id"]

    assert result.status == "filled"
    assert result.order_id == "o-1"
    assert result.filled_size == 10.0
    assert result.filled_price == 0.55
    assert result.fee_paid == pytest.approx(0.173)


async def test_sell_is_ask_and_prices_round_conservatively(db, rsa_key):
    await _seed(db)
    client, seen = _client(db, rsa_key, lambda r: _created())
    await client.submit_order(_leg(side="SELL", price=0.553))
    await client.submit_order(_leg(side="BUY", price=0.557))
    sell, buy = (json.loads(r.content) for r in seen)
    assert (sell["side"], sell["price"]) == ("ask", "0.5600")  # never sell lower
    assert (buy["side"], buy["price"]) == ("bid", "0.5500")  # never pay more


async def test_partial_fill(db, rsa_key):
    await _seed(db)
    client, _ = _client(db, rsa_key, lambda r: _created(fill="4.00", remaining="6.00"))
    result = await client.submit_order(_leg())
    assert result.status == "partially_filled"
    assert result.filled_size == 4.0


async def test_no_fill_is_failed(db, rsa_key):
    await _seed(db)
    client, _ = _client(db, rsa_key, lambda r: _created(fill="0.00", remaining="10.00"))
    result = await client.submit_order(_leg())
    assert result.status == "failed"
    assert result.filled_price is None


async def test_4xx_is_failed(db, rsa_key):
    await _seed(db)
    client, _ = _client(
        db,
        rsa_key,
        lambda r: httpx.Response(400, json={"code": "invalid_price", "message": "x"}),
    )
    assert (await client.submit_order(_leg())).status == "failed"


async def test_5xx_is_unknown(db, rsa_key):
    await _seed(db)
    client, _ = _client(db, rsa_key, lambda r: httpx.Response(503))
    result = await client.submit_order(_leg())
    assert result.status == "pending"


async def test_timeout_is_unknown_and_keeps_client_order_id(db, rsa_key):
    await _seed(db)

    def boom(request):
        raise httpx.ReadTimeout("timed out", request=request)

    client, seen = _client(db, rsa_key, boom)
    result = await client.submit_order(_leg())
    assert result.status == "pending"
    client_order_id = json.loads(seen[0].content)["client_order_id"]
    assert client_order_id in result.order_id


async def test_missing_limit_price_is_rejected_without_a_request(db, rsa_key):
    await _seed(db)
    client, seen = _client(db, rsa_key, lambda r: _created())
    leg = OrderLeg(market_id="kal_KXTEST-1", platform="kalshi", side="BUY", size=1)
    result = await client.submit_order(leg)
    assert result.status == "failed"
    assert seen == []


async def test_ticker_falls_back_to_stripping_prefix(db, rsa_key):
    client, seen = _client(db, rsa_key, lambda r: _created())
    await client.submit_order(_leg(market_id="kal_KXOTHER-2"))
    assert json.loads(seen[0].content)["ticker"] == "KXOTHER-2"


async def test_cancel_uses_v2_path_and_signs_without_query(db, rsa_key):
    await _seed(db)
    client, seen = _client(
        db,
        rsa_key,
        lambda r: (
            _created()
            if r.method == "POST"
            else httpx.Response(
                200, json={"order_id": "o-1", "reduced_by": "0.00", "ts_ms": 1}
            )
        ),
    )
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO signals (id, strategy, signal_type, market_id_a, model_edge,"
        " kelly_fraction, position_size_a, total_capital_at_risk, status,"
        " fired_at, updated_at) VALUES ('sig-1', 'P1_cross_market_arb', 'arb_pair',"
        " 'kal_KXTEST-1', 0.05, 0.1, 10, 5, 'fired', ?, ?)",
        (now, now),
    )
    await db.commit()
    await client.submit_order(_leg(), signal_id="sig-1")
    assert await client.cancel_order("o-1") is True
    request = seen[-1]
    assert request.method == "DELETE"
    assert request.url.path == "/trade-api/v2/portfolio/events/orders/o-1"
    assert request.url.params["market_ticker"] == "KXTEST-1"
    _verify_signature(rsa_key[0], request, "/trade-api/v2/portfolio/events/orders/o-1")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, content=b"not json"),
        httpx.Response(201, json={"order_id": "o-9", "fill_count": "5.00"}),  # no price
        httpx.Response(201, json={"order_id": "o-9", "fill_count": "abc"}),
        httpx.Response(201, json={"order_id": "o-9"}),  # required fill_count missing
        httpx.Response(201, json={"order_id": "o-9", "fill_count": None}),
        httpx.Response(201, json={"order_id": "o-9", "fill_count": "nan"}),
        httpx.Response(
            201,
            json={
                "order_id": "o-9",
                "fill_count": "5.00",
                "average_fill_price": "0.55",
                "average_fee_paid": "n/a",
            },
        ),
    ],
)
async def test_unparsable_acceptance_is_unknown_not_an_exception(db, rsa_key, response):
    # The exchange accepted the order; if we can't read what filled, the
    # fill state is unknown and the engine must halt.
    await _seed(db)
    client, _ = _client(db, rsa_key, lambda r: response)
    result = await client.submit_order(_leg())
    assert result.status == "pending"


@pytest.mark.parametrize("status", [408, 409])
async def test_ambiguous_4xx_is_unknown(db, rsa_key, status):
    await _seed(db)
    client, _ = _client(db, rsa_key, lambda r: httpx.Response(status))
    assert (await client.submit_order(_leg())).status == "pending"


async def test_count_is_floored_to_whole_contracts(db, rsa_key):
    await _seed(db)
    client, seen = _client(db, rsa_key, lambda r: _created(fill="12.00"))
    await client.submit_order(_leg(size=12.9))
    assert json.loads(seen[0].content)["count"] == "12"


async def test_sub_one_contract_is_rejected_without_a_request(db, rsa_key):
    await _seed(db)
    client, seen = _client(db, rsa_key, lambda r: _created())
    result = await client.submit_order(_leg(size=0.6))
    assert result.status == "failed"
    assert seen == []
