"""Risk checks for signal generation."""

import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskCheckResult:
    """Result of a risk check."""

    passed: bool
    check_type: str
    check_value: float
    threshold: float
    detail: str


async def check_position_limit(
    signal: Any, config: dict, db: Optional[Any] = None
) -> RiskCheckResult:
    """
    Check if signal respects maximum position size limit.

    Args:
        signal: Signal object
        config: Config dict with max_position_size_usd
        db: Database connection

    Returns:
        RiskCheckResult
    """
    max_size = config.get("max_position_size_usd", 5000)
    signal_size = sum(leg.size_usd for leg in signal.legs)

    passed = signal_size <= max_size

    return RiskCheckResult(
        passed=passed,
        check_type="position_limit",
        check_value=signal_size,
        threshold=max_size,
        detail=f"Signal size {signal_size:.2f} vs limit {max_size:.2f}",
    )


async def check_daily_loss_limit(
    signal: Any, config: dict, db: Optional[Any] = None
) -> RiskCheckResult:
    """
    Check daily loss limit not exceeded.

    Args:
        signal: Signal object
        config: Config dict with max_daily_loss_usd
        db: Database connection

    Returns:
        RiskCheckResult
    """
    if not db:
        # No DB, assume check passes
        return RiskCheckResult(
            passed=True,
            check_type="daily_loss_limit",
            check_value=0,
            threshold=config.get("max_daily_loss_usd", 10000),
            detail="No DB to check realized loss",
        )

    try:
        daily_loss = await db.get_realized_loss_today()
    except Exception as e:
        logger.error(f"Error checking daily loss: {e}")
        daily_loss = 0

    max_loss = config.get("max_daily_loss_usd", 10000)
    passed = daily_loss < max_loss

    return RiskCheckResult(
        passed=passed,
        check_type="daily_loss_limit",
        check_value=daily_loss,
        threshold=max_loss,
        detail=f"Daily loss {daily_loss:.2f} vs limit {max_loss:.2f}",
    )


async def check_concentration(
    signal: Any, config: dict, db: Optional[Any] = None
) -> RiskCheckResult:
    """
    Check market concentration limit.

    Args:
        signal: Signal object
        config: Config dict with max_concentration_pct
        db: Database connection

    Returns:
        RiskCheckResult
    """
    if not db:
        return RiskCheckResult(
            passed=True,
            check_type="concentration",
            check_value=0,
            threshold=config.get("max_concentration_pct", 0.3),
            detail="No DB to check concentration",
        )

    try:
        total_exposure = await db.get_total_exposure()
    except Exception as e:
        logger.error(f"Error checking concentration: {e}")
        total_exposure = 0

    signal_exposure = sum(leg.size_usd for leg in signal.legs)
    bankroll = config.get("bankroll_usd", 100000)
    max_concentration = config.get("max_concentration_pct", 0.3)

    total_pct = (total_exposure + signal_exposure) / bankroll if bankroll > 0 else 0
    passed = total_pct <= max_concentration

    return RiskCheckResult(
        passed=passed,
        check_type="concentration",
        check_value=total_pct,
        threshold=max_concentration,
        detail=f"Portfolio concentration {total_pct:.2%} vs limit {max_concentration:.2%}",
    )


async def check_duplicate_signal(
    signal: Any, config: dict, db: Optional[Any] = None
) -> RiskCheckResult:
    """
    Check for duplicate signals on same market.

    Args:
        signal: Signal object
        config: Config dict
        db: Database connection

    Returns:
        RiskCheckResult
    """
    if not db:
        return RiskCheckResult(
            passed=True,
            check_type="duplicate",
            check_value=0,
            threshold=0,
            detail="No DB to check duplicates",
        )

    try:
        # Check for recent signals on same markets
        market_ids = [leg.market_id for leg in signal.legs]
        recent_count = await db.count_recent_signals(market_ids, minutes=5)
    except Exception as e:
        logger.error(f"Error checking duplicates: {e}")
        recent_count = 0

    passed = recent_count == 0

    return RiskCheckResult(
        passed=passed,
        check_type="duplicate",
        check_value=recent_count,
        threshold=0,
        detail=f"Found {recent_count} recent signals on same markets",
    )


async def check_min_edge(
    signal: Any, config: dict, db: Optional[Any] = None
) -> RiskCheckResult:
    """
    Check minimum edge threshold.

    Args:
        signal: Signal object
        config: Config dict with min_edge
        db: Database connection

    Returns:
        RiskCheckResult
    """
    min_edge = config.get("min_edge", 0.02)

    # Get edge from metadata if available
    edge = getattr(signal, "edge", 0)

    passed = abs(edge) >= min_edge

    return RiskCheckResult(
        passed=passed,
        check_type="min_edge",
        check_value=edge,
        threshold=min_edge,
        detail=f"Edge {edge:.4f} vs minimum {min_edge:.4f}",
    )


async def run_all_checks(
    signal: Any, config: dict, db: Optional[Any] = None
) -> tuple[bool, list[RiskCheckResult]]:
    """
    Run all risk checks on signal.

    Args:
        signal: Signal object
        config: Config dict
        db: Database connection

    Returns:
        (all_passed, list of RiskCheckResult)
    """
    results = []

    # Run checks in order
    checks = [
        check_position_limit,
        check_daily_loss_limit,
        check_concentration,
        check_duplicate_signal,
        check_min_edge,
    ]

    for check_fn in checks:
        try:
            result = await check_fn(signal, config, db)
            results.append(result)

            # Log result
            status = "PASS" if result.passed else "FAIL"
            logger.info(
                f"[{status}] {result.check_type}: {result.detail}"
            )

        except Exception as e:
            logger.error(f"Error running {check_fn.__name__}: {e}")
            results.append(
                RiskCheckResult(
                    passed=False,
                    check_type=check_fn.__name__,
                    check_value=0,
                    threshold=0,
                    detail=f"Check error: {str(e)}",
                )
            )

    all_passed = all(r.passed for r in results)

    return all_passed, results
