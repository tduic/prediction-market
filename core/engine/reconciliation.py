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
from datetime import datetime, timedelta, timezone

import aiosqlite

from core.config import get_config

logger = logging.getLogger(__name__)


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
        try:
            from core.alerting import Severity, get_alert_manager

            get_alert_manager().send_nowait(
                title="Reconciliation discrepancies detected",
                message=f"{total} discrepancies: {summary}",
                severity=Severity.WARNING,
                component="reconciliation",
            )
        except Exception as _alert_err:
            logger.error("Failed to send reconciliation alert: %s", _alert_err)
    else:
        logger.info("RECONCILIATION clean: no discrepancies")

    return summary


async def _check_orphaned_positions(db: aiosqlite.Connection) -> int:
    """Open positions whose backing order(s) are failed or missing.

    If an order is 'failed' or 'cancelled' but a position is still marked
    'open' for the same signal_id + market_id, that's state drift.
    """
    cursor = await db.execute("""
        WITH latest_order AS (
            SELECT signal_id, market_id, status,
                   ROW_NUMBER() OVER (
                       PARTITION BY signal_id, market_id
                       ORDER BY submitted_at DESC
                   ) AS rn
            FROM orders
        )
        SELECT p.id, p.signal_id, p.market_id, lo.status AS order_status
        FROM positions p
        LEFT JOIN latest_order lo
               ON lo.signal_id = p.signal_id
              AND lo.market_id = p.market_id
              AND lo.rn = 1
        WHERE p.status = 'open'
        """)
    rows = await cursor.fetchall()
    count = 0
    for pos_id, signal_id, market_id, order_status in rows:
        if order_status is None or order_status in ("failed", "cancelled"):
            order_status_label = (
                "<no_matching_order>" if order_status is None else order_status
            )
            detail = (
                f"position_id={pos_id} signal_id={signal_id} "
                f"market_id={market_id} order_status={order_status_label}"
            )
            if await _is_recently_logged(db, "orphaned_position", detail):
                continue
            await _log_discrepancy(
                db,
                platform="internal",
                check_type="orphaned_position",
                local_value=1.0,
                exchange_value=0.0,
                discrepancy=1.0,
                status="discrepancy",
                detail=detail,
            )
            count += 1
    return count


async def _check_stuck_pending_orders(db: aiosqlite.Connection) -> int:
    """Orders in 'pending' state past the stuck threshold.

    `submitted_at` is stored as TEXT but arb_engine/base client write it
    as str(int(time.time())) — a 10-digit decimal string. Text comparison
    of equal-length decimal strings is lexicographically equivalent to
    numeric comparison, so we compare directly to allow SQLite to use the
    idx_orders_submitted_at index instead of computing CAST on every row.
    """
    threshold_s = get_config().risk_controls.reconcile_stuck_pending_threshold_s
    cutoff_str = str(int(time.time()) - threshold_s)
    cursor = await db.execute(
        """
        SELECT id, platform, signal_id, market_id, submitted_at
        FROM orders
        WHERE status = 'pending'
          AND submitted_at IS NOT NULL
          AND submitted_at != ''
          AND submitted_at < ?
        """,
        (cutoff_str,),
    )
    rows = await cursor.fetchall()
    count = 0
    for order_id, platform, signal_id, market_id, submitted_at in rows:
        try:
            age_s = int(time.time()) - int(submitted_at)
        except (TypeError, ValueError):
            age_s = -1
        detail = (
            f"order_id={order_id} signal_id={signal_id} "
            f"market_id={market_id} age_s={age_s}"
        )
        if await _is_recently_logged(db, "stuck_pending_order", detail):
            continue
        await _log_discrepancy(
            db,
            platform=platform or "unknown",
            check_type="stuck_pending_order",
            local_value=float(age_s),
            exchange_value=None,
            discrepancy=float(age_s),
            status="discrepancy",
            detail=detail,
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

    The scan is bounded to the last 30 days via submitted_at to prevent a
    full-table scan of orders as the DB grows. submitted_at is stored as a
    10-digit decimal epoch string; text comparison of equal-length decimal
    strings is lexicographically equivalent to numeric comparison, so the
    index on submitted_at (idx_orders_submitted_at) is used here. Arbs
    older than 30 days have already been logged by _is_recently_logged
    (1-hour dedup window) many times; the 30-day boundary is acceptable.
    Note: a signal straddling the boundary (one leg older than 30 days,
    one newer) will not appear in the subquery's HAVING COUNT(*) >= 2 and
    will therefore be invisible — this is an accepted trade-off.
    """
    cutoff_30d_str = str(int(time.time()) - 30 * 86400)
    cursor = await db.execute(
        """
        WITH multi_leg_signals AS (
            SELECT signal_id FROM orders
            WHERE submitted_at > ?
            GROUP BY signal_id
            HAVING COUNT(*) >= 2
        )
        SELECT o.signal_id,
               SUM(CASE WHEN o.status IN ('filled','partially_filled') THEN 1 ELSE 0 END) AS filled_count,
               COUNT(*) AS leg_count,
               GROUP_CONCAT(
                   CASE WHEN o.status IN ('filled','partially_filled') THEN o.platform END
               ) AS filled_platforms
        FROM orders o
        JOIN multi_leg_signals mls ON o.signal_id = mls.signal_id
        WHERE o.submitted_at > ?
        GROUP BY o.signal_id
        HAVING filled_count = 1
          AND o.signal_id NOT IN (SELECT signal_id FROM positions WHERE signal_id IS NOT NULL)
        """,
        (cutoff_30d_str, cutoff_30d_str),
    )
    rows = await cursor.fetchall()
    count = 0
    for signal_id, filled_count, leg_count, filled_platforms in rows:
        platform_detail = (
            f" filled_platform={filled_platforms}" if filled_platforms else ""
        )
        detail = (
            f"signal_id={signal_id} filled_legs={filled_count} "
            f"total_legs={leg_count}{platform_detail} — one side open without hedge"
        )
        if await _is_recently_logged(db, "unbalanced_arb_pair", detail):
            continue
        await _log_discrepancy(
            db,
            platform="cross_platform",
            check_type="unbalanced_arb_pair",
            local_value=float(filled_count),
            exchange_value=float(leg_count),
            discrepancy=float(leg_count - filled_count),
            status="discrepancy",
            detail=detail,
        )
        count += 1
    return count


async def _is_recently_logged(
    db: aiosqlite.Connection,
    check_type: str,
    detail: str,
    window_s: int = 3600,
) -> bool:
    """Return True if a reconciliation_log row with the same check_type and
    detail string was already inserted within the last *window_s* seconds.

    This prevents the same orphaned/stuck/unbalanced discrepancy from being
    logged on every reconciliation cycle when reconcile_every is small.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_s)).isoformat()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM reconciliation_log "
        "WHERE check_type = ? AND detail = ? AND checked_at >= ?",
        (check_type, detail, cutoff),
    )
    row = await cursor.fetchone()
    return bool(row and row[0] > 0)


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
