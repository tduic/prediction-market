"""
Phase 0 baseline snapshot.

Provides take_phase0_baseline_snapshot for capturing initial state metrics
that subsequent phases can compare against as a reference point.
"""

import logging
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger(__name__)


async def take_phase0_baseline_snapshot(db: aiosqlite.Connection) -> None:
    """Write a baseline row to phase0_baseline (Phase 0.4).

    Captures pair count and per-strategy PnL at T0 so every later phase
    can compare against this reference. Safe to call multiple times — each
    call appends a new point-in-time row.
    """
    now = datetime.now(timezone.utc).isoformat()

    cursor = await db.execute("SELECT COUNT(*) FROM market_pairs")
    total_pairs = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COUNT(*) FROM market_pairs WHERE active=1")
    active_pairs = (await cursor.fetchone())[0]

    pnl_by_strategy: dict[str, float] = {}
    cursor = await db.execute(
        "SELECT strategy, SUM(actual_pnl) FROM trade_outcomes GROUP BY strategy"
    )
    for row in await cursor.fetchall():
        pnl_by_strategy[row[0]] = row[1] or 0.0

    cursor = await db.execute("SELECT COUNT(*) FROM trade_outcomes")
    total_trade_count = (await cursor.fetchone())[0]
    total_realized_pnl = sum(pnl_by_strategy.values())

    await db.execute(
        """INSERT INTO phase0_baseline
           (snapshot_timestamp, pair_count, active_pair_count,
            p1_realized_pnl, p2_realized_pnl, p3_realized_pnl,
            p4_realized_pnl, p5_realized_pnl, total_realized_pnl,
            total_trade_count, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'phase0')""",
        (
            now,
            total_pairs,
            active_pairs,
            pnl_by_strategy.get("P1_cross_market_arb", 0.0),
            pnl_by_strategy.get("P2_structured_event", 0.0),
            pnl_by_strategy.get("P3_calibration_bias", 0.0),
            pnl_by_strategy.get("P4_liquidity_timing", 0.0),
            pnl_by_strategy.get("P5_information_latency", 0.0),
            total_realized_pnl,
            total_trade_count,
        ),
    )
    await db.commit()
    logger.info(
        "Phase 0 baseline: %d pairs (%d active) total_pnl=%.2f trades=%d",
        total_pairs,
        active_pairs,
        total_realized_pnl,
        total_trade_count,
    )
