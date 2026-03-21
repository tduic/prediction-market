"""
Database queries for market data operations.
All functions are async and take a Database instance as first argument.
"""

from typing import Any
from datetime import datetime, timezone
import logging

from ..db import Database

logger = logging.getLogger(__name__)


async def upsert_market(
    db: Database,
    market_id: str,
    platform: str,
    platform_id: str,
    title: str,
    description: str | None = None,
    category: str | None = None,
    event_type: str | None = None,
    resolution_source: str | None = None,
    resolution_criteria: str | None = None,
    close_time: str | None = None,
    resolve_time: str | None = None,
    status: str = "open",
) -> str:
    """
    Insert or update a market record.

    Args:
        db: Database instance
        market_id: Unique market identifier
        platform: Platform name (polymarket, kalshi, etc.)
        platform_id: Platform-specific market ID
        title: Market title
        description: Market description
        category: Market category
        event_type: Event type
        resolution_source: Resolution data source
        resolution_criteria: Resolution criteria
        close_time: Market close time
        resolve_time: Market resolution time
        status: Market status (open, closed, resolved, etc.)

    Returns:
        The market_id
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO markets (
        id, platform, platform_id, title, description, category,
        event_type, resolution_source, resolution_criteria,
        close_time, resolve_time, status, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(platform, platform_id) DO UPDATE SET
        title = excluded.title,
        description = excluded.description,
        category = excluded.category,
        event_type = excluded.event_type,
        resolution_source = excluded.resolution_source,
        resolution_criteria = excluded.resolution_criteria,
        close_time = excluded.close_time,
        resolve_time = excluded.resolve_time,
        status = excluded.status,
        updated_at = ?
    """

    params = (
        market_id,
        platform,
        platform_id,
        title,
        description,
        category,
        event_type,
        resolution_source,
        resolution_criteria,
        close_time,
        resolve_time,
        status,
        now,
        now,
        now,
    )

    await db.execute(sql, params)
    return market_id


async def get_market(
    db: Database,
    market_id: str,
) -> dict[str, Any] | None:
    """
    Retrieve a single market by ID.

    Args:
        db: Database instance
        market_id: Market ID to retrieve

    Returns:
        Market record as dictionary or None
    """
    sql = "SELECT * FROM markets WHERE id = ?"
    return await db.fetch_one(sql, (market_id,))


async def get_markets_by_platform(
    db: Database,
    platform: str,
    status: str | None = None,
    limit: int = 1000,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    Retrieve markets for a specific platform.

    Args:
        db: Database instance
        platform: Platform name
        status: Optional status filter
        limit: Maximum results
        offset: Result offset

    Returns:
        List of market records
    """
    if status:
        sql = """
        SELECT * FROM markets
        WHERE platform = ? AND status = ?
        ORDER BY updated_at DESC
        LIMIT ? OFFSET ?
        """
        params = (platform, status, limit, offset)
    else:
        sql = """
        SELECT * FROM markets
        WHERE platform = ?
        ORDER BY updated_at DESC
        LIMIT ? OFFSET ?
        """
        params = (platform, limit, offset)

    return await db.fetch_all(sql, params)


async def insert_price(
    db: Database,
    market_id: str,
    yes_price: float | None = None,
    no_price: float | None = None,
    spread: float | None = None,
    volume_24h: float | None = None,
    open_interest: float | None = None,
    liquidity: float | None = None,
    poll_latency_ms: int | None = None,
) -> int:
    """
    Insert a market price record.

    Args:
        db: Database instance
        market_id: Market ID
        yes_price: YES outcome price
        no_price: NO outcome price
        spread: Bid-ask spread
        volume_24h: 24h trading volume
        open_interest: Open interest
        liquidity: Liquidity measure
        poll_latency_ms: Polling latency in milliseconds

    Returns:
        Row ID of inserted record
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO market_prices (
        market_id, yes_price, no_price, spread,
        volume_24h, open_interest, liquidity, poll_latency_ms, polled_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        market_id,
        yes_price,
        no_price,
        spread,
        volume_24h,
        open_interest,
        liquidity,
        poll_latency_ms,
        now,
    )

    return await db.execute(sql, params)


async def get_latest_prices(
    db: Database,
    market_id: str,
    limit: int = 1,
) -> list[dict[str, Any]]:
    """
    Retrieve the latest price records for a market.

    Args:
        db: Database instance
        market_id: Market ID
        limit: Number of records to return

    Returns:
        List of price records ordered by most recent
    """
    sql = """
    SELECT * FROM market_prices
    WHERE market_id = ?
    ORDER BY polled_at DESC
    LIMIT ?
    """

    return await db.fetch_all(sql, (market_id, limit))


async def insert_ingestor_run(
    db: Database,
    platform: str,
    markets_fetched: int,
    markets_new: int,
    markets_updated: int,
    errors: int = 0,
    error_detail: str | None = None,
    duration_ms: int | None = None,
) -> int:
    """
    Insert an ingestor run record.

    Args:
        db: Database instance
        platform: Platform name
        markets_fetched: Total markets fetched
        markets_new: New markets found
        markets_updated: Markets updated
        errors: Error count
        error_detail: Error details
        duration_ms: Run duration in milliseconds

    Returns:
        Row ID of inserted record
    """
    now = datetime.now(timezone.utc).isoformat()

    sql = """
    INSERT INTO ingestor_runs (
        platform, markets_fetched, markets_new, markets_updated,
        errors, error_detail, duration_ms, ran_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    params = (
        platform,
        markets_fetched,
        markets_new,
        markets_updated,
        errors,
        error_detail,
        duration_ms,
        now,
    )

    return await db.execute(sql, params)


async def get_ingestor_runs(
    db: Database,
    platform: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Retrieve ingestor run records.

    Args:
        db: Database instance
        platform: Optional platform filter
        limit: Maximum records

    Returns:
        List of ingestor run records
    """
    if platform:
        sql = """
        SELECT * FROM ingestor_runs
        WHERE platform = ?
        ORDER BY ran_at DESC
        LIMIT ?
        """
        params = (platform, limit)
    else:
        sql = """
        SELECT * FROM ingestor_runs
        ORDER BY ran_at DESC
        LIMIT ?
        """
        params = (limit,)

    return await db.fetch_all(sql, params)


async def get_market_count(
    db: Database,
    platform: str | None = None,
) -> int:
    """
    Count markets, optionally filtered by platform.

    Args:
        db: Database instance
        platform: Optional platform filter

    Returns:
        Number of markets
    """
    if platform:
        sql = "SELECT COUNT(*) as count FROM markets WHERE platform = ?"
        result = await db.fetch_one(sql, (platform,))
    else:
        sql = "SELECT COUNT(*) as count FROM markets"
        result = await db.fetch_one(sql)

    return result["count"] if result else 0


async def get_markets_needing_update(
    db: Database,
    platform: str,
    minutes_old: int = 10,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    Get markets that haven't been updated recently.

    Args:
        db: Database instance
        platform: Platform to filter
        minutes_old: Consider markets older than this
        limit: Maximum results

    Returns:
        List of market records
    """
    sql = """
    SELECT m.* FROM markets m
    LEFT JOIN market_prices mp ON m.id = mp.market_id
    WHERE m.platform = ?
    AND (
        mp.polled_at IS NULL
        OR datetime(mp.polled_at) < datetime('now', '-' || ? || ' minutes')
    )
    AND m.status NOT IN ('resolved', 'closed')
    GROUP BY m.id
    ORDER BY COALESCE(mp.polled_at, m.created_at) ASC
    LIMIT ?
    """

    return await db.fetch_all(sql, (platform, minutes_old, limit))
