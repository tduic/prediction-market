"""
Database queries for violation and arbitrage opportunity management.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
import logging

from ..db import Database

logger = logging.getLogger(__name__)


async def insert_violation(
    db: Database,
    violation_id: str,
    pair_id: str,
    violation_type: str,
    price_a_at_detect: float,
    price_b_at_detect: float,
    raw_spread: float,
    net_spread: float,
    fee_estimate_a: Optional[float] = None,
    fee_estimate_b: Optional[float] = None,
    status: str = "detected",
) -> str:
    """
    Insert a violation record.

    Args:
        db: Database instance
        violation_id: Unique violation ID
        pair_id: Market pair ID
        violation_type: Type of violation
        price_a_at_detect: Market A price at detection
        price_b_at_detect: Market B price at detection
        raw_spread: Raw spread (without fees)
        net_spread: Net spread (after fees)
        fee_estimate_a: Estimated fee for market A
        fee_estimate_b: Estimated fee for market B
        status: Violation status

    Returns:
        The violation_id
    """
    now = datetime.utcnow().isoformat()

    sql = """
    INSERT INTO violations (
        id, pair_id, violation_type,
        price_a_at_detect, price_b_at_detect,
        raw_spread, net_spread,
        fee_estimate_a, fee_estimate_b,
        status, detected_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        violation_id,
        pair_id,
        violation_type,
        price_a_at_detect,
        price_b_at_detect,
        raw_spread,
        net_spread,
        fee_estimate_a,
        fee_estimate_b,
        status,
        now,
        now,
    )

    await db.execute(sql, params)
    return violation_id


async def get_violation(
    db: Database,
    violation_id: str,
) -> Optional[Dict[str, Any]]:
    """
    Retrieve a single violation by ID.

    Args:
        db: Database instance
        violation_id: Violation ID

    Returns:
        Violation record or None
    """
    sql = "SELECT * FROM violations WHERE id = ?"
    return await db.fetch_one(sql, (violation_id,))


async def update_violation_status(
    db: Database,
    violation_id: str,
    status: str,
    rejection_reason: Optional[str] = None,
) -> None:
    """
    Update violation status.

    Args:
        db: Database instance
        violation_id: Violation ID
        status: New status (closed, rejected, etc.)
        rejection_reason: Optional reason for closure
    """
    now = datetime.utcnow().isoformat()

    sql = """
    UPDATE violations
    SET status = ?, rejection_reason = ?, updated_at = ?
    WHERE id = ?
    """

    await db.execute(sql, (status, rejection_reason, now, violation_id))


async def close_violation(
    db: Database,
    violation_id: str,
    closed_at: Optional[str] = None,
) -> None:
    """
    Close a violation and record duration.

    Args:
        db: Database instance
        violation_id: Violation ID
        closed_at: Closure time (defaults to now)
    """
    if closed_at is None:
        closed_at = datetime.utcnow().isoformat()

    # Get the violation to calculate duration
    sql = "SELECT detected_at FROM violations WHERE id = ?"
    result = await db.fetch_one(sql, (violation_id,))

    if not result:
        logger.warning(f"Violation {violation_id} not found")
        return

    detected_dt = datetime.fromisoformat(result["detected_at"])
    closed_dt = datetime.fromisoformat(closed_at)
    duration_ms = int((closed_dt - detected_dt).total_seconds() * 1000)

    sql = """
    UPDATE violations
    SET status = 'closed', closed_at = ?, duration_open_ms = ?, updated_at = ?
    WHERE id = ?
    """

    now = datetime.utcnow().isoformat()
    await db.execute(sql, (closed_at, duration_ms, now, violation_id))


