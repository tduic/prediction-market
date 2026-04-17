"""
Tests for core/engine/resolution.py and core/engine/reconciliation.py.

Covers:
  - close_resolved_positions: closes open positions for resolved markets,
    computes PnL at settlement price, sets resolution_outcome.
  - reconcile_internal_state: flags orphaned positions, stuck pending orders,
    and unbalanced arb pairs to reconciliation_log.
"""

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.engine.reconciliation import reconcile_internal_state  # noqa: E402
from core.engine.resolution import close_resolved_positions  # noqa: E402


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _seed_market(
    db,
    market_id: str,
    *,
    status: str = "open",
    outcome: str | None = None,
    outcome_value: float | None = None,
) -> None:
    now = _iso_now()
    await db.execute(
        """INSERT INTO markets
           (id, platform, platform_id, title, status, outcome, outcome_value,
            created_at, updated_at)
           VALUES (?, 'kalshi', ?, ?, ?, ?, ?, ?, ?)""",
        (market_id, market_id, f"Market {market_id}", status, outcome,
         outcome_value, now, now),
    )


async def _seed_signal(db, signal_id: str, market_id: str) -> None:
    """Minimal signal row to satisfy FKs on orders/positions."""
    now = _iso_now()
    await db.execute(
        """INSERT INTO signals
           (id, strategy, signal_type, market_id_a, model_edge,
            kelly_fraction, position_size_a, total_capital_at_risk,
            fired_at, updated_at)
           VALUES (?, 'P1_cross_market_arb', 'arb', ?, 0.02,
                   0.25, 100.0, 100.0, ?, ?)""",
        (signal_id, market_id, now, now),
    )


async def _seed_position(
    db,
    pos_id: str,
    *,
    signal_id: str,
    market_id: str,
    side: str = "BUY",
    entry_price: float = 0.40,
    entry_size: float = 100.0,
    status: str = "open",
    fees_paid: float = 0.0,
) -> None:
    now = _iso_now()
    await db.execute(
        """INSERT INTO positions
           (id, signal_id, market_id, strategy, side, entry_price, entry_size,
            fees_paid, status, opened_at, updated_at)
           VALUES (?, ?, ?, 'P1_cross_market_arb', ?, ?, ?, ?, ?, ?, ?)""",
        (pos_id, signal_id, market_id, side, entry_price, entry_size,
         fees_paid, status, now, now),
    )


async def _seed_order(
    db,
    order_id: str,
    *,
    signal_id: str,
    market_id: str,
    status: str = "pending",
    platform: str = "kalshi",
    submitted_at: int | None = None,
    filled_price: float | None = None,
) -> None:
    ts = submitted_at if submitted_at is not None else int(time.time())
    now = _iso_now()
    await db.execute(
        """INSERT INTO orders
           (id, signal_id, platform, market_id, side, order_type,
            requested_size, filled_price, status, submitted_at, updated_at)
           VALUES (?, ?, ?, ?, 'buy', 'market', 100.0, ?, ?, ?, ?)""",
        (order_id, signal_id, platform, market_id, filled_price, status,
         str(ts), now),
    )


# ── Resolution tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolution_closes_yes_winner(db):
    await _seed_market(db, "mkt1", status="resolved", outcome="yes",
                       outcome_value=1.0)
    await _seed_signal(db, "sig1", "mkt1")
    await _seed_position(db, "pos1", signal_id="sig1", market_id="mkt1",
                         side="BUY", entry_price=0.40, entry_size=100.0)
    await db.commit()

    summary = await close_resolved_positions(db)

    assert summary["closed"] == 1.0
    assert summary["checked"] == 1.0
    # BUY at 0.40, settles at 1.0, size 100 → pnl = 60.0
    assert summary["total_pnl"] == pytest.approx(60.0)

    cursor = await db.execute(
        "SELECT status, exit_price, realized_pnl, resolution_outcome "
        "FROM positions WHERE id='pos1'"
    )
    row = await cursor.fetchone()
    assert row["status"] == "closed"
    assert row["exit_price"] == 1.0
    assert row["realized_pnl"] == pytest.approx(60.0)
    assert row["resolution_outcome"] == "yes"


@pytest.mark.asyncio
async def test_resolution_closes_no_loser(db):
    await _seed_market(db, "mkt2", status="resolved", outcome="no",
                       outcome_value=0.0)
    await _seed_signal(db, "sig2", "mkt2")
    await _seed_position(db, "pos2", signal_id="sig2", market_id="mkt2",
                         side="BUY", entry_price=0.40, entry_size=100.0)
    await db.commit()

    summary = await close_resolved_positions(db)

    assert summary["closed"] == 1.0
    # BUY at 0.40, settles at 0.0, size 100 → pnl = -40.0
    assert summary["total_pnl"] == pytest.approx(-40.0)


@pytest.mark.asyncio
async def test_resolution_skips_open_markets(db):
    await _seed_market(db, "mkt3", status="open")
    await _seed_signal(db, "sig3", "mkt3")
    await _seed_position(db, "pos3", signal_id="sig3", market_id="mkt3")
    await db.commit()

    summary = await close_resolved_positions(db)

    assert summary["checked"] == 0.0
    assert summary["closed"] == 0.0
    cursor = await db.execute("SELECT status FROM positions WHERE id='pos3'")
    row = await cursor.fetchone()
    assert row["status"] == "open"


