"""
Database queries for PnL tracking and trade analysis.
"""

from typing import Any
from datetime import datetime, timezone
import logging

from ..db import Database

logger = logging.getLogger(__name__)


async def insert_snapshot(
    db: Database,
    total_capital: float,
    cash: float,
    open_positions_count: int,
    open_notional: float | None = None,
    unrealized_pnl: float | None = None,
    realized_pnl_today: float | None = None,
    realized_pnl_total: float | None = None,
    fees_today: float | None = None,
    fees_total: float | None = None,
    snapshot_type: str = "scheduled",
    pnl_constraint_arb: float | None = None,
    pnl_event_model: float | None = None,
    pnl_calibration: float | None = None,
    pnl_liquidity: float | None = None,
    pnl_latency: float | None = None,
    capital_polymarket: float | None = None,
    capital_kalshi: float | None = None,
) -> int:
    """
    Insert a PnL snapshot.

    Args:
        db: Database instance
        total_capital: Total capital
        cash: Cash on hand
        open_positions_count: Number of open positions
        open_notional: Total notional exposure
        unrealized_pnl: Unrealized PnL
        realized_pnl_today: Realized PnL today
        realized_pnl_total: Total realized PnL
        fees_today: Fees paid today
        fees_total: Total fees paid
        snapshot_type: Type of snapshot (scheduled, manual, signal_fired)
        pnl_constraint_arb: PnL from constraint arbitrage
        pnl_event_model: PnL from event model
        pnl_calibration: PnL from calibration strategy
        pnl_liquidity: PnL from liquidity rebates
        pnl_latency: PnL from latency strategies
        capital_polymarket: Capital on Polymarket
        capital_kalshi: Capital on Kalshi

    Returns:
        Row ID
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO pnl_snapshots (
        snapshot_type, total_capital, cash, open_positions_count,
        open_notional, unrealized_pnl, realized_pnl_today, realized_pnl_total,
        fees_today, fees_total, pnl_constraint_arb, pnl_event_model,
        pnl_calibration, pnl_liquidity, pnl_latency,
        capital_polymarket, capital_kalshi, snapshotted_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        snapshot_type,
        total_capital,
        cash,
        open_positions_count,
        open_notional,
        unrealized_pnl,
        realized_pnl_today,
        realized_pnl_total,
        fees_today,
        fees_total,
        pnl_constraint_arb,
        pnl_event_model,
        pnl_calibration,
        pnl_liquidity,
        pnl_latency,
        capital_polymarket,
        capital_kalshi,
        now,
    )

    return await db.execute(sql, params)


async def get_latest_snapshot(
    db: Database,
) -> dict[str, Any] | None:
    """
    Get the most recent PnL snapshot.

    Args:
        db: Database instance

    Returns:
        Latest snapshot record or None
    """
    sql = """
    SELECT * FROM pnl_snapshots
    ORDER BY snapshotted_at DESC
    LIMIT 1
    """

    return await db.fetch_one(sql)


async def get_daily_snapshots(
    db: Database,
    days: int = 7,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """
    Get snapshots from the last N days.

    Args:
        db: Database instance
        days: Number of days to look back
        limit: Maximum results

    Returns:
        List of snapshot records
    """
    sql = """
    SELECT * FROM pnl_snapshots
    WHERE datetime(snapshotted_at) > datetime('now', '-' || ? || ' days')
    ORDER BY snapshotted_at DESC
    LIMIT ?
    """

    return await db.fetch_all(sql, (days, limit))


async def get_snapshots_by_type(
    db: Database,
    snapshot_type: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Get snapshots of a specific type.

    Args:
        db: Database instance
        snapshot_type: Type of snapshot
        limit: Maximum results

    Returns:
        List of snapshot records
    """
    sql = """
    SELECT * FROM pnl_snapshots
    WHERE snapshot_type = ?
    ORDER BY snapshotted_at DESC
    LIMIT ?
    """

    return await db.fetch_all(sql, (snapshot_type, limit))


