"""
Market resolution pass.

Closes open positions whose underlying markets have resolved. The ingestor
sets `markets.status='resolved'` (with `outcome` / `outcome_value`) when a
market settles. This pass walks open positions, matches them against those
resolved markets, computes realized PnL at the settlement price, and marks
the position closed with `resolution_outcome` set.

Call `close_resolved_positions(db)` periodically. It commits its own writes.
"""

import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)


async def close_resolved_positions(db: aiosqlite.Connection) -> dict[str, float]:
    """Close open positions for markets that have resolved.

    Returns a summary dict: {"checked": N, "closed": M, "total_pnl": X}.
    Positions whose market is still open are left alone.
    """
    summary: dict[str, float] = {"checked": 0.0, "closed": 0.0, "total_pnl": 0.0}

    cursor = await db.execute("""
        SELECT p.id, p.market_id, p.side, p.entry_price, p.entry_size,
               p.fees_paid, m.status, m.outcome, m.outcome_value
        FROM positions p
        JOIN markets m ON m.id = p.market_id
        WHERE p.status = 'open'
          AND m.status IN ('resolved', 'closed')
        """)
    rows = list(await cursor.fetchall())
    summary["checked"] = float(len(rows))

    if not rows:
        return summary

    now = datetime.now(timezone.utc).isoformat()
    closed = 0
    total_pnl = 0.0

    for row in rows:
        (
            pos_id,
            market_id,
            side,
            entry_price,
            entry_size,
            fees_paid,
            _status,
            outcome,
            outcome_value,
        ) = row

        # Settlement price: prefer outcome_value (explicit payout),
        # else interpret the outcome label (YES→1.0, NO→0.0).
        if outcome_value is not None:
            exit_price = float(outcome_value)
        elif isinstance(outcome, str):
            exit_price = 1.0 if outcome.strip().lower() in ("yes", "y", "1") else 0.0
        else:
            logger.warning(
                "resolution: market %s resolved but outcome is unknown — skipping pos %s",
                market_id,
                pos_id,
            )
            continue

        if side == "BUY":
            realized_pnl = round(
                (exit_price - entry_price) * entry_size - (fees_paid or 0), 4
            )
        else:
            realized_pnl = round(
                (entry_price - exit_price) * entry_size - (fees_paid or 0), 4
            )

        try:
            await db.execute(
                """
                UPDATE positions
                   SET status = 'closed',
                       exit_price = ?,
                       exit_size = ?,
                       realized_pnl = ?,
                       current_price = ?,
                       resolution_outcome = ?,
                       closed_at = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    exit_price,
                    entry_size,
                    realized_pnl,
                    exit_price,
                    outcome,
                    now,
                    now,
                    pos_id,
                ),
            )
            closed += 1
            total_pnl += realized_pnl
            logger.info(
                "RESOLUTION_CLOSE pos=%s market=%s outcome=%s exit=%.4f pnl=%.4f",
                pos_id,
                market_id,
                outcome,
                exit_price,
                realized_pnl,
            )
        except Exception as e:
            logger.error(
                "resolution: failed to close pos=%s market=%s err=%s",
                pos_id,
                market_id,
                e,
                exc_info=True,
            )

    if closed:
        try:
            await db.commit()
        except Exception as e:
            logger.warning("resolution commit failed: %s", e)
        logger.info(
            "close_resolved_positions: closed %d positions, total_pnl=%.4f",
            closed,
            total_pnl,
        )

    summary["closed"] = float(closed)
    summary["total_pnl"] = round(total_pnl, 4)
    return summary
