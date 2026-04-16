"""
Database queries for position and order management.
"""

import logging
from datetime import datetime, timezone
from typing import Any

from ..db import Database

logger = logging.getLogger(__name__)


async def insert_order(
    db: Database,
    order_id: str,
    signal_id: str,
    platform: str,
    market_id: str,
    side: str,
    order_type: str,
    requested_size: float,
    requested_price: float | None = None,
    platform_order_id: str | None = None,
) -> str:
    """
    Insert an order record.

    Args:
        db: Database instance
        order_id: Unique order ID
        signal_id: Associated signal ID
        platform: Platform name
        market_id: Market ID
        side: BUY or SELL
        order_type: LIMIT, MARKET, etc.
        requested_size: Order size
        requested_price: Limit price (if applicable)
        platform_order_id: Platform-specific order ID

    Returns:
        The order_id
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO orders (
        id, signal_id, platform, platform_order_id, market_id,
        side, order_type, requested_price, requested_size,
        status, retry_count, submitted_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        order_id,
        signal_id,
        platform,
        platform_order_id,
        market_id,
        side,
        order_type,
        requested_price,
        requested_size,
        "pending",
        0,
        now,
        now,
    )

    await db.execute(sql, params)
    return order_id


async def get_order(
    db: Database,
    order_id: str,
) -> dict[str, Any] | None:
    """
    Retrieve a single order by ID.

    Args:
        db: Database instance
        order_id: Order ID

    Returns:
        Order record or None
    """
    sql = "SELECT * FROM orders WHERE id = ?"
    return await db.fetch_one(sql, (order_id,))


async def update_order(
    db: Database,
    order_id: str,
    filled_price: float | None = None,
    filled_size: float | None = None,
    slippage: float | None = None,
    fee_paid: float | None = None,
    status: str | None = None,
    platform_order_id: str | None = None,
    failure_reason: str | None = None,
    fill_latency_ms: int | None = None,
) -> None:
    """
    Update order record.

    Args:
        db: Database instance
        order_id: Order ID
        filled_price: Fill price
        filled_size: Fill size
        slippage: Slippage amount
        fee_paid: Fee paid
        status: New status
        platform_order_id: Platform order ID
        failure_reason: Reason for failure
        fill_latency_ms: Latency from submission to fill
    """
    now = datetime.now(timezone.utc).isoformat()

    updates: list[str] = []
    params: list[Any] = []

    if filled_price is not None:
        updates.append("filled_price = ?")
        params.append(filled_price)

    if filled_size is not None:
        updates.append("filled_size = ?")
        params.append(filled_size)

    if slippage is not None:
        updates.append("slippage = ?")
        params.append(slippage)

    if fee_paid is not None:
        updates.append("fee_paid = ?")
        params.append(fee_paid)

    if status is not None:
        updates.append("status = ?")
        params.append(status)

    if platform_order_id is not None:
        updates.append("platform_order_id = ?")
        params.append(platform_order_id)

    if failure_reason is not None:
        updates.append("failure_reason = ?")
        params.append(failure_reason)

    if fill_latency_ms is not None:
        updates.append("fill_latency_ms = ?")
        params.append(fill_latency_ms)

    updates.append("updated_at = ?")
    params.append(now)

    params.append(order_id)

    sql = f"UPDATE orders SET {', '.join(updates)} WHERE id = ?"
    await db.execute(sql, tuple(params))


async def get_orders_for_signal(
    db: Database,
    signal_id: str,
) -> list[dict[str, Any]]:
    """
    Get all orders for a signal.

    Args:
        db: Database instance
        signal_id: Signal ID

    Returns:
        List of order records
    """
    sql = """
    SELECT * FROM orders
    WHERE signal_id = ?
    ORDER BY submitted_at DESC
    """

    return await db.fetch_all(sql, (signal_id,))


async def get_pending_orders(
    db: Database,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """
    Get pending orders.

    Args:
        db: Database instance
        limit: Maximum results

    Returns:
        List of pending order records
    """
    sql = """
    SELECT * FROM orders
    WHERE status IN ('pending', 'submitted', 'partial')
    ORDER BY submitted_at ASC
    LIMIT ?
    """

    return await db.fetch_all(sql, (limit,))


async def insert_order_event(
    db: Database,
    order_id: str,
    event_type: str,
    price: float | None = None,
    size: float | None = None,
    detail: str | None = None,
) -> int:
    """
    Insert an order event (fill, cancel, etc.).

    Args:
        db: Database instance
        order_id: Order ID
        event_type: Event type (fill, cancel, reject, etc.)
        price: Price at event
        size: Size at event
        detail: Event details

    Returns:
        Row ID
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO order_events (
        order_id, event_type, price, size, detail, occurred_at
    ) VALUES (?, ?, ?, ?, ?, ?)
    """

    params = (order_id, event_type, price, size, detail, now)
    return await db.execute(sql, params)


async def get_order_events(
    db: Database,
    order_id: str,
) -> list[dict[str, Any]]:
    """
    Get event history for an order.

    Args:
        db: Database instance
        order_id: Order ID

    Returns:
        List of order event records
    """
    sql = """
    SELECT * FROM order_events
    WHERE order_id = ?
    ORDER BY occurred_at DESC
    """

    return await db.fetch_all(sql, (order_id,))


async def insert_position(
    db: Database,
    position_id: str,
    signal_id: str,
    market_id: str,
    strategy: str,
    side: str,
    entry_price: float,
    entry_size: float,
) -> str:
    """
    Insert a position record.

    Args:
        db: Database instance
        position_id: Unique position ID
        signal_id: Associated signal ID
        market_id: Market ID
        strategy: Strategy name
        side: BUY or SELL
        entry_price: Entry price
        entry_size: Entry size

    Returns:
        The position_id
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO positions (
        id, signal_id, market_id, strategy, side,
        entry_price, entry_size, status, opened_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        position_id,
        signal_id,
        market_id,
        strategy,
        side,
        entry_price,
        entry_size,
        "open",
        now,
        now,
    )

    await db.execute(sql, params)
    return position_id


