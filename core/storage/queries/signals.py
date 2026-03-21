"""
Database queries for signal management and risk tracking.
"""

from typing import Optional, List, Any
from datetime import datetime, timezone
import logging

from ..db import Database

logger = logging.getLogger(__name__)


async def insert_signal(
    db: Database,
    signal_id: str,
    strategy: str,
    signal_type: str,
    market_id_a: str,
    market_id_b: str | None,
    model_edge: float,
    kelly_fraction: float,
    position_size_a: float,
    position_size_b: float | None,
    total_capital_at_risk: float,
    violation_id: str | None = None,
    target_price_a: float | None = None,
    target_price_b: float | None = None,
    model_fair_value: float | None = None,
    risk_check_passed: int = 1,
    daily_loss_limit_remaining: float | None = None,
    portfolio_exposure_pct: float | None = None,
    status: str = "queued",
) -> str:
    """
    Insert a trading signal.

    Args:
        db: Database instance
        signal_id: Unique signal ID
        strategy: Strategy that generated signal
        signal_type: Signal type (market_pair, single_market, etc.)
        market_id_a: First market ID
        market_id_b: Second market ID (for pairs)
        model_edge: Predicted edge
        kelly_fraction: Kelly fraction used for sizing
        position_size_a: Sized position for market A
        position_size_b: Sized position for market B
        total_capital_at_risk: Total capital at risk
        violation_id: Associated violation ID
        target_price_a: Target price for market A
        target_price_b: Target price for market B
        model_fair_value: Fair value estimate
        risk_check_passed: Whether risk checks passed (1/0)
        daily_loss_limit_remaining: Remaining daily loss limit
        portfolio_exposure_pct: Portfolio exposure percentage
        status: Signal status

    Returns:
        The signal_id
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO signals (
        id, strategy, signal_type, market_id_a, market_id_b,
        model_edge, kelly_fraction, position_size_a, position_size_b,
        total_capital_at_risk, violation_id, target_price_a, target_price_b,
        model_fair_value, risk_check_passed, daily_loss_limit_remaining,
        portfolio_exposure_pct, status, fired_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        signal_id,
        strategy,
        signal_type,
        market_id_a,
        market_id_b,
        model_edge,
        kelly_fraction,
        position_size_a,
        position_size_b,
        total_capital_at_risk,
        violation_id,
        target_price_a,
        target_price_b,
        model_fair_value,
        risk_check_passed,
        daily_loss_limit_remaining,
        portfolio_exposure_pct,
        status,
        now,
        now,
    )

    await db.execute(sql, params)
    return signal_id


async def get_signal(
    db: Database,
    signal_id: str,
) -> dict[str, Any] | None:
    """
    Retrieve a single signal by ID.

    Args:
        db: Database instance
        signal_id: Signal ID

    Returns:
        Signal record or None
    """
    sql = "SELECT * FROM signals WHERE id = ?"
    return await db.fetch_one(sql, (signal_id,))


async def update_signal_status(
    db: Database,
    signal_id: str,
    status: str,
) -> None:
    """
    Update signal status.

    Args:
        db: Database instance
        signal_id: Signal ID
        status: New status (queued, submitted, filled, cancelled, etc.)
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    UPDATE signals
    SET status = ?, updated_at = ?
    WHERE id = ?
    """

    await db.execute(sql, (status, now, signal_id))


async def get_recent_signals(
    db: Database,
    strategy: str | None = None,
    status: str | None = None,
    limit: int = 100,
    minutes: int = 60,
) -> list[dict[str, Any]]:
    """
    Get recent signals, optionally filtered.

    Args:
        db: Database instance
        strategy: Optional strategy filter
        status: Optional status filter
        limit: Maximum results
        minutes: Look back this many minutes

    Returns:
        List of signal records
    """
    where_clauses = ["datetime(fired_at) > datetime('now', '-' || ? || ' minutes')"]
    params = [minutes]

    if strategy:
        where_clauses.append("strategy = ?")
        params.append(strategy)

    if status:
        where_clauses.append("status = ?")
        params.append(status)

    where_sql = " AND ".join(where_clauses)

    sql = f"""
    SELECT * FROM signals
    WHERE {where_sql}
    ORDER BY fired_at DESC
    LIMIT ?
    """

    params.append(limit)
    return await db.fetch_all(sql, tuple(params))


async def get_signals_by_violation(
    db: Database,
    violation_id: str,
) -> list[dict[str, Any]]:
    """
    Get all signals associated with a violation.

    Args:
        db: Database instance
        violation_id: Violation ID

    Returns:
        List of signal records
    """
    sql = """
    SELECT * FROM signals
    WHERE violation_id = ?
    ORDER BY fired_at DESC
    """

    return await db.fetch_all(sql, (violation_id,))


async def get_open_signals(
    db: Database,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """
    Get all open/unfilled signals.

    Args:
        db: Database instance
        limit: Maximum results

    Returns:
        List of signal records with status in (queued, submitted)
    """
    sql = """
    SELECT * FROM signals
    WHERE status IN ('queued', 'submitted')
    ORDER BY fired_at DESC
    LIMIT ?
    """

    return await db.fetch_all(sql, (limit,))


async def insert_risk_check(
    db: Database,
    signal_id: str,
    check_type: str,
    passed: int,
    check_value: float | None = None,
    threshold: float | None = None,
    detail: str | None = None,
    violation_id: str | None = None,
) -> int:
    """
    Insert a risk check log entry.

    Args:
        db: Database instance
        signal_id: Signal ID
        check_type: Type of check (position_size, daily_loss, etc.)
        passed: Whether check passed (1/0)
        check_value: The value being checked
        threshold: Threshold for the check
        detail: Additional details
        violation_id: Associated violation ID

    Returns:
        Row ID
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO risk_check_log (
        signal_id, violation_id, check_type, passed, check_value,
        threshold, detail, evaluated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        signal_id,
        violation_id,
        check_type,
        passed,
        check_value,
        threshold,
        detail,
        now,
    )

    return await db.execute(sql, params)


