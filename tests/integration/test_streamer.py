"""Websocket price streaming: payload parsing and subscription mapping.

Payload samples in tests/fixtures/polymarket_ws_messages.json were captured
live from the Polymarket CLOB market channel. They are the regression guard
for the current wire format:

- ``price_change`` frames carry ``asset_id`` / ``best_bid`` / ``best_ask``
  inside each ``price_changes[]`` entry (no top-level ``asset_id``), and one
  frame covers both the YES and the NO token of a market.
- ``book`` frames arrive as a list; bids are sorted ascending and asks
  descending, so the best level is the *last* one on each side.
- ``last_trade_price`` frames are trades, not quotes, and must not feed the
  mid-price stream.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core.ingestor.streamer import (
    parse_kalshi_ws_message,
    parse_polymarket_ws_message,
    polymarket_subscriptions,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "polymarket_ws_messages.json"
WS = json.loads(FIXTURE.read_text())

PC_COND = "0x1a01bf78f56a507fcb666d564d8c8b91b0750679163ed6e96746102c9b7d285d"
PC_YES = (
    "109876868437950584369987384406356259939519193117253465815665152916226511121427"
)
PC_NO = "38310927461258927563020508025396887688249787380193911540547071417200646834355"
BOOK_COND = "0x747dc809fb79e1b05be09c42d6179459a58de2ef3e40f02484a4e1260f741f75"
BOOK_YES = (
    "107505882767731489358349912513945399560393482969656700824895970500493757150417"
)
LTP_YES = (
    "83247781037352156539108067944461291821683755894607244160607042790356561625563"
)


class TestParsePolymarketWsMessage:
    def test_price_change_maps_yes_token_to_internal_id_with_mid(self):
        token_map = {PC_YES: f"poly_{PC_COND}"}
        out = parse_polymarket_ws_message(WS["price_change"], token_map)
        assert out == {f"poly_{PC_COND}": (0.768 + 0.769) / 2}

    def test_price_change_ignores_unsubscribed_no_token(self):
        # The same frame carries the NO token's change; it must not be
        # emitted under any id (it would be a 1-p price on the YES stream).
        out = parse_polymarket_ws_message(WS["price_change"], {PC_YES: "poly_x"})
        assert list(out) == ["poly_x"]
        assert abs(out["poly_x"] - 0.7685) < 1e-9

    def test_price_change_not_matched_by_condition_id(self):
        # Subscribing/mapping by conditionId (the old behaviour) yields nothing.
        out = parse_polymarket_ws_message(
            WS["price_change"], {PC_COND: f"poly_{PC_COND}"}
        )
        assert out == {}

    def test_book_snapshot_uses_best_bid_and_best_ask(self):
        out = parse_polymarket_ws_message(WS["book"], {BOOK_YES: "poly_b"})
        # best bid = max(bids) = 0.048, best ask = min(asks) = 0.049
        assert abs(out["poly_b"] - 0.0485) < 1e-9

    def test_last_trade_price_is_not_a_quote(self):
        out = parse_polymarket_ws_message(WS["last_trade_price"], {LTP_YES: "poly_l"})
        assert out == {}

    def test_multiple_changes_for_same_token_keep_latest(self):
        frame = {
            "event_type": "price_change",
            "market": "0xabc",
            "price_changes": [
                {"asset_id": "1", "best_bid": "0.40", "best_ask": "0.42"},
                {"asset_id": "2", "best_bid": "0.10", "best_ask": "0.12"},
                {"asset_id": "1", "best_bid": "0.44", "best_ask": "0.46"},
            ],
        }
        out = parse_polymarket_ws_message(frame, {"1": "poly_1", "2": "poly_2"})
        assert out == {"poly_1": 0.45, "poly_2": 0.11}

    def test_one_sided_or_crossed_book_is_skipped(self):
        frame = {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "1", "best_bid": "0", "best_ask": "0.42"},
                {"asset_id": "2", "best_bid": "0.50", "best_ask": "0.40"},
                {"asset_id": "3", "best_bid": "", "best_ask": "0.40"},
            ],
        }
        out = parse_polymarket_ws_message(frame, {"1": "a", "2": "b", "3": "c"})
        assert out == {}

    def test_garbage_frames_do_not_raise(self):
        for frame in (None, "PONG", 3, [], [None], {"event_type": "book"}):
            assert parse_polymarket_ws_message(frame, {"1": "a"}) == {}


class TestPolymarketSubscriptions:
    async def test_subscribes_yes_tokens_and_maps_back_to_internal_id(self, db):
        now = datetime.now(UTC).isoformat()
        await db.executemany(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES (?, 'polymarket', ?, ?, ?, ?, 'open', ?, ?)""",
            [
                (f"poly_{PC_COND}", PC_COND, "A", PC_YES, PC_NO, now, now),
                (f"poly_{BOOK_COND}", BOOK_COND, "B", BOOK_YES, None, now, now),
                ("poly_0xnotoken", "0xnotoken", "C", None, None, now, now),
            ],
        )
        await db.commit()

        tokens, token_map = await polymarket_subscriptions(
            db, [f"poly_{PC_COND}", f"poly_{BOOK_COND}", "poly_0xnotoken"]
        )

        assert sorted(tokens) == sorted([PC_YES, BOOK_YES])
        assert token_map == {
            PC_YES: f"poly_{PC_COND}",
            BOOK_YES: f"poly_{BOOK_COND}",
        }
        # And a live frame for that token now reaches the arb engine's id.
        out = parse_polymarket_ws_message(WS["price_change"], token_map)
        assert f"poly_{PC_COND}" in out


