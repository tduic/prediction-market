"""
Integration tests for the PaperExecutionClient.

Tests order submission, fill simulation, signal_id propagation,
limit order rejection, fee calculation, and DB writes.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.clients.paper import PaperExecutionClient  # noqa: E402
from execution.models import OrderLeg  # noqa: E402


async def _seed_market_with_price(db, market_id, platform, price):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT OR IGNORE INTO markets
           (id, platform, platform_id, title, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'open', ?, ?)""",
        (market_id, platform, market_id, f"Test {market_id}", now, now),
    )
    await db.execute(
        """INSERT INTO market_prices
           (market_id, yes_price, no_price, spread, liquidity, polled_at)
           VALUES (?, ?, ?, 0.02, 10000, ?)""",
        (market_id, price, round(1 - price, 4), now),
    )
    await db.commit()


async def _create_signal(db, signal_id, market_id):
    """Create a minimal valid signal record for FK constraints."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT INTO signals
           (id, violation_id, strategy, signal_type, market_id_a,
            model_edge, kelly_fraction, position_size_a, total_capital_at_risk,
            status, fired_at, updated_at)
           VALUES (?, NULL, 'P1_cross_market_arb', 'arb_pair', ?,
                   0.05, 0.10, 5.0, 10.0, 'fired', ?, ?)""",
        (signal_id, market_id, now, now),
    )
    await db.commit()


