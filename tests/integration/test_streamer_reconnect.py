"""Websocket reconnect behaviour (offline, websockets.connect is faked).

- A server that closes normally ends ``async for`` without an exception; the
  loop used to reconnect immediately with no sleep (a hot spin against the
  exchange). Reconnects must back off exponentially with jitter, and the
  backoff resets once messages flow again.
- The Kalshi stream loaded its RSA key before the retry loop, so a missing or
  bad key raised out of the task (swallowed by gather) and the feed died for
  good with no log. It must log loudly and keep retrying.
"""

import asyncio
import json
import logging

import pytest
import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from core.ingestor import streamer
from core.ingestor.streamer import stream_prices_kalshi, stream_prices_polymarket


class FakeWS:
    def __init__(self, messages: list[str]):
        self._messages = list(messages)
        self.sent: list[str] = []

    async def send(self, msg: str) -> None:
        self.sent.append(msg)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        await asyncio.sleep(0)
        if not self._messages:
            raise StopAsyncIteration  # normal close
        return self._messages.pop(0)


class FakeConnect:
    """Stands in for websockets.connect; each call serves one script entry."""

    def __init__(self, scripts: list[list[str] | Exception]):
        self.scripts = list(scripts)
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        script = self.scripts.pop(0) if self.scripts else []
        outer = self

        class _CM:
            async def __aenter__(self_inner):
                if isinstance(script, Exception):
                    raise script
                return FakeWS(script)

            async def __aexit__(self_inner, *exc):
                return False

        del outer
        return _CM()


@pytest.fixture
def sleeps(monkeypatch):
    """Record reconnect delays; stop the stream after `limit` reconnects."""
    state: dict = {"delays": [], "stop": None, "limit": 6}

    async def fake_wait(stop_event: asyncio.Event, delay: float) -> None:
        state["delays"].append(delay)
        if len(state["delays"]) >= state["limit"]:
            stop_event.set()
        await asyncio.sleep(0)

    monkeypatch.setattr(streamer, "_sleep_unless_stopped", fake_wait)
    monkeypatch.setattr(streamer, "_jitter", lambda: 1.0)
    return state


PRICE_MSG = json.dumps(
    {
        "event_type": "price_change",
        "price_changes": [{"asset_id": "tok", "best_bid": "0.4", "best_ask": "0.42"}],
    }
)


async def _noop(market_id: str, price: float) -> None:
    return None


class TestPolymarketReconnect:
    async def test_normal_close_backs_off_exponentially(self, monkeypatch, sleeps):
        fake = FakeConnect([[] for _ in range(10)])  # every connection closes at once
        monkeypatch.setattr(websockets, "connect", fake)
        stop = asyncio.Event()
        await asyncio.wait_for(
            stream_prices_polymarket(["tok"], stop, _noop, {"tok": "poly_x"}), 5
        )
        d = sleeps["delays"]
        assert len(d) == sleeps["limit"]
        assert all(x > 0 for x in d), "reconnect without sleeping (hot spin)"
        assert d == sorted(d) and d[-1] > d[0]
        assert max(d) <= streamer._BACKOFF_MAX_S

    async def test_backoff_resets_after_messages(self, monkeypatch, sleeps):
        fake = FakeConnect([[], [], [], [PRICE_MSG], [], []])
        monkeypatch.setattr(websockets, "connect", fake)
        got: list[tuple[str, float]] = []

        async def on_price(mid: str, p: float) -> None:
            got.append((mid, p))

        sleeps["limit"] = 5
        stop = asyncio.Event()
        await asyncio.wait_for(
            stream_prices_polymarket(["tok"], stop, on_price, {"tok": "poly_x"}), 5
        )
        d = sleeps["delays"]
        assert got == [("poly_x", pytest.approx(0.41))]
        # three growing delays, then reset to the base after a good session
        assert d[0] < d[1] < d[2]
        assert d[3] == d[0]

    async def test_connect_errors_back_off_too(self, monkeypatch, sleeps):
        fake = FakeConnect([OSError("refused") for _ in range(10)])
        monkeypatch.setattr(websockets, "connect", fake)
        stop = asyncio.Event()
        await asyncio.wait_for(stream_prices_polymarket(["tok"], stop, _noop), 5)
        d = sleeps["delays"]
        assert d == sorted(d) and d[-1] > d[0]


def _write_key(path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


class TestKalshiReconnect:
    async def test_bad_key_logs_loudly_and_keeps_retrying(
        self, monkeypatch, sleeps, tmp_path, caplog
    ):
        bad = tmp_path / "kalshi.pem"
        bad.write_text("not a pem")
        fake = FakeConnect([[] for _ in range(10)])
        monkeypatch.setattr(websockets, "connect", fake)
        sleeps["limit"] = 3
        stop = asyncio.Event()
        with caplog.at_level(logging.ERROR, logger="core.ingestor.streamer"):
            await asyncio.wait_for(
                stream_prices_kalshi(["T"], stop, _noop, "key", str(bad)), 5
            )
        # Did not raise; retried with backoff; never connected without a key.
        assert len(sleeps["delays"]) == 3
        assert fake.calls == 0
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors and "key" in errors[0].getMessage().lower()

    async def test_key_fixed_later_recovers(self, monkeypatch, sleeps, tmp_path):
        keyfile = tmp_path / "kalshi.pem"  # missing at first
        fake = FakeConnect([[] for _ in range(10)])
        monkeypatch.setattr(websockets, "connect", fake)

        async def fake_wait(stop_event, delay):
            sleeps["delays"].append(delay)
            if len(sleeps["delays"]) == 2:
                _write_key(keyfile)
            if fake.calls >= 1:
                stop_event.set()

        monkeypatch.setattr(streamer, "_sleep_unless_stopped", fake_wait)
        stop = asyncio.Event()
        await asyncio.wait_for(
            stream_prices_kalshi(["T"], stop, _noop, "key", str(keyfile)), 5
        )
        assert fake.calls >= 1

    async def test_normal_close_backs_off(self, monkeypatch, sleeps, tmp_path):
        keyfile = tmp_path / "kalshi.pem"
        _write_key(keyfile)
        fake = FakeConnect([[] for _ in range(10)])
        monkeypatch.setattr(websockets, "connect", fake)
        stop = asyncio.Event()
        await asyncio.wait_for(
            stream_prices_kalshi(["T"], stop, _noop, "key", str(keyfile)), 5
        )
        d = sleeps["delays"]
        assert all(x > 0 for x in d) and d[-1] > d[0]
