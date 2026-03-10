"""Complementarity constraint rule for prediction markets.

Enforces: P(X) + P(not X) = 100% on binary markets
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class ViolationInfo:
    """Information about a constraint violation."""
    rule_type: str
    severity: str  # "critical" or "warning"
    description: str
    implied_arbitrage: float  # Estimated profit opportunity in percent


def check(
    yes_price: float,
    no_price: float,
    tolerance: float = 0.01
) -> Optional[ViolationInfo]:
    """
    Check complementarity constraint for binary markets.

    For a binary market, YES price + NO price should equal 1.0 (100%).
    The tolerance parameter accounts for trading fees and small market inefficiencies.

    Args:
        yes_price: Probability/price of YES outcome (0.0 to 1.0)
        no_price: Probability/price of NO outcome (0.0 to 1.0)
        tolerance: Maximum allowed deviation from 1.0 (default 0.01 = 1%)

    Returns:
        ViolationInfo if violation detected, None otherwise

    Raises:
        ValueError: If prices are outside [0, 1]
    """
    if not 0.0 <= yes_price <= 1.0:
        raise ValueError(f"yes_price must be in [0, 1], got {yes_price}")
    if not 0.0 <= no_price <= 1.0:
        raise ValueError(f"no_price must be in [0, 1], got {no_price}")
    if tolerance < 0:
        raise ValueError(f"tolerance must be non-negative, got {tolerance}")

    total = yes_price + no_price

    # Check if deviation exceeds tolerance
    deviation = abs(total - 1.0)

    if deviation > tolerance:
        # Determine arbitrage direction
        if total > 1.0:
            # Overpriced - can sell both sides for profit
            arbitrage = total - 1.0
            direction = "short both"
        else:
            # Underpriced - can buy both sides for profit
            arbitrage = 1.0 - total
            direction = "long both"

        return ViolationInfo(
            rule_type="complementarity",
            severity="warning" if arbitrage < 0.05 else "critical",
            description=(
                f"Binary market prices not complementary. "
                f"YES: {yes_price:.4f}, NO: {no_price:.4f}, Sum: {total:.4f}. "
                f"Expected: 1.0000. Deviation: {deviation:.4f}. "
                f"Arbitrage strategy: {direction}."
            ),
            implied_arbitrage=arbitrage * 100
        )

    return None