async def get_position(
    db: Database,
    position_id: str,
) -> dict[str, Any] | None:
    """
    Retrieve a single position by ID.

    Args:
        db: Database instance
        position_id: Position ID

    Returns:
        Position record or None
    """
    sql = "SELECT * FROM positions WHERE id = ?"
    return await db.fetch_one(sql, (position_id,))


async def update_position(
    db: Database,
    position_id: str,
    current_price: float | None = None,
    unrealized_pnl: float | None = None,
) -> None:
    """
    Update position with current market data.

    Args:
        db: Database instance
        position_id: Position ID
        current_price: Current market price
        unrealized_pnl: Unrealized PnL
    """
    now = datetime.now(timezone.utc).isoformat()

    updates: list[str] = []
    params: list[Any] = []

    if current_price is not None:
        updates.append("current_price = ?")
        params.append(current_price)

    if unrealized_pnl is not None:
        updates.append("unrealized_pnl = ?")
        params.append(unrealized_pnl)

    updates.append("updated_at = ?")
    params.append(now)
    params.append(position_id)

    sql = f"UPDATE positions SET {', '.join(updates)} WHERE id = ?"
    await db.execute(sql, tuple(params))


async def close_position(
    db: Database,
    position_id: str,
    exit_price: float,
    exit_size: float,
    realized_pnl: float,
    fees_paid: float | None = None,
    resolution_outcome: str | None = None,
) -> None:
    """
    Close a position.

    Args:
        db: Database instance
        position_id: Position ID
        exit_price: Exit price
        exit_size: Exit size
        realized_pnl: Realized PnL
        fees_paid: Fees paid
        resolution_outcome: Market resolution outcome
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    UPDATE positions
    SET status = 'closed', exit_price = ?, exit_size = ?,
        realized_pnl = ?, fees_paid = ?, resolution_outcome = ?,
        closed_at = ?, updated_at = ?
    WHERE id = ?
    """

    params = (
        exit_price,
        exit_size,
        realized_pnl,
        fees_paid,
        resolution_outcome,
        now,
        now,
        position_id,
    )

    await db.execute(sql, params)


async def get_open_positions(
    db: Database,
    strategy: str | None = None,
) -> list[dict[str, Any]]:
    """
    Get all open positions.

    Args:
        db: Database instance
        strategy: Optional strategy filter

    Returns:
        List of open position records
    """
    if strategy:
        sql = """
        SELECT * FROM positions
        WHERE status = 'open' AND strategy = ?
        ORDER BY opened_at DESC
        """
        return await db.fetch_all(sql, (strategy,))
    else:
        sql = """
        SELECT * FROM positions
        WHERE status = 'open'
        ORDER BY opened_at DESC
        """
        return await db.fetch_all(sql)


async def get_positions_for_market(
    db: Database,
    market_id: str,
) -> list[dict[str, Any]]:
    """
    Get all positions for a market.

    Args:
        db: Database instance
        market_id: Market ID

    Returns:
        List of position records
    """
    sql = """
    SELECT * FROM positions
    WHERE market_id = ?
    ORDER BY opened_at DESC
    """

    return await db.fetch_all(sql, (market_id,))


async def get_position_count(
    db: Database,
    status: str | None = None,
) -> int:
    """
    Count positions, optionally by status.

    Args:
        db: Database instance
        status: Optional status filter

    Returns:
        Position count
    """
    if status:
        sql = "SELECT COUNT(*) as count FROM positions WHERE status = ?"
        result = await db.fetch_one(sql, (status,))
    else:
        sql = "SELECT COUNT(*) as count FROM positions"
        result = await db.fetch_one(sql)

    return result["count"] if result else 0
