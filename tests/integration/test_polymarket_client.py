"""
Tests for ``execution/clients/polymarket.PolymarketExecutionClient``.

Focus is on the DB-backed pieces that don't require the py-clob-client
network stack. In particular: resolving the ERC-1155 token_id from our
internal market_id, and refusing to route an order when the token_id is
missing (which would otherwise result in a CLOB 4xx on the wrong token).
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.clients.polymarket import PolymarketExecutionClient  # noqa: E402
from execution.models import OrderLeg  # noqa: E402


@pytest.mark.asyncio
class TestResolveTokenId:
    async def test_returns_yes_token_id_when_set(self, db):
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_condA', 'polymarket', '0xcondA', 't',
                       '111', '222', 'open', 'now', 'now')""",
        )
        await db.commit()

        client = PolymarketExecutionClient(db, private_key="dummy", funder="0x0")
        tok = await client._resolve_token_id("poly_condA")
        assert tok == "111"

    async def test_returns_none_when_token_missing(self, db):
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_noTok', 'polymarket', '0xnoTok', 't',
                       NULL, NULL, 'open', 'now', 'now')""",
        )
        await db.commit()

        client = PolymarketExecutionClient(db, private_key="dummy", funder="0x0")
        tok = await client._resolve_token_id("poly_noTok")
        assert tok is None

    async def test_returns_none_when_market_unknown(self, db):
        client = PolymarketExecutionClient(db, private_key="dummy", funder="0x0")
        tok = await client._resolve_token_id("poly_does_not_exist")
        assert tok is None


@pytest.mark.asyncio
class TestSubmitOrderRouting:
    async def test_refuses_when_no_token_id_on_file(self, db):
        """Without yes_token_id we must NOT send `leg.market_id` (prefixed
        internal id) to CLOB — the upstream 4xx is silent failure. Instead
        write a failed order row with a clear error message."""
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_bad', 'polymarket', '0xbad', 't',
                       NULL, NULL, 'open', 'now', 'now')""",
        )
        await db.commit()

        # orders.signal_id is NOT NULL → seed a signal row and pass its id.
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_test', 's', 'arb_pair', 'poly_bad', 'poly_bad',
                       0.01, 0.01, 10.0, 10.0,
                       'fired', 'now', 'now')""",
        )
        await db.commit()

        client = PolymarketExecutionClient(db, private_key="dummy", funder="0x0")
        leg = OrderLeg(
            market_id="poly_bad",
            platform="polymarket",
            side="BUY",
            size=10.0,
            limit_price=0.5,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id="sig_test")
        assert result.status == "failed"
        assert "token_id" in (result.error_message or "")
        # Order row was written.
        cursor = await db.execute(
            "SELECT status FROM orders WHERE id = ?", (result.order_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "failed"


@pytest.mark.asyncio
class TestWriteOrderWithResolvedOrder:
    async def test_write_order_uses_resolved_side_price_and_book(self, db):
        """When write_order gets a ResolvedOrder, the orders row
        reflects what hit the exchange (resolved values), not the
        original strategy intent."""
        from execution.clients.base import BaseExecutionClient
        from execution.clients.base import OrderResult
        from execution.clients.polymarket_book import ResolvedOrder
        from execution.enums import Book, Side
        from execution.models import OrderLeg

        # Seed market first (FK dependency for signals).
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_m', 'polymarket', '0xm', 't', '111', '222',
                       'open', 'now', 'now')""",
        )
        # Seed signal for FK.
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_w', 's', 'arb_pair', 'poly_m', 'poly_m',
                       0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        )
        await db.commit()

        class _Client(BaseExecutionClient):
            async def submit_order(self, leg, **kw):
                raise NotImplementedError

        client = _Client(db, platform_label="polymarket")
        leg = OrderLeg(
            market_id="poly_m", platform="polymarket",
            side=Side.SELL, size=10, limit_price=0.62,
        )
        resolved = ResolvedOrder(
            token_id="222", side=Side.BUY, limit_price=0.38,
            size=10, book=Book.NO, translated=True,
        )
        result = OrderResult(
            order_id="ord_x", platform="polymarket", status="pending",
            submission_latency_ms=0,
        )
        await client.write_order(leg, result, signal_id="sig_w", resolved=resolved)
        await db.commit()

        cursor = await db.execute(
            "SELECT side, book, requested_price FROM orders WHERE id = 'ord_x'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "BUY"     # resolved side, not original SELL
        assert row[1] == "NO"      # resolved book
        assert row[2] == 0.38      # translated price

    async def test_write_order_without_resolved_uses_leg_values(self, db):
        """Kalshi (and any other caller without a resolver) keeps today's
        behavior: orders row reflects leg.side and leg.limit_price,
        book defaults to 'YES' via migration 012."""
        from execution.clients.base import BaseExecutionClient, OrderResult
        from execution.enums import Side
        from execution.models import OrderLeg

        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES ('kal_m', 'kalshi', 'KXM', 't', 'open', 'now', 'now')""",
        )
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_k', 's', 'arb_pair', 'kal_m', 'kal_m',
                       0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        )
        await db.commit()

        class _Client(BaseExecutionClient):
            async def submit_order(self, leg, **kw):
                raise NotImplementedError

        client = _Client(db, platform_label="kalshi")
        leg = OrderLeg(
            market_id="kal_m", platform="kalshi",
            side=Side.BUY, size=10, limit_price=0.35,
        )
        result = OrderResult(
            order_id="ord_k", platform="kalshi", status="pending",
            submission_latency_ms=0,
        )
        await client.write_order(leg, result, signal_id="sig_k")
        await db.commit()

        cursor = await db.execute(
            "SELECT side, book, requested_price FROM orders WHERE id = 'ord_k'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "BUY"
        assert row[1] == "YES"
        assert row[2] == 0.35
