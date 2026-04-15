"""
Integration tests for DB schema compliance.

Every INSERT statement in the paper trading pipeline must match the
real migration schema. These tests exercise each INSERT path against
an in-memory DB with the real schema to catch column name mismatches,
FK violations, and NOT NULL failures.

This is the exact bug class that caused the FK cascade failure:
violations INSERT used wrong column names → signal never inserted →
order FK to signals failed → all trades silently dropped.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def _insert_market(db, market_id, platform="polymarket"):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """INSERT OR IGNORE INTO markets
           (id, platform, platform_id, title, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'open', ?, ?)""",
        (market_id, platform, market_id, f"Title {market_id}", now, now),
    )
    await db.execute(
        """INSERT INTO market_prices
           (market_id, yes_price, no_price, spread, liquidity, polled_at)
           VALUES (?, 0.50, 0.50, 0.02, 10000, ?)""",
        (market_id, now),
    )


@pytest.mark.asyncio
class TestMarketPairsInsert:
    async def test_insert_market_pair(self, db):
        """market_pairs INSERT matches schema."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "mkt_a")
        await _insert_market(db, "mkt_b", "kalshi")

        pair_id = "mkt_a_mkt_b"
        await db.execute(
            """INSERT OR IGNORE INTO market_pairs
               (id, market_id_a, market_id_b, pair_type, similarity_score,
                match_method, active, created_at, updated_at)
               VALUES (?, ?, ?, 'cross_platform', ?, 'inverted_index', 1, ?, ?)""",
            (pair_id, "mkt_a", "mkt_b", 0.85, now, now),
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM market_pairs WHERE id = ?", (pair_id,))
        row = await cursor.fetchone()
        assert row is not None


@pytest.mark.asyncio
class TestViolationsInsert:
    async def test_insert_violation(self, db):
        """violations INSERT uses correct column names (price_a_at_detect, not price_a)."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "v_a")
        await _insert_market(db, "v_b", "kalshi")

        pair_id = "v_a_v_b"
        await db.execute(
            """INSERT INTO market_pairs
               (id, market_id_a, market_id_b, pair_type, similarity_score,
                match_method, active, created_at, updated_at)
               VALUES (?, ?, ?, 'cross_platform', 0.85, 'inverted_index', 1, ?, ?)""",
            (pair_id, "v_a", "v_b", now, now),
        )

        violation_id = f"viol_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT OR IGNORE INTO violations
               (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
                raw_spread, net_spread, fee_estimate_a, fee_estimate_b,
                status, detected_at, updated_at)
               VALUES (?, ?, 'cross_platform', ?, ?, ?, ?, ?, ?, 'detected', ?, ?)""",
            (violation_id, pair_id, 0.45, 0.55, 0.10, 0.08, 0.009, 0.011, now, now),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM violations WHERE id = ?", (violation_id,)
        )
        row = await cursor.fetchone()
        assert row is not None

    async def test_violation_fk_to_market_pairs(self, db):
        """violations.pair_id must reference a valid market_pairs.id."""
        now = datetime.now(timezone.utc).isoformat()

        # Insert violation with non-existent pair_id should fail FK
        with pytest.raises(Exception):
            await db.execute(
                """INSERT INTO violations
                   (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
                    raw_spread, net_spread, status, detected_at, updated_at)
                   VALUES (?, ?, 'cross_platform', 0.45, 0.55, 0.10, 0.08, 'detected', ?, ?)""",
                ("bad_viol", "nonexistent_pair", now, now),
            )