async def get_risk_checks_for_signal(
    db: Database,
    signal_id: str,
) -> list[dict[str, Any]]:
    """
    Get all risk checks performed for a signal.

    Args:
        db: Database instance
        signal_id: Signal ID

    Returns:
        List of risk check records
    """
    sql = """
    SELECT * FROM risk_check_log
    WHERE signal_id = ?
    ORDER BY evaluated_at DESC
    """

    return await db.fetch_all(sql, (signal_id,))


async def get_failed_risk_checks(
    db: Database,
    limit: int = 100,
    minutes: int = 60,
) -> list[dict[str, Any]]:
    """
    Get recent failed risk checks.

    Args:
        db: Database instance
        limit: Maximum results
        minutes: Look back this many minutes

    Returns:
        List of failed risk check records
    """
    sql = """
    SELECT * FROM risk_check_log
    WHERE passed = 0
    AND datetime(evaluated_at) > datetime('now', '-' || ? || ' minutes')
    ORDER BY evaluated_at DESC
    LIMIT ?
    """

    return await db.fetch_all(sql, (minutes, limit))


async def get_signal_count(
    db: Database,
    strategy: str | None = None,
    status: str | None = None,
) -> int:
    """
    Count signals, optionally filtered.

    Args:
        db: Database instance
        strategy: Optional strategy filter
        status: Optional status filter

    Returns:
        Signal count
    """
    where_clauses = []
    params = []

    if strategy:
        where_clauses.append("strategy = ?")
        params.append(strategy)

    if status:
        where_clauses.append("status = ?")
        params.append(status)

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    sql = f"SELECT COUNT(*) as count FROM signals WHERE {where_sql}"
    result = await db.fetch_one(sql, tuple(params) if params else ())

    return result["count"] if result else 0


async def get_signal_statistics(
    db: Database,
) -> dict[str, Any]:
    """
    Get summary statistics on signals.

    Args:
        db: Database instance

    Returns:
        Dictionary with signal statistics
    """
    sql = """
    SELECT
        COUNT(*) as total_signals,
        SUM(CASE WHEN status = 'queued' THEN 1 ELSE 0 END) as queued_signals,
        SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END) as submitted_signals,
        SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) as filled_signals,
        AVG(model_edge) as avg_edge,
        AVG(total_capital_at_risk) as avg_capital_at_risk,
        COUNT(DISTINCT strategy) as unique_strategies
    FROM signals
    """

    result = await db.fetch_one(sql)
    return result if result else {}
