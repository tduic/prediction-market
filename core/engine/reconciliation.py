"""
Internal state reconciliation for the trading system.

Runs DB-level consistency checks between the `orders`, `positions`, and
`trade_outcomes` tables and logs any discrepancies to `reconciliation_log`
so operators can investigate.

Scope is intentionally internal (DB ↔ DB) rather than DB ↔ exchange:
the exchange API paths already write through BaseExecutionClient, so the
place drift is most likely to appear is in our own write ordering — e.g.
an arb where one leg filled and the other didn't (position never written),
or an order left in `pending` because a crash happened before the update.

Call `reconcile_internal_state(db)` periodically (every N strategy
cycles). It commits its own writes.
"""

import logging
import time
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)

# Pending orders older than this are considered stuck.
STUCK_PENDING_THRESHOLD_S = 300


async def reconcile_internal_state(db: aiosqlite.Connection) -> dict[str, int]:
    """Run all internal reconciliation checks.

    Returns a summary dict with the number of discrepancies found per
    check_type. Each discrepancy is also written to `reconciliation_log`
    with status='discrepancy' so it can be reviewed.
    """
    summary: dict[str, int] = {
        "orphaned_positions": 0,
        "stuck_pending_orders": 0,
        "unbalanced_arb_pairs": 0,
    }

    summary["orphaned_positions"] = await _check_orphaned_positions(db)
    summary["stuck_pending_orders"] = await _check_stuck_pending_orders(db)
    summary["unbalanced_arb_pairs"] = await _check_unbalanced_arb_pairs(db)

    try:
        await db.commit()
    except Exception as e:
        logger.warning("reconciliation commit failed: %s", e)

    total = sum(summary.values())
    if total:
        logger.warning("RECONCILIATION found %d discrepancies: %s", total, summary)
    else:
        logger.info("RECONCILIATION clean: no discrepancies")

    return summary


async def _check_orphaned_positions(db: aiosqlite.Connection) -> int:
    """Open positions whose backing order(s) are failed or missing.

    If an order is 'failed' or 'cancelled' but a position is still marked
    'open' for the same signal_id + market_id, that's state drift.
    """
    cursor = await db.execute("""
        SELECT p.id, p.signal_id, p.market_id,
               (SELECT o.status FROM orders o
                 WHERE o.signal_id = p.signal_id AND o.market_id = p.market_id
                 ORDER BY o.submitted_at DESC LIMIT 1) AS order_status
        FROM positions p
        WHERE p.status = 'open'
        """)
    rows = await cursor.fetchall()
    count = 0
    for pos_id, signal_id, market_id, order_status in rows:
        if order_status is None or order_status in ("failed", "cancelled"):
            await _log_discrepancy(
                db,
                platform="internal",
                check_type="orphaned_position",
                local_value=1.0,
                exchange_value=0.0,
                discrepancy=1.0,
                status="discrepancy",
                detail=(
                    f"position_id={pos_id} signal_id={signal_id} "
                    f"market_id={market_id} order_status={order_status!r}"
                ),
            )
            count += 1
    return count


async def _check_stuck_pending_orders(db: aiosqlite.Connection) -> int:
    """Orders in 'pending' state past the stuck threshold.

    `submitted_at` is stored as TEXT but arb_engine/base client write it
    as str(int(time.time())) — so CAST to INTEGER works for comparison.
    """
    cutoff = int(time.time()) - STUCK_PENDING_THRESHOLD_S
    cursor = await db.execute(
        """
        SELECT id, platform, signal_id, market_id, submitted_at
        FROM orders
        WHERE status = 'pending'
          AND CAST(submitted_at AS INTEGER) < ?
        """,
        (cutoff,),
    )
    rows = await cursor.fetchall()
    count = 0
    for order_id, platform, signal_id, market_id, submitted_at in rows:
        try:
            age_s = int(time.time()) - int(submitted_at)
        except (TypeError, ValueError):
            age_s = -1
        await _log_discrepancy(
            db,
            platform=platform or "unknown",
            check_type="stuck_pending_order",
            local_value=float(age_s),
            exchange_value=None,
            discrepancy=float(age_s),
            status="discrepancy",
            detail=(
                f"order_id={order_id} signal_id={signal_id} "
                f"market_id={market_id} age_s={age_s}"
            ),
        )
        count += 1
    return count


async def _check_unbalanced_arb_pairs(db: aiosqlite.Connection) -> int:
    """Arb signals where only one leg filled but no position was written.

    Arb trades write two orders under the same signal_id. When both fill,
    arb_engine writes a positions row. If exactly one order filled (or
    partially_filled) and no position exists for that signal, one leg is
    exposed on-exchange without a hedge — that's the case the in-process
    UNBALANCED_ARB log in arb_engine warns about, and we record it here
    so it persists past the log ring buffer.
    """
    cursor = await db.execute("""
        SELECT o.signal_id,
               SUM(CASE WHEN o.status IN ('filled','partially_filled') THEN 1 ELSE 0 END) AS filled_count,
               COUNT(*) AS leg_count
        FROM orders o
        WHERE o.signal_id IN (
            SELECT signal_id FROM orders GROUP BY signal_id HAVING COUNT(*) = 2
        )
        GROUP BY o.signal_id
        HAVING filled_count = 1
        """)
    rows = await cursor.fetchall()
    count = 0
    for signal_id, filled_count, leg_count in rows:
        # Skip if a position was written under this signal (balanced case).
        pos_cursor = await db.execute(
            "SELECT 1 FROM positions WHERE signal_id = ? LIMIT 1",
            (signal_id,),
        )
        if await pos_cursor.fetchone():
            continue
        await _log_discrepancy(
            db,
            platform="cross_platform",
            check_type="unbalanced_arb_pair",
            local_value=float(filled_count),
            exchange_value=float(leg_count),
            discrepancy=float(leg_count - filled_count),
            status="discrepancy",
            detail=(
                f"signal_id={signal_id} filled_legs={filled_count} "
                f"total_legs={leg_count} — one side open without hedge"
            ),
        )
        count += 1
    return count


async def _log_discrepancy(
    db: aiosqlite.Connection,
    *,
    platform: str,
    check_type: str,
    local_value: float,
    exchange_value: float | None,
    discrepancy: float | None,
    status: str,
    detail: str,
    action_taken: str | None = None,
) -> None:
    """Insert one row into reconciliation_log."""
    try:
        await db.execute(
            """
            INSERT INTO reconciliation_log (
                platform, check_type, local_value, exchange_value,
                discrepancy, status, detail, action_taken, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                platform,
                check_type,
                local_value,
                exchange_value,
                discrepancy,
                status,
                detail,
                action_taken,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    except Exception as e:
        logger.error(
            "Failed to write reconciliation_log row: check_type=%s err=%s",
            check_type,
            e,
        )