@pytest.mark.asyncio
async def test_resolution_infers_outcome_from_label_when_no_value(db):
    """If outcome_value is NULL, fall back to YES→1.0 / NO→0.0."""
    await _seed_market(db, "mkt4", status="resolved", outcome="YES",
                       outcome_value=None)
    await _seed_signal(db, "sig4", "mkt4")
    await _seed_position(db, "pos4", signal_id="sig4", market_id="mkt4",
                         side="BUY", entry_price=0.30, entry_size=50.0)
    await db.commit()

    summary = await close_resolved_positions(db)

    assert summary["closed"] == 1.0
    # BUY at 0.30, YES→1.0, size 50 → pnl = 35.0
    assert summary["total_pnl"] == pytest.approx(35.0)


@pytest.mark.asyncio
async def test_resolution_sell_side_pnl(db):
    """SELL side PnL = (entry - exit) * size."""
    await _seed_market(db, "mkt5", status="resolved", outcome="no",
                       outcome_value=0.0)
    await _seed_signal(db, "sig5", "mkt5")
    await _seed_position(db, "pos5", signal_id="sig5", market_id="mkt5",
                         side="SELL", entry_price=0.60, entry_size=100.0)
    await db.commit()

    summary = await close_resolved_positions(db)

    assert summary["closed"] == 1.0
    # SELL at 0.60, settles at 0.0, size 100 → pnl = 60.0
    assert summary["total_pnl"] == pytest.approx(60.0)


# ── Reconciliation tests ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconciliation_flags_orphaned_position(db):
    """Position 'open' but its order is 'failed' → flagged."""
    await _seed_market(db, "mktA", status="open")
    await _seed_signal(db, "sigA", "mktA")
    await _seed_position(db, "posA", signal_id="sigA", market_id="mktA")
    await _seed_order(db, "ordA", signal_id="sigA", market_id="mktA",
                      status="failed")
    await db.commit()

    summary = await reconcile_internal_state(db)
    assert summary["orphaned_positions"] == 1

    cursor = await db.execute(
        "SELECT check_type, detail FROM reconciliation_log "
        "WHERE check_type='orphaned_position'"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert "posA" in rows[0]["detail"]


@pytest.mark.asyncio
async def test_reconciliation_clean_when_everything_consistent(db):
    await _seed_market(db, "mktB", status="open")
    await _seed_signal(db, "sigB", "mktB")
    await _seed_position(db, "posB", signal_id="sigB", market_id="mktB")
    await _seed_order(db, "ordB", signal_id="sigB", market_id="mktB",
                      status="filled", filled_price=0.40)
    await db.commit()

    summary = await reconcile_internal_state(db)
    assert summary["orphaned_positions"] == 0
    assert summary["stuck_pending_orders"] == 0
    assert summary["unbalanced_arb_pairs"] == 0


@pytest.mark.asyncio
async def test_reconciliation_flags_stuck_pending_order(db):
    """Order 'pending' for > STUCK_PENDING_THRESHOLD_S → flagged."""
    await _seed_market(db, "mktC", status="open")
    await _seed_signal(db, "sigC", "mktC")
    # Submitted an hour ago, still pending
    await _seed_order(
        db, "ordC", signal_id="sigC", market_id="mktC",
        status="pending", submitted_at=int(time.time()) - 3600,
    )
    await db.commit()

    summary = await reconcile_internal_state(db)
    assert summary["stuck_pending_orders"] == 1

    cursor = await db.execute(
        "SELECT detail FROM reconciliation_log "
        "WHERE check_type='stuck_pending_order'"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert "ordC" in rows[0]["detail"]


@pytest.mark.asyncio
async def test_reconciliation_skips_recent_pending_order(db):
    """Order 'pending' but submitted just now → not flagged."""
    await _seed_market(db, "mktD", status="open")
    await _seed_signal(db, "sigD", "mktD")
    await _seed_order(
        db, "ordD", signal_id="sigD", market_id="mktD",
        status="pending", submitted_at=int(time.time()) - 10,
    )
    await db.commit()

    summary = await reconcile_internal_state(db)
    assert summary["stuck_pending_orders"] == 0


@pytest.mark.asyncio
async def test_reconciliation_flags_unbalanced_arb_pair(db):
    """Two orders for same signal, only one filled, no position → flagged."""
    await _seed_market(db, "mktE1", status="open")
    await _seed_market(db, "mktE2", status="open")
    await _seed_signal(db, "sigE", "mktE1")
    await _seed_order(db, "ordE1", signal_id="sigE", market_id="mktE1",
                      status="filled", filled_price=0.40)
    await _seed_order(db, "ordE2", signal_id="sigE", market_id="mktE2",
                      status="failed")
    # No position row — arb never completed.
    await db.commit()

    summary = await reconcile_internal_state(db)
    assert summary["unbalanced_arb_pairs"] == 1

    cursor = await db.execute(
        "SELECT detail FROM reconciliation_log "
        "WHERE check_type='unbalanced_arb_pair'"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 1
    assert "sigE" in rows[0]["detail"]


@pytest.mark.asyncio
async def test_reconciliation_skips_balanced_arb_pair(db):
    """Two orders filled and a position written → not flagged."""
    await _seed_market(db, "mktF1", status="open")
    await _seed_market(db, "mktF2", status="open")
    await _seed_signal(db, "sigF", "mktF1")
    await _seed_order(db, "ordF1", signal_id="sigF", market_id="mktF1",
                      status="filled", filled_price=0.40)
    await _seed_order(db, "ordF2", signal_id="sigF", market_id="mktF2",
                      status="filled", filled_price=0.55)
    await _seed_position(db, "posF", signal_id="sigF", market_id="mktF1")
    await db.commit()

    summary = await reconcile_internal_state(db)
    assert summary["unbalanced_arb_pairs"] == 0