async def insert_trade_outcome(
    db: Database,
    trade_id: str,
    signal_id: str,
    strategy: str,
    market_id_a: str,
    predicted_edge: float | None = None,
    predicted_pnl: float | None = None,
    actual_pnl: float | None = None,
    fees_total: float | None = None,
    edge_captured_pct: float | None = None,
    signal_to_fill_ms: int | None = None,
    holding_period_ms: int | None = None,
    spread_at_signal: float | None = None,
    volume_at_signal: float | None = None,
    liquidity_at_signal: float | None = None,
    violation_id: str | None = None,
    market_id_b: str | None = None,
) -> str:
    """
    Insert a trade outcome record.

    Args:
        db: Database instance
        trade_id: Unique trade ID
        signal_id: Associated signal ID
        strategy: Strategy name
        market_id_a: Primary market ID
        predicted_edge: Predicted edge
        predicted_pnl: Predicted PnL
        actual_pnl: Actual PnL
        fees_total: Total fees
        edge_captured_pct: Percentage of predicted edge captured
        signal_to_fill_ms: Milliseconds from signal to fill
        holding_period_ms: Position holding period
        spread_at_signal: Spread at signal time
        volume_at_signal: Volume at signal time
        liquidity_at_signal: Liquidity at signal time
        violation_id: Associated violation ID
        market_id_b: Secondary market ID

    Returns:
        The trade_id
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO trade_outcomes (
        id, signal_id, strategy, violation_id,
        market_id_a, market_id_b, predicted_edge, predicted_pnl, actual_pnl,
        fees_total, edge_captured_pct, signal_to_fill_ms, holding_period_ms,
        spread_at_signal, volume_at_signal, liquidity_at_signal,
        resolved_at, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        trade_id,
        signal_id,
        strategy,
        violation_id,
        market_id_a,
        market_id_b,
        predicted_edge,
        predicted_pnl,
        actual_pnl,
        fees_total,
        edge_captured_pct,
        signal_to_fill_ms,
        holding_period_ms,
        spread_at_signal,
        volume_at_signal,
        liquidity_at_signal,
        now,
        now,
    )

    await db.execute(sql, params)
    return trade_id


async def get_trade_outcome(
    db: Database,
    trade_id: str,
) -> dict[str, Any] | None:
    """
    Retrieve a single trade outcome by ID.

    Args:
        db: Database instance
        trade_id: Trade ID

    Returns:
        Trade outcome record or None
    """
    sql = "SELECT * FROM trade_outcomes WHERE id = ?"
    return await db.fetch_one(sql, (trade_id,))


async def get_trade_outcomes_by_signal(
    db: Database,
    signal_id: str,
) -> list[dict[str, Any]]:
    """
    Get trade outcomes for a signal.

    Args:
        db: Database instance
        signal_id: Signal ID

    Returns:
        List of trade outcome records
    """
    sql = """
    SELECT * FROM trade_outcomes
    WHERE signal_id = ?
    ORDER BY resolved_at DESC
    """

    return await db.fetch_all(sql, (signal_id,))


async def get_strategy_pnl(
    db: Database,
    strategy: str,
    days: int = 7,
) -> dict[str, Any] | None:
    """
    Get PnL summary for a strategy over recent period.

    Args:
        db: Database instance
        strategy: Strategy name
        days: Look back this many days

    Returns:
        Dictionary with strategy PnL stats
    """
    sql = """
    SELECT
        COUNT(*) as total_trades,
        SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        SUM(CASE WHEN actual_pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
        SUM(actual_pnl) as total_pnl,
        AVG(actual_pnl) as avg_pnl,
        SUM(fees_total) as total_fees,
        SUM(CASE WHEN predicted_edge > 0 THEN predicted_edge ELSE 0 END) as total_predicted_edge,
        AVG(edge_captured_pct) as avg_edge_capture,
        AVG(signal_to_fill_ms) as avg_execution_time
    FROM trade_outcomes
    WHERE strategy = ?
    AND datetime(resolved_at) > datetime('now', '-' || ? || ' days')
    """

    return await db.fetch_one(sql, (strategy, days))


async def get_overall_pnl(
    db: Database,
    days: int = 7,
) -> dict[str, Any] | None:
    """
    Get overall PnL statistics.

    Args:
        db: Database instance
        days: Look back this many days

    Returns:
        Dictionary with overall PnL stats
    """
    sql = """
    SELECT
        COUNT(*) as total_trades,
        COUNT(DISTINCT strategy) as unique_strategies,
        SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
        SUM(CASE WHEN actual_pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
        SUM(actual_pnl) as total_pnl,
        AVG(actual_pnl) as avg_pnl,
        SUM(fees_total) as total_fees,
        MIN(actual_pnl) as worst_trade,
        MAX(actual_pnl) as best_trade
    FROM trade_outcomes
    WHERE datetime(resolved_at) > datetime('now', '-' || ? || ' days')
    """

    return await db.fetch_one(sql, (days,))


async def get_recent_trades(
    db: Database,
    strategy: str | None = None,
    limit: int = 100,
    days: int = 7,
) -> list[dict[str, Any]]:
    """
    Get recent completed trades.

    Args:
        db: Database instance
        strategy: Optional strategy filter
        limit: Maximum results
        days: Look back this many days

    Returns:
        List of trade outcome records
    """
    if strategy:
        sql = """
        SELECT * FROM trade_outcomes
        WHERE strategy = ?
        AND datetime(resolved_at) > datetime('now', '-' || ? || ' days')
        ORDER BY resolved_at DESC
        LIMIT ?
        """
        return await db.fetch_all(sql, (strategy, days, limit))
    else:
        sql = """
        SELECT * FROM trade_outcomes
        WHERE datetime(resolved_at) > datetime('now', '-' || ? || ' days')
        ORDER BY resolved_at DESC
        LIMIT ?
        """
        return await db.fetch_all(sql, (days, limit))


async def get_hourly_pnl_series(
    db: Database,
    hours: int = 24,
) -> list[dict[str, Any]]:
    """
    Get hourly PnL series for charting.

    Args:
        db: Database instance
        hours: Look back this many hours

    Returns:
        List of hourly PnL records
    """
    sql = """
    SELECT
        datetime(
            (julianday(snapshotted_at) - julianday('1970-01-01')) * 86400,
            '-' || (
                (
                    (julianday(snapshotted_at) - julianday('1970-01-01')) * 86400
                ) % 3600
            ) || ' seconds',
            'unixepoch'
        ) as hour,
        AVG(total_capital) as avg_capital,
        AVG(unrealized_pnl) as avg_unrealized_pnl,
        SUM(realized_pnl_today) as hourly_realized_pnl
    FROM pnl_snapshots
    WHERE datetime(snapshotted_at) > datetime('now', '-' || ? || ' hours')
    GROUP BY hour
    ORDER BY hour DESC
    """

    return await db.fetch_all(sql, (hours,))
