"""
Risk checks for signal validation before execution.

All monetary limits are computed as percentages of current portfolio value,
so they scale automatically as the account grows or shrinks.

Portfolio value = starting_capital + realized_pnl_total - fees_total

These checks are called by the execution handler BEFORE routing orders.
Every check result is logged to the risk_check_log table for audit.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    """Result of a single risk check."""

    passed: bool
    check_type: str
    check_value: float
    threshold: float
    detail: str


async def get_portfolio_value(
    db: aiosqlite.Connection,
    starting_capital: float,
) -> float:
    """
    Compute current portfolio value from trade history.

    Returns starting_capital + sum(actual_pnl) - sum(fees_total)
    from trade_outcomes. Falls back to starting_capital if no trades.
    """
    try:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(actual_pnl), 0), COALESCE(SUM(fees_total), 0) "
            "FROM trade_outcomes"
        )
        row = await cursor.fetchone()
        realized_pnl = row[0] if row else 0
        total_fees = row[1] if row else 0
        return starting_capital + realized_pnl - total_fees
    except Exception as e:
        logger.warning(
            "Could not compute portfolio value: %s — using starting_capital", e
        )
        return starting_capital


async def check_position_limit(
    signal: Any,
    portfolio_value: float,
    max_position_pct: float,
    db: aiosqlite.Connection | None = None,
) -> RiskCheckResult:
    """
    Check if signal size respects maximum position size (% of portfolio).

    A single trade should not risk more than max_position_pct of the portfolio.
    Default: 5% → on a $10,000 portfolio, max single position = $500.
    """
    max_size = portfolio_value * max_position_pct
    signal_size = 0.0
    for leg in signal.legs:
        price = leg.limit_price if leg.limit_price is not None else 0.5
        signal_size += leg.size * price

    passed = signal_size <= max_size

    return RiskCheckResult(
        passed=passed,
        check_type="position_limit",
        check_value=signal_size,
        threshold=max_size,
        detail=(
            f"Signal size ${signal_size:.2f} vs limit ${max_size:.2f} "
            f"({max_position_pct:.0%} of ${portfolio_value:.2f})"
        ),
    )


async def check_daily_loss_limit(
    signal: Any,
    portfolio_value: float,
    max_daily_loss_pct: float,
    db: aiosqlite.Connection | None = None,
) -> RiskCheckResult:
    """
    Check if today's realized losses are within the daily loss limit.

    Default: 2% → on a $10,000 portfolio, max daily loss = $200.
    """
    max_loss = portfolio_value * max_daily_loss_pct

    if not db:
        return RiskCheckResult(
            passed=True,
            check_type="daily_loss_limit",
            check_value=0,
            threshold=max_loss,
            detail="No DB — cannot verify daily loss, passing cautiously",
        )

    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Match circuit_breaker._compute_daily_loss() exactly: net = pnl - fees,
        # loss = max(0, -net). Removing the WHERE actual_pnl < 0 filter ensures
        # fee drag on individually-profitable trades is included in the tally.
        cursor = await db.execute(
            "SELECT COALESCE(SUM(actual_pnl - COALESCE(fees_total, 0)), 0) "
            "FROM trade_outcomes WHERE DATE(created_at) = ?",
            (today,),
        )
        row = await cursor.fetchone()
        net_pnl = float(row[0]) if row and row[0] is not None else 0.0
        daily_loss = max(0.0, -net_pnl)
    except Exception as e:
        logger.error("Error checking daily loss: %s", e)
        daily_loss = 0

    passed = daily_loss < max_loss

    return RiskCheckResult(
        passed=passed,
        check_type="daily_loss_limit",
        check_value=daily_loss,
        threshold=max_loss,
        detail=(
            f"Today's net loss (incl. fees) ${daily_loss:.2f} vs limit ${max_loss:.2f} "
            f"({max_daily_loss_pct:.0%} of ${portfolio_value:.2f})"
        ),
    )


async def check_portfolio_exposure(
    signal: Any,
    portfolio_value: float,
    max_exposure_pct: float,
    db: aiosqlite.Connection | None = None,
) -> RiskCheckResult:
    """
    Check that total open exposure + this signal doesn't exceed the cap.

    Default: 20% → on $10,000, max total deployed capital = $2,000.
    """
    max_exposure = portfolio_value * max_exposure_pct

    # Current open exposure from positions table
    current_exposure = 0.0
    if db:
        try:
            cursor = await db.execute(
                "SELECT COALESCE(SUM(entry_price * entry_size), 0) "
                "FROM positions WHERE status = 'open'"
            )
            row = await cursor.fetchone()
            current_exposure = row[0] if row and row[0] else 0
        except Exception as e:
            logger.warning("Could not query open exposure: %s", e)

    # Add this signal's notional
    signal_notional = 0.0
    for leg in signal.legs:
        price = leg.limit_price if leg.limit_price is not None else 0.5
        signal_notional += leg.size * price

    total_exposure = current_exposure + signal_notional
    passed = total_exposure <= max_exposure

    return RiskCheckResult(
        passed=passed,
        check_type="portfolio_exposure",
        check_value=total_exposure,
        threshold=max_exposure,
        detail=(
            f"Total exposure ${total_exposure:.2f} (open ${current_exposure:.2f} + "
            f"signal ${signal_notional:.2f}) vs limit ${max_exposure:.2f} "
            f"({max_exposure_pct:.0%} of ${portfolio_value:.2f})"
        ),
    )


async def check_duplicate_signal(
    signal: Any,
    duplicate_window_s: int = 300,
    db: aiosqlite.Connection | None = None,
) -> RiskCheckResult:
    """
    Check for duplicate signals on the same market within a time window.

    Prevents the system from stacking trades on the same opportunity.
    """
    if not db:
        return RiskCheckResult(
            passed=True,
            check_type="duplicate",
            check_value=0,
            threshold=0,
            detail="No DB — cannot check duplicates",
        )

    try:
        market_ids = [leg.market_id for leg in signal.legs]
        placeholders = ",".join("?" for _ in market_ids)
        cursor = await db.execute(
            f"SELECT COUNT(*) FROM orders "
            f"WHERE market_id IN ({placeholders}) "
            f"AND submitted_at > datetime('now', '-{duplicate_window_s} seconds')",
            market_ids,
        )
        row = await cursor.fetchone()
        recent_count = row[0] if row else 0
    except Exception as e:
        logger.warning("Error checking duplicates: %s", e)
        recent_count = 0

    passed = recent_count == 0

    return RiskCheckResult(
        passed=passed,
        check_type="duplicate",
        check_value=recent_count,
        threshold=0,
        detail=f"Found {recent_count} recent orders on same markets (window: {duplicate_window_s}s)",
    )


async def check_min_edge(
    signal: Any,
    min_edge: float = 0.02,
) -> RiskCheckResult:
    """
    Check that the signal's expected edge exceeds the minimum threshold.

    Prevents trading on noise — the edge must clear fees + slippage.
    """
    edge = getattr(signal, "edge", None) or 0
    passed = abs(edge) >= min_edge

    return RiskCheckResult(
        passed=passed,
        check_type="min_edge",
        check_value=edge,
        threshold=min_edge,
        detail=f"Edge {edge:.4f} vs minimum {min_edge:.4f}",
    )


async def run_all_checks(
    signal: Any,
    risk_config: Any,
    db: aiosqlite.Connection | None = None,
) -> tuple[bool, list[RiskCheckResult]]:
    """
    Run all risk checks on a signal before execution.

    Args:
        signal: TradingSignal with .legs, optionally .edge
        risk_config: RiskControlConfig from core.config
        db: aiosqlite.Connection for querying portfolio state

    Returns:
        (all_passed, list of RiskCheckResult)
    """
    portfolio_value = (
        await get_portfolio_value(db, risk_config.starting_capital)
        if db
        else risk_config.starting_capital
    )

    results: list[RiskCheckResult] = []

    checks = [
        lambda: check_position_limit(
            signal, portfolio_value, risk_config.max_position_pct, db
        ),
        lambda: check_daily_loss_limit(
            signal, portfolio_value, risk_config.max_daily_loss_pct, db
        ),
        lambda: check_portfolio_exposure(
            signal, portfolio_value, risk_config.max_portfolio_exposure_pct, db
        ),
        lambda: check_duplicate_signal(
            signal, risk_config.duplicate_signal_window_s, db
        ),
        lambda: check_min_edge(signal, risk_config.min_edge),
    ]

    for check_fn in checks:
        try:
            result = await check_fn()
            results.append(result)

            status = "PASS" if result.passed else "FAIL"
            logger.info("[%s] %s: %s", status, result.check_type, result.detail)

        except Exception as e:
            logger.error("Error running risk check: %s", e)
            results.append(
                RiskCheckResult(
                    passed=False,
                    check_type="error",
                    check_value=0,
                    threshold=0,
                    detail=f"Check error: {str(e)}",
                )
            )

    all_passed = all(r.passed for r in results)

    # Log all results to risk_check_log for audit
    if db:
        await _log_risk_checks(db, signal, results)

    return all_passed, results


async def _log_risk_checks(
    db: aiosqlite.Connection,
    signal: Any,
    results: list[RiskCheckResult],
) -> None:
    """Write risk check results to the risk_check_log table.

    Risk checks always run before the signal is written to DB across all
    callers (arb_engine, generator, execution/handler), so signal_id is
    always NULL at log time (the FK would otherwise fail under
    PRAGMA foreign_keys=ON). violation_id is pulled off the signal when
    available — callers that insert the violation before running checks
    (e.g. arb_engine) get the audit linkage; callers without a violation
    context leave it NULL.
    """
    signal_id = None
    violation_id = getattr(signal, "violation_id", None)
    now = datetime.now(timezone.utc).isoformat()

    try:
        for r in results:
            await db.execute(
                """INSERT INTO risk_check_log
                   (signal_id, violation_id, check_type, passed, check_value,
                    threshold, detail, evaluated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    signal_id,
                    violation_id,
                    r.check_type,
                    int(r.passed),
                    r.check_value,
                    r.threshold,
                    r.detail,
                    now,
                ),
            )
        await db.commit()
    except Exception as e:
        logger.warning("Failed to log risk checks: %s", e)
