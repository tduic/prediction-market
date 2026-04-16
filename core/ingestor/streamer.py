"""
Websocket price streaming for Polymarket and Kalshi.

Provides stream_prices_polymarket and stream_prices_kalshi which connect to
exchange websockets and call an on_price callback for each price update.
"""

import asyncio
import json
import logging
import time
import typing
from pathlib import Path

logger = logging.getLogger(__name__)


async def stream_prices_polymarket(
    asset_ids: list[str],
    stop_event: asyncio.Event,
    on_price: typing.Callable[[str, float], typing.Awaitable[None]],
    id_map: dict[str, str] | None = None,
):
    """Stream real-time prices from Polymarket CLOB websocket.

    Polymarket limit: 500 assets per connection, so we chunk.
    Calls `on_price(market_id, price)` for every update — this is
    how the ArbitrageEngine gets triggered on each tick.

    id_map: optional mapping from platform_id (asset_id) -> internal market_id (poly_XXX)
    """
    import websockets

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    CHUNK_SIZE = 450

    async def _connect_chunk(chunk_ids: list[str]):
        while not stop_event.is_set():
            try:
                async with websockets.connect(WS_URL, ping_interval=10) as ws:
                    sub_msg = json.dumps({"assets_ids": chunk_ids, "type": "market"})
                    await ws.send(sub_msg)
                    logger.info(
                        "Polymarket WS: subscribed to %d assets", len(chunk_ids)
                    )

                    async for raw in ws:
                        if stop_event.is_set():
                            break
                        try:
                            data = json.loads(raw)
                            for evt in data if isinstance(data, list) else [data]:
                                asset_id = evt.get("asset_id", "")
                                price = None

                                if evt.get("event_type") == "price_change":
                                    changes = evt.get("price_changes", [evt])
                                    for c in changes:
                                        bid = c.get("best_bid")
                                        ask = c.get("best_ask")
                                        if bid and ask:
                                            try:
                                                price = (float(bid) + float(ask)) / 2.0
                                            except (ValueError, TypeError):
                                                pass
                                elif "price" in evt and asset_id:
                                    try:
                                        price = float(evt["price"])
                                    except (ValueError, TypeError):
                                        pass

                                if price is not None and asset_id:
                                    market_id = (id_map or {}).get(asset_id, asset_id)
                                    await on_price(market_id, price)
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                if not stop_event.is_set():
                    logger.warning("Polymarket WS reconnecting: %s", e)
                    await asyncio.sleep(2)

    chunks = [
        asset_ids[i : i + CHUNK_SIZE] for i in range(0, len(asset_ids), CHUNK_SIZE)
    ]
    logger.info(
        "Polymarket WS: %d assets across %d connections",
        len(asset_ids),
        len(chunks),
    )
    tasks = [asyncio.create_task(_connect_chunk(c)) for c in chunks]
    await asyncio.gather(*tasks, return_exceptions=True)


async def stream_prices_kalshi(
    tickers: list[str],
    stop_event: asyncio.Event,
    on_price: typing.Callable[[str, float], typing.Awaitable[None]],
    api_key: str,
    rsa_key_path: str,
):
    """Stream real-time prices from Kalshi websocket.

    Calls `on_price(market_id, price)` for every ticker update.
    market_id is formatted as "kal_{ticker}" to match our internal IDs.
    """
    import base64

    import websockets
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    key_path = Path(rsa_key_path).expanduser()
    private_key = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None
    )

    def sign(method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode("utf-8")
        sig = private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    while not stop_event.is_set():
        try:
            headers = sign("GET", "/trade-api/ws/v2")
            async with websockets.connect(WS_URL, additional_headers=headers) as ws:
                for i, ticker in enumerate(tickers):
                    sub = json.dumps(
                        {
                            "id": i + 1,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["ticker"],
                                "market_ticker": ticker,
                            },
                        }
                    )
                    await ws.send(sub)

                logger.info("Kalshi WS: subscribed to %d tickers", len(tickers))

                async for raw in ws:
                    if stop_event.is_set():
                        break
                    try:
                        data = json.loads(raw)
                        if data.get("type") == "ticker":
                            msg = data.get("msg", {})
                            ticker = msg.get("market_ticker", "")
                            bid = msg.get("yes_bid_dollars")
                            ask = msg.get("yes_ask_dollars")
                            if ticker and bid and ask:
                                try:
                                    price = (float(bid) + float(ask)) / 2.0
                                    await on_price(f"kal_{ticker}", price)
                                except (ValueError, TypeError):
                                    pass
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            if not stop_event.is_set():
                logger.warning("Kalshi WS reconnecting: %s", e)
                await asyncio.sleep(2)