@pytest.mark.asyncio
class TestSignalsInsert:
    async def test_insert_signal(self, db):
        """signals INSERT includes all NOT NULL columns."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "s_a")
        await _insert_market(db, "s_b", "kalshi")

        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO signals
               (id, violation_id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a, position_size_b,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES (?, NULL, 'P1_cross_market_arb', 'arb_pair', ?, ?,
                       0.05, 0.10, 5.0, 5.0, 10.0, 'fired', ?, ?)""",
            (signal_id, "s_a", "s_b", now, now),
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM signals WHERE id = ?", (signal_id,))
        row = await cursor.fetchone()
        assert row is not None

    async def test_signal_fk_to_markets(self, db):
        """signals.market_id_a must reference markets.id."""
        now = datetime.now(timezone.utc).isoformat()

        with pytest.raises(Exception):
            await db.execute(
                """INSERT INTO signals
                   (id, strategy, signal_type, market_id_a,
                    model_edge, kelly_fraction, position_size_a, total_capital_at_risk,
                    status, fired_at, updated_at)
                   VALUES (?, 'P1', 'arb', ?, 0.05, 0.10, 5.0, 10.0, 'fired', ?, ?)""",
                ("bad_sig", "nonexistent_market", now, now),
            )


@pytest.mark.asyncio
class TestOrdersInsert:
    async def test_insert_order(self, db):
        """orders INSERT matches schema with all required fields."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "o_mkt")

        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a,
                model_edge, kelly_fraction, position_size_a, total_capital_at_risk,
                status, fired_at, updated_at)
               VALUES (?, 'P1', 'arb', ?, 0.05, 0.10, 5.0, 10.0, 'fired', ?, ?)""",
            (signal_id, "o_mkt", now, now),
        )

        order_id = f"PAPER-{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO orders
               (id, signal_id, platform, platform_order_id,
                market_id, side, order_type,
                requested_price, requested_size,
                filled_price, filled_size, slippage, fee_paid,
                status, failure_reason,
                retry_count, submitted_at, filled_at,
                submission_latency_ms, fill_latency_ms,
                strategy, updated_at)
               VALUES (?, ?, 'paper_polymarket', ?,
                       ?, 'buy', 'limit',
                       0.55, 5.0,
                       0.50, 5.0, 0.05, 0.05,
                       'filled', NULL,
                       0, ?, ?, 150, 300,
                       'P1_cross_market_arb', ?)""",
            (order_id, signal_id, order_id, "o_mkt", now, now, now),
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = await cursor.fetchone()
        assert row is not None

    async def test_order_strategy_column(self, db):
        """orders.strategy column stores and retrieves correctly."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "strat_mkt")

        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a,
                model_edge, kelly_fraction, position_size_a, total_capital_at_risk,
                status, fired_at, updated_at)
               VALUES (?, 'P3_calibration_bias', 'single', ?, 0.08, 0.15, 5.0, 5.0, 'fired', ?, ?)""",
            (signal_id, "strat_mkt", now, now),
        )

        order_id = f"PAPER-{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO orders
               (id, signal_id, platform, market_id, side, order_type,
                requested_size, status, submitted_at, strategy, updated_at)
               VALUES (?, ?, 'paper_kalshi', ?, 'buy', 'limit',
                       5.0, 'filled', ?, 'P3_calibration_bias', ?)""",
            (order_id, signal_id, "strat_mkt", now, now),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT strategy FROM orders WHERE id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "P3_calibration_bias"

    async def test_order_strategy_nullable(self, db):
        """orders.strategy is nullable for backward compat."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "null_mkt")

        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a,
                model_edge, kelly_fraction, position_size_a, total_capital_at_risk,
                status, fired_at, updated_at)
               VALUES (?, 'P1', 'arb', ?, 0.05, 0.10, 5.0, 10.0, 'fired', ?, ?)""",
            (signal_id, "null_mkt", now, now),
        )

        order_id = f"PAPER-{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO orders
               (id, signal_id, platform, market_id, side, order_type,
                requested_size, status, submitted_at, updated_at)
               VALUES (?, ?, 'paper', ?, 'buy', 'limit', 5.0, 'pending', ?, ?)""",
            (order_id, signal_id, "null_mkt", now, now),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT strategy FROM orders WHERE id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None

    async def test_order_fk_to_signal(self, db):
        """orders.signal_id must reference signals.id."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "fk_mkt")

        with pytest.raises(Exception):
            await db.execute(
                """INSERT INTO orders
                   (id, signal_id, platform, market_id, side, order_type,
                    requested_size, status, submitted_at, updated_at)
                   VALUES (?, ?, 'paper', ?, 'buy', 'limit', 5.0, 'pending', ?, ?)""",
                ("bad_order", "nonexistent_signal", "fk_mkt", now, now),
            )


