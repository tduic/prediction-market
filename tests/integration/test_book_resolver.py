"""
Tests for execution.clients.polymarket_book.BookResolver.

The resolver encodes the no-naked-shorts rule: a SELL on Polymarket
becomes either a SELL on the YES book (if we hold inventory) or a
BUY on the NO book at (1 - p) otherwise. Returns None for unresolvable
requests (missing tokens, invalid price/size, kill-switch).
"""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.clients.polymarket_book import BookResolver  # noqa: E402
from execution.enums import Book, Side  # noqa: E402


async def _seed_market(
    db, market_id="poly_0xA", yes="111", no="222", last_price_no=0.4
):
    await db.execute(
        """INSERT INTO markets
           (id, platform, platform_id, title, yes_token_id, no_token_id,
            last_price_no, status, created_at, updated_at)
           VALUES (?, 'polymarket', ?, 't', ?, ?, ?, 'open', 'now', 'now')""",
        (market_id, market_id, yes, no, last_price_no),
    )
    await db.commit()


async def _seed_open_buy_yes_position(db, market_id, entry_size=10.0):
    # Signals row to satisfy FK
    await db.execute(
        """INSERT OR IGNORE INTO signals
           (id, strategy, signal_type, market_id_a, market_id_b,
            model_edge, kelly_fraction, position_size_a,
            total_capital_at_risk, status, fired_at, updated_at)
           VALUES ('sig_seed', 's', 'arb_pair', ?, ?,
                   0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        (market_id, market_id),
    )
    await db.execute(
        """INSERT INTO positions
           (id, signal_id, market_id, strategy, side, book, entry_price,
            entry_size, status, opened_at, updated_at)
           VALUES ('pos_seed', 'sig_seed', ?, 'P3_calibration_bias',
                   'BUY', 'YES', 0.5, ?, 'open', 'now', 'now')""",
        (market_id, entry_size),
    )
    await db.commit()


@pytest.mark.asyncio
class TestBookResolver:
    async def test_buy_returns_yes_untranslated(self, db):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.BUY, 10, 0.6)
        assert r is not None
        assert (r.token_id, r.side, r.limit_price, r.book, r.translated) == (
            "111", Side.BUY, 0.6, Book.YES, False,
        )

    async def test_sell_with_no_inventory_translates_to_buy_no(self, db):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.62)
        assert r is not None
        assert r.token_id == "222"
        assert r.side is Side.BUY
        assert r.limit_price == pytest.approx(0.38)
        assert r.book is Book.NO
        assert r.translated is True

    async def test_sell_with_sufficient_yes_inventory_uses_yes_book(self, db):
        await _seed_market(db)
        await _seed_open_buy_yes_position(db, "poly_0xA", entry_size=10.0)
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.55)
        assert r is not None
        assert r.token_id == "111"
        assert r.side is Side.SELL
        assert r.limit_price == 0.55
        assert r.book is Book.YES
        assert r.translated is False

    async def test_sell_with_partial_inventory_still_translates(self, db):
        """All-or-nothing: inventory=7 < request=10 → BUY NO for full size."""
        await _seed_market(db)
        await _seed_open_buy_yes_position(db, "poly_0xA", entry_size=7.0)
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.60)
        assert r is not None
        assert r.translated is True
        assert r.book is Book.NO
        assert r.size == 10  # full size, not 3

    async def test_inventory_only_counts_open_buy_yes(self, db):
        await _seed_market(db)
        # Closed BUY — should not count
        await db.execute(
            """INSERT OR IGNORE INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_x', 's', 'arb_pair', 'poly_0xA', 'poly_0xA',
                       0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        )
        await db.execute(
            """INSERT INTO positions
               (id, signal_id, market_id, strategy, side, book,
                entry_price, entry_size, status, opened_at, updated_at)
               VALUES ('pos_closed', 'sig_x', 'poly_0xA', 's',
                       'BUY', 'YES', 0.5, 10, 'closed', 'now', 'now'),
                      ('pos_sell',   'sig_x', 'poly_0xA', 's',
                       'SELL', 'YES', 0.5, 10, 'open', 'now', 'now'),
                      ('pos_no',     'sig_x', 'poly_0xA', 's',
                       'BUY', 'NO',  0.5, 10, 'open', 'now', 'now')""",
        )
        await db.commit()
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.60)
        assert r is not None and r.translated  # still translated (inventory=0)

    async def test_inventory_does_not_cross_markets(self, db):
        await _seed_market(db, "poly_0xAAA")
        await _seed_market(db, "poly_0xBBB")
        await _seed_open_buy_yes_position(db, "poly_0xAAA", entry_size=100)
        r = await BookResolver(db).resolve("poly_0xBBB", Side.SELL, 10, 0.60)
        assert r is not None and r.translated

    async def test_missing_market_returns_none(self, db):
        r = await BookResolver(db).resolve("poly_0xNOPE", Side.BUY, 10, 0.5)
        assert r is None

    async def test_missing_yes_token_returns_none(self, db):
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_0xNo_yes', 'polymarket', '0x', 't',
                       NULL, '222', 'open', 'now', 'now')""",
        )
        await db.commit()
        r = await BookResolver(db).resolve("poly_0xNo_yes", Side.BUY, 10, 0.5)
        assert r is None

    async def test_missing_no_token_with_sell_returns_none(self, db):
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_0xNo_no', 'polymarket', '0x', 't',
                       '111', NULL, 'open', 'now', 'now')""",
        )
        await db.commit()
        r = await BookResolver(db).resolve("poly_0xNo_no", Side.SELL, 10, 0.60)
        assert r is None

    async def test_missing_no_token_with_buy_returns_yes(self, db):
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_0xNo_no2', 'polymarket', '0x', 't',
                       '111', NULL, 'open', 'now', 'now')""",
        )
        await db.commit()
        r = await BookResolver(db).resolve("poly_0xNo_no2", Side.BUY, 10, 0.5)
        assert r is not None
        assert r.book is Book.YES

    async def test_sell_price_at_bounds(self, db):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.99)
        assert r is not None
        assert r.limit_price == pytest.approx(0.01)

    @pytest.mark.parametrize("bad", [None, 0.0, -0.1, 1.0, 1.5])
    async def test_invalid_price_returns_none(self, db, bad):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.BUY, 10, bad)
        assert r is None

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    async def test_invalid_size_returns_none(self, db, bad):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.BUY, bad, 0.5)
        assert r is None

    async def test_kill_switch_disables_translation(self, db, monkeypatch):
        await _seed_market(db)
        monkeypatch.setenv("POLYMARKET_ALLOW_SHORT_TRANSLATION", "false")
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.60)
        assert r is None

    async def test_kill_switch_does_not_block_sell_with_inventory(
        self, db, monkeypatch
    ):
        await _seed_market(db)
        await _seed_open_buy_yes_position(db, "poly_0xA", entry_size=10)
        monkeypatch.setenv("POLYMARKET_ALLOW_SHORT_TRANSLATION", "false")
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.55)
        assert r is not None  # uses inventory path, no translation
        assert r.translated is False
