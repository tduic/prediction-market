"""
PnL snapshot and analytics reporting.

Provides take_trading_snapshot and print_analytics for capturing periodic
trading performance metrics and printing strategy-level analytics reports.
"""

import logging
from datetime import datetime, timezone

import aiosqlite

from core.strategies.assignment import STRATEGIES

logger = logging.getLogger(__name__)

PAPER_CAPITAL = 10_000  # Default paper trading starting capital


async def take_trading_snapshot(db: aiosqlite.Connection) -> int | None:
    """
    Write a pnl_snapshots row and per-strategy strategy_pnl_snapshots rows.

    Computes metrics from trade_outcomes (the table paper trading writes to).
    This feeds the dashboard's overview cards, equity curve, strategy PnL chart,
    and risk metrics.
    """
    now = datetime.now(timezone.utc).isoformat()

    try:
        # ── Aggregate totals from trade_outcomes ──
        cursor = await db.execute("""SELECT
                   COALESCE(SUM(actual_pnl), 0) as realized_pnl,
                   COALESCE(SUM(fees_total), 0) as total_fees,
                   COUNT(*) as trade_count
               FROM trade_outcomes""")
        totals = await cursor.fetchone()
        realized_pnl_total = totals[0] if totals else 0
        fees_total = totals[1] if totals else 0
        total_capital = PAPER_CAPITAL + realized_pnl_total - fees_total
        cash = total_capital  # Paper trading has no open positions

        # ── Insert pnl_snapshots row ──
        cursor = await db.execute(
            """INSERT INTO pnl_snapshots (
                   snapshot_type, total_capital, cash,
                   open_positions_count, open_notional,
                   unrealized_pnl, realized_pnl_today, realized_pnl_total,
                   fees_today, fees_total,
                   pnl_constraint_arb, pnl_event_model, pnl_calibration,
                   pnl_liquidity, pnl_latency,
                   capital_polymarket, capital_kalshi, snapshotted_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "periodic",
                total_capital,
                cash,
                0,  # open_positions_count
                0.0,  # open_notional
                0.0,  # unrealized_pnl
                0.0,  # realized_pnl_today (we could compute this, but keeping simple)
                realized_pnl_total,
                0.0,  # fees_today
                fees_total,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,  # strategy-specific PnL columns
                total_capital * 0.6,  # capital_polymarket
                total_capital * 0.4,  # capital_kalshi
                now,
            ),
        )
        snapshot_id = cursor.lastrowid

        # ── Per-strategy breakdown → strategy_pnl_snapshots ──
        cursor = await db.execute("""SELECT
                   strategy,
                   COALESCE(SUM(actual_pnl), 0) as realized_pnl,
                   COALESCE(SUM(fees_total), 0) as fees,
                   COUNT(*) as trade_count,
                   SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) as win_count
               FROM trade_outcomes
               GROUP BY strategy""")
        strategy_rows = await cursor.fetchall()

        for row in strategy_rows:
            await db.execute(
                """INSERT INTO strategy_pnl_snapshots
                       (snapshot_id, strategy, realized_pnl, unrealized_pnl, fees, trade_count, win_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_id, row[0], row[1], 0.0, row[2], row[3], row[4]),
            )

        await db.commit()
        logger.info(
            "Snapshot #%d: capital=$%.2f realized=$%.4f fees=$%.4f",
            snapshot_id,
            total_capital,
            realized_pnl_total,
            fees_total,
        )
        return snapshot_id

    except Exception as e:
        logger.error("Failed to take snapshot: %s", e)
        return None


async def print_analytics(db: aiosqlite.Connection):
    """Print strategy-level analytics from the DB."""
    from core.analytics import StrategyScorecard

    scorecard = StrategyScorecard(db)

    print("\n" + "=" * 70)
    print("  PAPER TRADING SESSION ANALYTICS")
    print("=" * 70)

    # Portfolio summary
    try:
        summary = await scorecard.get_portfolio_summary(days=1)
        if summary:
            print("\n  Portfolio Summary (last 24h)")
            for k, v in summary.items():
                if isinstance(v, float):
                    print(
                        f"    {k}: ${v:.4f}"
                        if "pnl" in k.lower()
                        else f"    {k}: {v:.4f}"
                    )
                else:
                    print(f"    {k}: {v}")
    except Exception as e:
        logger.debug("Portfolio summary error: %s", e)

    # Per-strategy breakdown
    print(
        f"\n  {'Strategy':<28} {'Trades':>7} {'Win%':>7} "
        f"{'PnL':>10} {'Sharpe':>8} {'Edge%':>8}"
    )
    print("  " + "-" * 68)

    for strategy in STRATEGIES:
        try:
            stats = await scorecard.get_strategy_summary(strategy, days=1)
            if stats and stats.get("total_trades", 0) > 0:
                print(
                    f"  {strategy:<28} {stats['total_trades']:>7} "
                    f"{stats.get('win_rate', 0):>6.1f}% "
                    f"${stats.get('total_pnl', 0):>9.4f} "
                    f"{stats.get('sharpe_ratio', 0):>7.2f} "
                    f"{stats.get('avg_edge_captured_pct', 0):>7.1f}%"
                )
        except Exception as e:
            logger.debug("No data for %s: %s", strategy, e)

    # Raw totals
    cursor = await db.execute("SELECT COUNT(*) FROM trade_outcomes")
    row = await cursor.fetchone()
    total = row[0] if row else 0

    cursor = await db.execute(
        "SELECT SUM(actual_pnl), SUM(fees_total) FROM trade_outcomes"
    )
    row = await cursor.fetchone()
    total_pnl = row[0] if row and row[0] else 0
    total_fees = row[1] if row and row[1] else 0

    # Matches and violations
    cursor = await db.execute(
        "SELECT COUNT(*) FROM markets WHERE platform = 'polymarket'"
    )
    poly_count = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM markets WHERE platform = 'kalshi'")
    kalshi_count = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM violations")
    viol_count = (await cursor.fetchone())[0]

    print(f"\n  Markets: {poly_count} Polymarket + {kalshi_count} Kalshi")
    print(f"  Violations detected: {viol_count}")
    print(f"  Trades executed: {total}")
    print(f"  Total PnL: ${total_pnl:.4f}")
    print(f"  Total fees: ${total_fees:.4f}")
    print(f"  Net: ${total_pnl:.4f}")
    print("=" * 70)
