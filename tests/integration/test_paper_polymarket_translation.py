"""
Paper-execution fidelity for Polymarket no-naked-shorts translation.

Paper must simulate the same translated order live would send. That
means reading the NO-book reference price from ``markets.last_price_no``
when a SELL is translated, and applying the same marketable-at-limit
check against it.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def _seed(db, last_price_no=0.40):
    await db.execute(
        """INSERT INTO markets
           (id, platform, platform_id, title, yes_token_id, no_token_id,
            last_price_no, status, created_at, updated_at)
           VALUES ('poly_p', 'polymarket', '0xp', 't', '111', '222',
                   ?, 'open', 'now', 'now')""",
        (last_price_no,),
    )
    # market_prices has the YES price (ingestor-derived complement for NO).
    await db.execute(
        """INSERT INTO market_prices
           (market_id, yes_price, no_price, spread, liquidity, polled_at)
           VALUES ('poly_p', 0.60, 0.40, 0.02, 10000, 'now')""",
    )
    await db.execute(
        """INSERT INTO signals
           (id, strategy, signal_type, market_id_a, market_id_b,
            model_edge, kelly_fraction, position_size_a,
            total_capital_at_risk, status, fired_at, updated_at)
           VALUES ('sig_p', 's', 'arb_pair', 'poly_p', 'poly_p',
                   0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
    )
    await db.commit()


@pytest.mark.asyncio
class TestPaperPolymarketTranslation:
    async def test_simulates_translated_buy_no_fill(self, db):
        from execution.clients.paper import PaperExecutionClient
        from execution.enums import Side
        from execution.models import OrderLeg

        await _seed(db, last_price_no=0.40)

        client = PaperExecutionClient(db, platform_label="polymarket")
        leg = OrderLeg(
            market_id="poly_p", platform="polymarket",
            side=Side.SELL, size=10, limit_price=0.60,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id="sig_p")

        # Translated limit is 1 - 0.60 = 0.40, NO book price is 0.40,
        # so 0.40 <= 0.40 → fillable as a BUY.
        assert result.status in ("filled", "partially_filled")
        cursor = await db.execute(
            "SELECT side, book, requested_price FROM orders WHERE id = ?",
            (result.order_id,),
        )
        row = await cursor.fetchone()
        assert tuple(row) == ("BUY", "NO", 0.40)

    async def test_rejects_when_translated_price_below_no_ask(self, db):
        from execution.clients.paper import PaperExecutionClient
        from execution.enums import Side
        from execution.models import OrderLeg

        # NO book trading at 0.50; translated limit of 0.38 is too low.
        await _seed(db, last_price_no=0.50)

        client = PaperExecutionClient(db, platform_label="polymarket")
        leg = OrderLeg(
            market_id="poly_p", platform="polymarket",
            side=Side.SELL, size=10, limit_price=0.62,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id="sig_p")
        assert result.status == "failed"
        assert "above limit" in (result.error_message or "")