class TestTickWidthGuard:
    """Stub books (0.001 / 0.999) must not emit a 0.50 tick on either feed."""

    def test_polymarket_wide_price_change_is_dropped(self):
        frame = {
            "event_type": "price_change",
            "price_changes": [
                {"asset_id": "1", "best_bid": "0.001", "best_ask": "0.999"},
                {"asset_id": "2", "best_bid": "0.30", "best_ask": "0.51"},
                {"asset_id": "3", "best_bid": "0.40", "best_ask": "0.60"},
            ],
        }
        out = parse_polymarket_ws_message(frame, {"1": "a", "2": "b", "3": "c"})
        assert out == {"c": 0.5}  # 0.20 wide is the limit; 0.21 is not

    def test_polymarket_wide_book_is_dropped(self):
        frame = [
            {
                "event_type": "book",
                "asset_id": "1",
                "bids": [{"price": "0.001", "size": "10"}],
                "asks": [{"price": "0.999", "size": "10"}],
            }
        ]
        assert parse_polymarket_ws_message(frame, {"1": "a"}) == {}


class TestParseKalshiWsMessage:
    @staticmethod
    def _tick(bid, ask, ticker="KXT-1"):
        return {
            "type": "ticker",
            "sid": 1,
            "msg": {
                "market_ticker": ticker,
                "yes_bid_dollars": bid,
                "yes_ask_dollars": ask,
            },
        }

    def test_two_sided_tick_emits_mid_under_internal_id(self):
        assert parse_kalshi_ws_message(self._tick("0.4500", "0.4700")) == (
            "kal_KXT-1",
            pytest.approx(0.46),
        )

    def test_zero_bid_string_is_one_sided_not_a_price(self):
        # "0.0000" is a truthy string: the old `if bid and ask` let it through.
        assert parse_kalshi_ws_message(self._tick("0.0000", "0.1300")) is None
        assert parse_kalshi_ws_message(self._tick("0.4000", "0.0000")) is None

    def test_wide_stub_book_is_dropped(self):
        assert parse_kalshi_ws_message(self._tick("0.0200", "0.9800")) is None

    def test_non_ticker_and_garbage(self):
        for frame in (None, [], {"type": "subscribed"}, {"type": "ticker"}):
            assert parse_kalshi_ws_message(frame) is None
