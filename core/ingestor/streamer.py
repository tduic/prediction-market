"""
Websocket price streaming for Polymarket and Kalshi.

Provides stream_prices_polymarket and stream_prices_kalshi which connect to
exchange websockets and call an on_price callback for each price update.
"""

import asyncio
import json
import logging
import random
import time
import typing
from pathlib import Path

logger = logging.getLogger(__name__)

_BACKOFF_BASE_S = 1.0
_BACKOFF_MAX_S = 60.0


def _jitter() -> float:
    """Multiplier in [0.5, 1.0) so many connections don't reconnect in lockstep."""
    return 0.5 + random.random() / 2


class _Backoff:
    """Exponential reconnect delay with jitter; reset once data flows."""

    def __init__(self) -> None:
        self.failures = 0

    def reset(self) -> None:
        self.failures = 0

    def next_delay(self) -> float:
        raw = min(_BACKOFF_BASE_S * 2**self.failures, _BACKOFF_MAX_S)
        self.failures += 1
        return raw * _jitter()


async def _sleep_unless_stopped(stop_event: asyncio.Event, delay: float) -> None:
    """Sleep up to ``delay`` seconds, returning early when stop is requested."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        pass


def _to_price(value: typing.Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def _mid(bid: float | None, ask: float | None) -> float | None:
    """Mid of a two-sided, uncrossed book inside (0, 1); else None."""
    if bid is None or ask is None:
        return None
    if bid <= 0 or ask <= 0 or bid >= 1 or ask >= 1 or bid > ask:
        return None
    return (bid + ask) / 2.0


def parse_polymarket_ws_message(
    data: typing.Any, token_map: typing.Mapping[str, str]
) -> dict[str, float]:
    """Extract ``{internal_market_id: mid}`` from one CLOB market-channel frame.

    ``token_map`` maps a subscribed CLOB token id (the market's YES
    ``clobTokenIds[0]``) to our internal ``poly_<conditionId>`` id. Only
    tokens in the map are emitted: a ``price_change`` frame also carries the
    complementary NO token, which must not leak onto the YES stream.

    Current wire format (verified live):
      - ``price_change``: top level ``{market, price_changes, timestamp,
        event_type}``; each ``price_changes[]`` entry has ``asset_id``,
        ``best_bid``, ``best_ask``.
      - ``book``: ``{asset_id, bids, asks, ...}``; levels are unsorted for our
        purposes (bids ascend, asks descend), so take max bid / min ask.
      - ``last_trade_price``: a trade print, ignored here — this is a quote
        (mid) stream and mixing trade prints into it creates false spreads.

    When several updates for the same market occur in one frame, the last
    one wins.
    """
    out: dict[str, float] = {}
    events = data if isinstance(data, list) else [data]
    for evt in events:
        if not isinstance(evt, dict):
            continue
        etype = evt.get("event_type")
        if etype == "price_change":
            changes = evt.get("price_changes")
            if not isinstance(changes, list):
                # Legacy shape: the change fields sat at the top level.
                changes = [evt]
            for c in changes:
                if not isinstance(c, dict):
                    continue
                market_id = token_map.get(str(c.get("asset_id", "")))
                if market_id is None:
                    continue
                mid = _mid(_to_price(c.get("best_bid")), _to_price(c.get("best_ask")))
                if mid is not None:
                    out[market_id] = mid
        elif etype == "book":
            market_id = token_map.get(str(evt.get("asset_id", "")))
            if market_id is None:
                continue
            bids = [
                p
                for p in (_to_price(lvl.get("price")) for lvl in evt.get("bids") or [])
                if p is not None
            ]
            asks = [
                p
                for p in (_to_price(lvl.get("price")) for lvl in evt.get("asks") or [])
                if p is not None
            ]
            mid = _mid(max(bids) if bids else None, min(asks) if asks else None)
            if mid is not None:
                out[market_id] = mid
    return out


async def polymarket_subscriptions(
    db: typing.Any, internal_ids: typing.Iterable[str]
) -> tuple[list[str], dict[str, str]]:
    """Resolve internal ``poly_*`` ids to CLOB YES token ids for the WS feed.

    The CLOB market channel is keyed by token id (``clobTokenIds``), not by
    conditionId — subscribing with ``platform_id`` silently yields no data.
    Returns ``(token_ids, token_map)`` where ``token_map`` maps each YES token
    back to its internal market id. Markets without a stored
    ``yes_token_id`` are skipped with a warning (they cannot be streamed).
    """
    tokens: list[str] = []
    token_map: dict[str, str] = {}
    missing: list[str] = []
    for pid in internal_ids:
        cursor = await db.execute(
            "SELECT yes_token_id FROM markets WHERE id = ?", (pid,)
        )
        row = await cursor.fetchone()
        token = row[0] if row else None
        if not token:
            missing.append(pid)
            continue
        token = str(token)
        if token not in token_map:
            tokens.append(token)
        token_map[token] = pid
    if missing:
        logger.warning(
            "Polymarket WS: %d matched markets have no yes_token_id and will "
            "get no live prices (e.g. %s) — re-run the market refresh",
            len(missing),
            ", ".join(missing[:3]),
        )
    return tokens, token_map


async def stream_prices_polymarket(
    asset_ids: list[str],
    stop_event: asyncio.Event,
    on_price: typing.Callable[[str, float], typing.Awaitable[None]],
    id_map: dict[str, str] | None = None,
):
    """Stream real-time prices from Polymarket CLOB websocket.

    Polymarket limit: 500 assets per connection, so we chunk.
    Calls `on_price(market_id, mid)` for every quote update — this is
    how the ArbitrageEngine gets triggered on each tick.

    asset_ids: CLOB **token ids** (markets.yes_token_id), not conditionIds;
      see ``polymarket_subscriptions``.
    id_map: token id -> internal market_id (poly_<conditionId>). Tokens not in
      the map are emitted under the token id itself.
    """
    import websockets

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    CHUNK_SIZE = 450

    bad = [a for a in asset_ids if str(a).startswith("0x")]
    if bad:
        logger.error(
            "Polymarket WS: %d/%d subscription ids look like conditionIds "
            "(0x...) — the market channel needs CLOB token ids and will send "
            "nothing for these",
            len(bad),
            len(asset_ids),
        )

    token_map: dict[str, str] = {a: (id_map or {}).get(a, a) for a in asset_ids}

    async def _connect_chunk(chunk_ids: list[str]):
        backoff = _Backoff()
        while not stop_event.is_set():
            reason = "server closed the connection"
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
                        backoff.reset()
                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        for market_id, price in parse_polymarket_ws_message(
                            data, token_map
                        ).items():
                            await on_price(market_id, price)
            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
            if stop_event.is_set():
                break
            # Always pause before reconnecting — a normal close ends the
            # `async for` without an exception and used to reconnect in a
            # tight loop.
            delay = backoff.next_delay()
            logger.warning(
                "Polymarket WS disconnected (%s); reconnecting in %.1fs",
                reason,
                delay,
            )
            await _sleep_unless_stopped(stop_event, delay)

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
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    def load_key() -> RSAPrivateKey:
        key_path = Path(rsa_key_path).expanduser()
        loaded_key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
        if not isinstance(loaded_key, RSAPrivateKey):
            raise TypeError(
                f"Kalshi key at {key_path} is not an RSA private key "
                f"(got {type(loaded_key).__name__})"
            )
        return loaded_key

    def sign(private_key: RSAPrivateKey, method: str, path: str) -> dict:
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

    private_key: RSAPrivateKey | None = None
    backoff = _Backoff()
    while not stop_event.is_set():
        reason = "server closed the connection"
        if private_key is None:
            # Loaded inside the loop: a missing/bad key used to raise out of
            # the task before the retry loop and kill the feed silently.
            try:
                private_key = load_key()
            except Exception as e:
                delay = backoff.next_delay()
                logger.error(
                    "Kalshi WS: cannot load RSA key from %s (%s: %s) — NO Kalshi "
                    "prices until fixed; retrying in %.1fs",
                    rsa_key_path,
                    type(e).__name__,
                    e,
                    delay,
                )
                await _sleep_unless_stopped(stop_event, delay)
                continue
        try:
            headers = sign(private_key, "GET", "/trade-api/ws/v2")
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
                    backoff.reset()
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
            reason = f"{type(e).__name__}: {e}"
        if stop_event.is_set():
            break
        delay = backoff.next_delay()
        logger.warning(
            "Kalshi WS disconnected (%s); reconnecting in %.1fs", reason, delay
        )
        await _sleep_unless_stopped(stop_event, delay)