@pytest.mark.asyncio
class TestPositionsInsert:
    async def test_insert_position(self, db):
        """positions INSERT matches schema."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "pos_mkt")

        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a,
                model_edge, kelly_fraction, position_size_a, total_capital_at_risk,
                status, fired_at, updated_at)
               VALUES (?, 'P1', 'arb', ?, 0.05, 0.10, 5.0, 10.0, 'fired', ?, ?)""",
            (signal_id, "pos_mkt", now, now),
        )

        pos_id = f"pos_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO positions
               (id, signal_id, market_id, strategy, side, entry_price,
                entry_size, exit_price, exit_size, realized_pnl, fees_paid,
                status, opened_at, closed_at, updated_at)
               VALUES (?, ?, ?, 'P1_cross_market_arb', 'BUY', 0.45,
                       5.0, 0.55, 5.0, 0.50, 0.05,
                       'closed', ?, ?, ?)""",
            (pos_id, signal_id, "pos_mkt", now, now, now),
        )
        await db.commit()

        cursor = await db.execute("SELECT * FROM positions WHERE id = ?", (pos_id,))
        row = await cursor.fetchone()
        assert row is not None


@pytest.mark.asyncio
class TestTradeOutcomesInsert:
    async def test_insert_trade_outcome(self, db):
        """trade_outcomes INSERT with actual_pnl (not realized_pnl)."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "to_a")
        await _insert_market(db, "to_b", "kalshi")

        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a, total_capital_at_risk,
                status, fired_at, updated_at)
               VALUES (?, 'P1', 'arb', ?, ?, 0.05, 0.10, 5.0, 10.0, 'fired', ?, ?)""",
            (signal_id, "to_a", "to_b", now, now),
        )

        trade_id = f"trade_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO trade_outcomes
               (id, signal_id, strategy, violation_id, market_id_a, market_id_b,
                predicted_edge, predicted_pnl, actual_pnl, fees_total,
                edge_captured_pct, signal_to_fill_ms, holding_period_ms,
                resolved_at, created_at)
               VALUES (?, ?, 'P1_cross_market_arb', NULL, ?, ?,
                       0.05, 0.25, 0.20, 0.05,
                       80.0, 250, 5000, ?, ?)""",
            (trade_id, signal_id, "to_a", "to_b", now, now),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT actual_pnl FROM trade_outcomes WHERE id = ?", (trade_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 0.20


