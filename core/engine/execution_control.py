"""Persistent execution halt.

Tripped when the engine can no longer be sure what it holds: an order whose
fill state is unknown, or an arb where exactly one leg filled. While halted,
no new orders are sent by any strategy. The halt survives restarts and never
clears itself; an operator clears it with ``scripts/clear_halt.py`` after
reconciling positions on both venues.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite

from core.alerting import Severity, get_alert_manager

logger = logging.getLogger(__name__)


async def get_halt(db: aiosqlite.Connection) -> dict | None:
    """Return {"reason", "component", "halted_at"} when halted, else None."""
    try:
        cursor = await db.execute(
            "SELECT halted, reason, component, halted_at "
            "FROM execution_control WHERE id = 1"
        )
        row = await cursor.fetchone()
    except aiosqlite.OperationalError:
        logger.error("execution_control table missing — run migrations")
        return None
    if row is None or not row[0]:
        return None
    return {"reason": row[1], "component": row[2], "halted_at": row[3]}


async def is_halted(db: aiosqlite.Connection) -> bool:
    return await get_halt(db) is not None


async def halt(db: aiosqlite.Connection, reason: str, component: str) -> None:
    """Set the halt (idempotent — the first reason is kept) and alert."""
    now = datetime.now(timezone.utc).isoformat()
    cursor = await db.execute(
        """UPDATE execution_control
           SET halted = 1, reason = ?, component = ?, halted_at = ?,
               cleared_at = NULL, updated_at = ?
           WHERE id = 1 AND halted = 0""",
        (reason[:2000], component, now, now),
    )
    newly_halted = cursor.rowcount > 0
    if newly_halted:
        await db.execute(
            """INSERT INTO system_events
               (event_type, severity, component, detail, occurred_at)
               VALUES ('EXECUTION_HALTED', 'critical', ?, ?, ?)""",
            (component, reason[:4000], now),
        )
    await db.commit()
    if not newly_halted:
        logger.warning("Execution already halted; additional reason: %s", reason)
        return
    logger.critical("EXECUTION HALTED (%s): %s", component, reason)
    try:
        get_alert_manager().send_nowait(
            title="Execution halted",
            message=f"{reason[:500]} — clear with scripts/clear_halt.py after "
            "reconciling both venues",
            severity=Severity.CRITICAL,
            component=component,
        )
    except Exception:  # alerting must never mask the halt
        logger.exception("Failed to send execution-halt alert")


async def clear_halt(db: aiosqlite.Connection, reason: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """UPDATE execution_control
           SET halted = 0, cleared_at = ?, updated_at = ?
           WHERE id = 1""",
        (now, now),
    )
    await db.execute(
        """INSERT INTO system_events
           (event_type, severity, component, detail, occurred_at)
           VALUES ('EXECUTION_HALT_CLEARED', 'warning', 'operator', ?, ?)""",
        (reason[:4000], now),
    )
    await db.commit()
    logger.warning("Execution halt cleared: %s", reason)