@pytest.mark.asyncio
class TestPaperOrderSubmission:
    async def test_fill_at_market_price(self, db):
        """Order fills at the current DB market price."""
        await _seed_market_with_price(db, "mkt_1", "polymarket", 0.55)
        signal_id = "sig_test_fill"
        await _create_signal(db, signal_id, "mkt_1")

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_1",
            platform="polymarket",
            side="BUY",
            size=5.0,
            limit_price=0.60,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert result.status == "filled"
        assert result.filled_price == 0.55
        assert result.filled_size == 5.0
        assert result.fee_paid > 0

    async def test_signal_id_propagated_to_db(self, db):
        """signal_id is written to the orders table."""
        await _seed_market_with_price(db, "mkt_sig", "polymarket", 0.50)
        signal_id = "sig_propagation_test"
        await _create_signal(db, signal_id, "mkt_sig")

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_sig",
            platform="polymarket",
            side="BUY",
            size=3.0,
            limit_price=0.55,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        cursor = await db.execute(
            "SELECT signal_id FROM orders WHERE id = ?", (result.order_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == signal_id

    async def test_no_price_with_limit_uses_limit(self, db):
        """When no DB price exists, fills at limit price."""
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES (?, 'polymarket', ?, 'No Price Market', 'open', ?, ?)""",
            ("mkt_noprice", "mkt_noprice", now, now),
        )
        signal_id = "sig_noprice"
        await _create_signal(db, signal_id, "mkt_noprice")
        await db.commit()

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_noprice",
            platform="polymarket",
            side="BUY",
            size=2.0,
            limit_price=0.50,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert result.status == "filled"
        assert result.filled_price == 0.50

    async def test_no_price_no_limit_fails(self, db):
        """When no DB price and no limit price, order fails."""
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES (?, 'polymarket', ?, 'No Price Market 2', 'open', ?, ?)""",
            ("mkt_noprice2", "mkt_noprice2", now, now),
        )
        signal_id = "sig_noprice2"
        await _create_signal(db, signal_id, "mkt_noprice2")
        await db.commit()

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_noprice2",
            platform="polymarket",
            side="BUY",
            size=2.0,
            limit_price=None,
            order_type="MARKET",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert result.status == "failed"
        assert "No market price" in result.error_message


@pytest.mark.asyncio
class TestInvalidPriceFallback:
    """
    Regression guard for the paper-fill 0.0 bug.

    Kalshi / Polymarket ingestor code paths default ``last_price`` to 0.0
    when the field is missing from the upstream API response, and the DB
    can hold a stale 0.0 row too. Paper fills must reject non-positive
    prices and fall back to the limit price (or fail when there is none)
    instead of booking a fill at $0.00 and inflating realized PnL.
    """

    async def test_live_zero_price_falls_back_to_limit(self, db, monkeypatch):
        """A 0.0 price from the live fetch is treated as 'no price'."""
        await _seed_market_with_price(db, "mkt_zero_live", "kalshi", 0.45)
        signal_id = "sig_zero_live"
        await _create_signal(db, signal_id, "mkt_zero_live")

        client = PaperExecutionClient(db, platform_label="paper_kalshi")

        async def _zero_live(self, platform, platform_id):
            return 0.0

        monkeypatch.setattr(PaperExecutionClient, "_fetch_live_price", _zero_live)

        # Force the DB fallback to also return 0.0, mirroring the prod case
        # where every cached price for this market is stale/zero.
        async def _zero_db(self, market_id, book=None):
            return 0.0

        monkeypatch.setattr(PaperExecutionClient, "_get_db_price", _zero_db)

        leg = OrderLeg(
            market_id="mkt_zero_live",
            platform="kalshi",
            side="BUY",
            size=45.8,
            limit_price=0.0505,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert result.status == "filled"
        assert result.filled_price == 0.0505
        # Fee must be non-zero when a real fill occurs.
        assert result.fee_paid > 0

    async def test_db_zero_price_falls_back_to_limit(self, db, monkeypatch):
        """A stale 0.0 row in market_prices is treated as 'no price'."""
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES (?, 'kalshi', ?, 'Stale Zero', 'open', ?, ?)""",
            ("mkt_zero_db", "mkt_zero_db", now, now),
        )
        await db.execute(
            """INSERT INTO market_prices
               (market_id, yes_price, no_price, spread, liquidity, polled_at)
               VALUES (?, 0.0, 1.0, 0.0, 10000, ?)""",
            ("mkt_zero_db", now),
        )
        signal_id = "sig_zero_db"
        await _create_signal(db, signal_id, "mkt_zero_db")
        await db.commit()

        client = PaperExecutionClient(db, platform_label="paper_kalshi")

        # Disable the live path so we exercise the DB fallback only.
        async def _none_live(self, platform, platform_id):
            return None

        monkeypatch.setattr(PaperExecutionClient, "_fetch_live_price", _none_live)

        leg = OrderLeg(
            market_id="mkt_zero_db",
            platform="kalshi",
            side="BUY",
            size=45.8,
            limit_price=0.0505,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert result.status == "filled"
        assert result.filled_price == 0.0505
        assert result.fee_paid > 0

    async def test_invalid_price_no_limit_fails(self, db, monkeypatch):
        """No valid price and no limit → order fails (not filled at 0)."""
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES (?, 'kalshi', ?, 'Invalid Only', 'open', ?, ?)""",
            ("mkt_invalid_only", "mkt_invalid_only", now, now),
        )
        signal_id = "sig_invalid_only"
        await _create_signal(db, signal_id, "mkt_invalid_only")
        await db.commit()

        client = PaperExecutionClient(db, platform_label="paper_kalshi")

        async def _zero(self, *args, **kwargs):
            return 0.0

        monkeypatch.setattr(PaperExecutionClient, "_fetch_live_price", _zero)
        monkeypatch.setattr(PaperExecutionClient, "_get_db_price", _zero)

        leg = OrderLeg(
            market_id="mkt_invalid_only",
            platform="kalshi",
            side="BUY",
            size=10.0,
            limit_price=None,
            order_type="MARKET",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert result.status == "failed"
        assert "No market price" in result.error_message


@pytest.mark.asyncio
class TestLimitOrderRejection:
    async def test_buy_above_limit_rejected(self, db):
        """BUY rejected when market price > limit price."""
        await _seed_market_with_price(db, "mkt_lim_buy", "polymarket", 0.70)
        signal_id = "sig_lim_buy"
        await _create_signal(db, signal_id, "mkt_lim_buy")

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_lim_buy",
            platform="polymarket",
            side="BUY",
            size=5.0,
            limit_price=0.60,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert result.status == "failed"
        assert "above limit" in result.error_message

    async def test_sell_below_limit_rejected(self, db):
        """SELL rejected when market price < limit price."""
        await _seed_market_with_price(db, "mkt_lim_sell", "kalshi", 0.30)
        signal_id = "sig_lim_sell"
        await _create_signal(db, signal_id, "mkt_lim_sell")

        client = PaperExecutionClient(db, platform_label="paper_kalshi")
        leg = OrderLeg(
            market_id="mkt_lim_sell",
            platform="kalshi",
            side="SELL",
            size=5.0,
            limit_price=0.40,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert result.status == "failed"
        assert "below limit" in result.error_message


@pytest.mark.asyncio
class TestFeeCalculation:
    async def test_polymarket_fee_rate(self, db):
        """Polymarket uses 2% fee rate."""
        await _seed_market_with_price(db, "mkt_fee_pm", "polymarket", 0.50)
        signal_id = "sig_fee_pm"
        await _create_signal(db, signal_id, "mkt_fee_pm")

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_fee_pm",
            platform="polymarket",
            side="BUY",
            size=10.0,
            limit_price=0.55,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        expected_fee = round(10.0 * 0.50 * 0.02, 4)
        assert result.fee_paid == expected_fee

    async def test_kalshi_fee_rate(self, db):
        """Kalshi uses 7% fee rate."""
        await _seed_market_with_price(db, "mkt_fee_ka", "kalshi", 0.50)
        signal_id = "sig_fee_ka"
        await _create_signal(db, signal_id, "mkt_fee_ka")

        client = PaperExecutionClient(db, platform_label="paper_kalshi")
        leg = OrderLeg(
            market_id="mkt_fee_ka",
            platform="kalshi",
            side="BUY",
            size=10.0,
            limit_price=0.55,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        expected_fee = round(10.0 * 0.50 * 0.07, 4)
        assert result.fee_paid == expected_fee


@pytest.mark.asyncio
class TestDBWrites:
    async def test_filled_order_creates_order_event(self, db):
        """A filled order also writes to order_events."""
        await _seed_market_with_price(db, "mkt_evt", "polymarket", 0.50)
        signal_id = "sig_evt"
        await _create_signal(db, signal_id, "mkt_evt")

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_evt",
            platform="polymarket",
            side="BUY",
            size=5.0,
            limit_price=0.55,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        cursor = await db.execute(
            "SELECT event_type FROM order_events WHERE order_id = ?",
            (result.order_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "filled"

    async def test_failed_order_no_event(self, db):
        """A failed order does NOT write to order_events."""
        await _seed_market_with_price(db, "mkt_fail_evt", "polymarket", 0.70)
        signal_id = "sig_fail_evt"
        await _create_signal(db, signal_id, "mkt_fail_evt")

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_fail_evt",
            platform="polymarket",
            side="BUY",
            size=5.0,
            limit_price=0.60,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert result.status == "failed"
        cursor = await db.execute(
            "SELECT COUNT(*) FROM order_events WHERE order_id = ?",
            (result.order_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == 0

    async def test_cancel_order(self, db):
        """cancel_order updates the order status."""
        await _seed_market_with_price(db, "mkt_cancel", "polymarket", 0.50)
        signal_id = "sig_cancel"
        await _create_signal(db, signal_id, "mkt_cancel")

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_cancel",
            platform="polymarket",
            side="BUY",
            size=5.0,
            limit_price=0.55,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        success = await client.cancel_order(result.order_id)
        assert success is True

    async def test_get_order_status(self, db):
        """get_order_status returns correct data."""
        await _seed_market_with_price(db, "mkt_status", "polymarket", 0.50)
        signal_id = "sig_status"
        await _create_signal(db, signal_id, "mkt_status")

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_status",
            platform="polymarket",
            side="BUY",
            size=5.0,
            limit_price=0.55,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        status = await client.get_order_status(result.order_id)
        assert status is not None
        assert status["status"] == "filled"
        assert status["fill_price"] == 0.50

    async def test_stats_tracking(self, db):
        """Client tracks total_submitted and total_filled."""
        await _seed_market_with_price(db, "mkt_stats", "polymarket", 0.50)
        signal_id = "sig_stats"
        await _create_signal(db, signal_id, "mkt_stats")

        client = PaperExecutionClient(db, platform_label="paper_polymarket")
        leg = OrderLeg(
            market_id="mkt_stats",
            platform="polymarket",
            side="BUY",
            size=5.0,
            limit_price=0.55,
            order_type="LIMIT",
        )
        await client.submit_order(leg, signal_id=signal_id)
        await db.commit()

        assert client.total_submitted == 1
        assert client.total_filled == 1