@pytest.mark.asyncio
class TestStrategyPnlSnapshotsInsert:
    """Test the normalized strategy_pnl_snapshots table."""

    async def test_insert_strategy_pnl_snapshot(self, db):
        """strategy_pnl_snapshots INSERT matches schema."""
        now = datetime.now(timezone.utc).isoformat()

        # Create parent pnl_snapshot
        cursor = await db.execute(
            """INSERT INTO pnl_snapshots
               (snapshot_type, total_capital, cash, open_positions_count, snapshotted_at)
               VALUES ('scheduled', 100000, 95000, 5, ?)""",
            (now,),
        )
        snapshot_id = cursor.lastrowid
        assert snapshot_id is not None

        # Insert per-strategy rows
        await db.execute(
            """INSERT INTO strategy_pnl_snapshots
               (snapshot_id, strategy, realized_pnl, unrealized_pnl, fees, trade_count, win_count)
               VALUES (?, 'P1_cross_market_arb', 150.50, 25.00, 12.50, 20, 14)""",
            (snapshot_id,),
        )
        await db.execute(
            """INSERT INTO strategy_pnl_snapshots
               (snapshot_id, strategy, realized_pnl, unrealized_pnl, fees, trade_count, win_count)
               VALUES (?, 'P3_calibration_bias', -30.00, 10.00, 8.00, 10, 3)""",
            (snapshot_id,),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM strategy_pnl_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        )
        row = await cursor.fetchone()
        assert row[0] == 2

    async def test_strategy_pnl_fk_to_snapshots(self, db):
        """strategy_pnl_snapshots.snapshot_id must reference pnl_snapshots.id."""
        with pytest.raises(Exception):
            await db.execute(
                """INSERT INTO strategy_pnl_snapshots
                   (snapshot_id, strategy, realized_pnl)
                   VALUES (99999, 'P1_cross_market_arb', 100.0)""",
            )

    async def test_strategy_pnl_query_by_strategy(self, db):
        """Can query strategy_pnl_snapshots filtered by strategy."""
        now = datetime.now(timezone.utc).isoformat()

        cursor = await db.execute(
            """INSERT INTO pnl_snapshots
               (snapshot_type, total_capital, cash, open_positions_count, snapshotted_at)
               VALUES ('scheduled', 100000, 95000, 5, ?)""",
            (now,),
        )
        snapshot_id = cursor.lastrowid

        strategies = [
            "P1_cross_market_arb",
            "P3_calibration_bias",
            "P4_liquidity_timing",
        ]
        for strat in strategies:
            await db.execute(
                """INSERT INTO strategy_pnl_snapshots
                   (snapshot_id, strategy, realized_pnl, trade_count)
                   VALUES (?, ?, 50.0, 10)""",
                (snapshot_id, strat),
            )
        await db.commit()

        cursor = await db.execute(
            "SELECT * FROM strategy_pnl_snapshots WHERE strategy = ?",
            ("P3_calibration_bias",),
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        assert rows[0]["strategy"] == "P3_calibration_bias"


@pytest.mark.asyncio
class TestFullCascade:
    """Test the full FK chain: market_pairs → violations → signals → orders."""

    async def test_full_chain_succeeds(self, db):
        """All 4 levels of the FK chain insert successfully, with strategy on orders."""
        now = datetime.now(timezone.utc).isoformat()
        await _insert_market(db, "chain_a")
        await _insert_market(db, "chain_b", "kalshi")

        pair_id = "chain_a_chain_b"
        await db.execute(
            """INSERT INTO market_pairs
               (id, market_id_a, market_id_b, pair_type, similarity_score,
                match_method, active, created_at, updated_at)
               VALUES (?, ?, ?, 'cross_platform', 0.90, 'inverted_index', 1, ?, ?)""",
            (pair_id, "chain_a", "chain_b", now, now),
        )

        violation_id = f"viol_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO violations
               (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
                raw_spread, net_spread, fee_estimate_a, fee_estimate_b,
                status, detected_at, updated_at)
               VALUES (?, ?, 'cross_platform', 0.45, 0.55, 0.10, 0.08, 0.009, 0.011, 'detected', ?, ?)""",
            (violation_id, pair_id, now, now),
        )

        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO signals
               (id, violation_id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a, position_size_b,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES (?, ?, 'P1_cross_market_arb', 'arb_pair', ?, ?,
                       0.10, 0.20, 5.0, 5.0, 10.0, 'fired', ?, ?)""",
            (signal_id, violation_id, "chain_a", "chain_b", now, now),
        )

        order_id = f"PAPER-{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO orders
               (id, signal_id, platform, platform_order_id,
                market_id, side, order_type,
                requested_price, requested_size,
                filled_price, filled_size, slippage, fee_paid,
                status, retry_count, submitted_at, filled_at,
                submission_latency_ms, fill_latency_ms,
                strategy, updated_at)
               VALUES (?, ?, 'paper_polymarket', ?,
                       ?, 'buy', 'limit',
                       0.50, 5.0, 0.45, 5.0, 0.05, 0.05,
                       'filled', 0, ?, ?, 150, 300,
                       'P1_cross_market_arb', ?)""",
            (order_id, signal_id, order_id, "chain_a", now, now, now),
        )
        await db.commit()

        # Verify all rows exist
        for table, id_val in [
            ("market_pairs", pair_id),
            ("violations", violation_id),
            ("signals", signal_id),
            ("orders", order_id),
        ]:
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM {table} WHERE id = ?", (id_val,)
            )
            row = await cursor.fetchone()
            assert row[0] == 1, f"Missing row in {table}"

        # Verify strategy on order
        cursor = await db.execute(
            "SELECT strategy FROM orders WHERE id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        assert row[0] == "P1_cross_market_arb"


@pytest.mark.asyncio
class TestOrderEventsInsert:
    """order_events INSERT must use the real schema columns."""

    async def test_insert_with_correct_schema(self, db):
        """Verifies the fixed _log_order_event column mapping against real schema.

        order_events has FK(order_id) → orders(id), so we must create the full
        chain: market → market_pairs → violations → signals → orders first.
        """
        import json

        now = datetime.now(timezone.utc).isoformat()

        # Set up FK chain
        await _insert_market(db, "evt_mkt_a")
        await _insert_market(db, "evt_mkt_b", "kalshi")

        pair_id = "evt_mkt_a_evt_mkt_b"
        await db.execute(
            """INSERT INTO market_pairs
               (id, market_id_a, market_id_b, pair_type, similarity_score,
                match_method, active, created_at, updated_at)
               VALUES (?, 'evt_mkt_a', 'evt_mkt_b', 'cross_platform', 0.90,
                       'inverted_index', 1, ?, ?)""",
            (pair_id, now, now),
        )
        violation_id = f"viol_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO violations
               (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
                raw_spread, net_spread, status, detected_at, updated_at)
               VALUES (?, ?, 'cross_platform', 0.45, 0.55, 0.10, 0.08,
                       'detected', ?, ?)""",
            (violation_id, pair_id, now, now),
        )
        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO signals
               (id, violation_id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a, position_size_b,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES (?, ?, 'P1_cross_market_arb', 'arb_pair', 'evt_mkt_a', 'evt_mkt_b',
                       0.10, 0.20, 5.0, 5.0, 10.0, 'fired', ?, ?)""",
            (signal_id, violation_id, now, now),
        )
        order_id = f"PAPER-{uuid.uuid4().hex[:12]}"
        await db.execute(
            """INSERT INTO orders
               (id, signal_id, platform, platform_order_id,
                market_id, side, order_type,
                requested_price, requested_size,
                filled_price, filled_size, slippage, fee_paid,
                status, retry_count, submitted_at, filled_at,
                submission_latency_ms, fill_latency_ms,
                strategy, updated_at)
               VALUES (?, ?, 'paper_polymarket', ?,
                       'evt_mkt_a', 'buy', 'limit',
                       0.45, 5.0, 0.45, 5.0, 0.0, 0.05,
                       'filled', 0, ?, ?, 100, 200,
                       'P1_cross_market_arb', ?)""",
            (order_id, signal_id, order_id, now, now, now),
        )
        await db.commit()

        # Now test the corrected order_events INSERT
        detail_payload = json.dumps(
            {
                "signal_id": signal_id,
                "leg_index": 0,
                "detail": "Submitted to paper_polymarket",
            }
        )
        await db.execute(
            """
            INSERT INTO order_events
            (order_id, event_type, detail, occurred_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (order_id, "ACCEPTED", detail_payload),
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT order_id, event_type, detail FROM order_events WHERE order_id = ?",
            (order_id,),
        )
        row = await cursor.fetchone()
        assert row is not None, "order_events row not written"
        assert row["order_id"] == order_id
        assert row["event_type"] == "ACCEPTED"
        parsed = json.loads(row["detail"])
        assert parsed["signal_id"] == signal_id
        assert parsed["leg_index"] == 0