async def get_active_violations(
    db: Database,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """
    Get all active (detected) violations.

    Args:
        db: Database instance
        limit: Maximum results

    Returns:
        List of violation records with status='detected'
    """
    sql = """
    SELECT * FROM violations
    WHERE status = 'detected'
    ORDER BY detected_at DESC
    LIMIT ?
    """

    return await db.fetch_all(sql, (limit,))


async def get_violations_by_pair(
    db: Database,
    pair_id: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Get all violations for a specific market pair.

    Args:
        db: Database instance
        pair_id: Market pair ID
        limit: Maximum results

    Returns:
        List of violation records
    """
    sql = """
    SELECT * FROM violations
    WHERE pair_id = ?
    ORDER BY detected_at DESC
    LIMIT ?
    """

    return await db.fetch_all(sql, (pair_id, limit))


async def get_violations_by_status(
    db: Database,
    status: str,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """
    Get violations with a specific status.

    Args:
        db: Database instance
        status: Violation status to filter
        limit: Maximum results

    Returns:
        List of violation records
    """
    sql = """
    SELECT * FROM violations
    WHERE status = ?
    ORDER BY detected_at DESC
    LIMIT ?
    """

    return await db.fetch_all(sql, (status, limit))


async def get_violation_count(
    db: Database,
    status: Optional[str] = None,
) -> int:
    """
    Count violations, optionally by status.

    Args:
        db: Database instance
        status: Optional status filter

    Returns:
        Violation count
    """
    if status:
        sql = "SELECT COUNT(*) as count FROM violations WHERE status = ?"
        result = await db.fetch_one(sql, (status,))
    else:
        sql = "SELECT COUNT(*) as count FROM violations"
        result = await db.fetch_one(sql)

    return result["count"] if result else 0


async def insert_pair_spread_history(
    db: Database,
    pair_id: str,
    price_a: float,
    price_b: float,
    raw_spread: float,
    net_spread: float,
    constraint_satisfied: Optional[int] = None,
) -> int:
    """
    Insert a pair spread history record.

    Args:
        db: Database instance
        pair_id: Market pair ID
        price_a: Market A price
        price_b: Market B price
        raw_spread: Raw spread
        net_spread: Net spread
        constraint_satisfied: Whether constraint was satisfied (1/0)

    Returns:
        Row ID
    """
    now = datetime.utcnow().isoformat()

    sql = """
    INSERT INTO pair_spread_history (
        pair_id, price_a, price_b, raw_spread, net_spread,
        constraint_satisfied, evaluated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        pair_id,
        price_a,
        price_b,
        raw_spread,
        net_spread,
        constraint_satisfied,
        now,
    )

    return await db.execute(sql, params)


async def get_pair_spread_history(
    db: Database,
    pair_id: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """
    Get spread history for a pair.

    Args:
        db: Database instance
        pair_id: Market pair ID
        limit: Maximum results

    Returns:
        List of spread history records
    """
    sql = """
    SELECT * FROM pair_spread_history
    WHERE pair_id = ?
    ORDER BY evaluated_at DESC
    LIMIT ?
    """

    return await db.fetch_all(sql, (pair_id, limit))


async def get_max_spread_in_period(
    db: Database,
    pair_id: str,
    minutes: int = 60,
) -> Optional[Dict[str, Any]]:
    """
    Get the record with maximum net spread in recent period.

    Args:
        db: Database instance
        pair_id: Market pair ID
        minutes: Look back this many minutes

    Returns:
        Record with max spread or None
    """
    sql = """
    SELECT * FROM pair_spread_history
    WHERE pair_id = ?
    AND datetime(evaluated_at) > datetime('now', '-' || ? || ' minutes')
    ORDER BY net_spread DESC
    LIMIT 1
    """

    return await db.fetch_one(sql, (pair_id, minutes))


async def get_violation_statistics(
    db: Database,
) -> Dict[str, Any]:
    """
    Get summary statistics on violations.

    Args:
        db: Database instance

    Returns:
        Dictionary with violation statistics
    """
    sql = """
    SELECT
        COUNT(*) as total_violations,
        SUM(CASE WHEN status = 'detected' THEN 1 ELSE 0 END) as active_violations,
        SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_violations,
        AVG(CASE WHEN duration_open_ms IS NOT NULL THEN duration_open_ms ELSE NULL END) as avg_open_duration_ms,
        AVG(net_spread) as avg_net_spread,
        MAX(net_spread) as max_net_spread,
        MIN(net_spread) as min_net_spread
    FROM violations
    """

    result = await db.fetch_one(sql)
    return result if result else {}
