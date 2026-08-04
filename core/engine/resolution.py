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
import uuid
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
               p.fees_paid, m.status, m.outcome, m.outcome_value,
               p.signal_id, p.strategy, p.opened_at
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
            signal_id,
            strategy,
            opened_at,
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
            update_cursor = await db.execute(
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
                 WHERE id = ? AND status = 'open'
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
            if update_cursor.rowcount == 0:
                continue
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
            if signal_id and strategy:
                holding_period_ms = None
                if opened_at:
                    try:
                        now_dt = datetime.fromisoformat(now)
                        opened_dt = datetime.fromisoformat(opened_at)
                        if opened_dt.tzinfo is None:
                            opened_dt = opened_dt.replace(tzinfo=timezone.utc)
                        holding_period_ms = int(
                            (now_dt - opened_dt).total_seconds() * 1000
                        )
                    except Exception:
                        pass
                try:
                    await db.execute(
                        """INSERT OR IGNORE INTO trade_outcomes
                           (id, signal_id, strategy, market_id_a,
                            actual_pnl, fees_total, holding_period_ms,
                            resolved_at, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            f"trade_{uuid.uuid4().hex[:12]}",
                            signal_id,
                            strategy,
                            market_id,
                            realized_pnl,
                            fees_paid or 0,
                            holding_period_ms,
                            now,
                            now,
                        ),
                    )
                except Exception as _e:
                    logger.debug(
                        "resolution: trade_outcomes insert failed pos=%s: %s",
                        pos_id,
                        _e,
                    )
        except Exception as e:
            logger.exception(
                "resolution: failed to close pos=%s market=%s err=%s",
                pos_id,
                market_id,
                e,
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
