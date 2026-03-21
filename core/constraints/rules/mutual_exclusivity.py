"""Mutual exclusivity constraint rule for prediction markets.

Enforces: P(A wins) + P(B wins) + P(C wins) ≤ 100% for exhaustive outcomes
"""

from dataclasses import dataclass
from typing import Optional, List


@dataclass
class ViolationInfo:
    """Information about a constraint violation."""

    rule_type: str
    severity: str  # "critical" or "warning"
    description: str
    implied_arbitrage: float  # Estimated profit opportunity in percent


def check(prices: List[float]) -> Optional[ViolationInfo]:
    """
    Check mutual exclusivity constraint for a group of exhaustive outcomes.

    For a set of mutually exclusive and exhaustive outcomes, the sum of
    probabilities should equal 1.0. If the sum exceeds 1.0, there's an
    exploitable arbitrage (sell each outcome proportionally).

    Args:
        prices: List of probabilities for mutually exclusive outcomes (0.0 to 1.0 each)

    Returns:
        ViolationInfo if violation detected, None otherwise

    Raises:
        ValueError: If any price is outside [0, 1] or list is empty
    """
    if not prices:
        raise ValueError("prices list cannot be empty")

    for i, price in enumerate(prices):
        if not 0.0 <= price <= 1.0:
            raise ValueError(f"prices[{i}] must be in [0, 1], got {price}")

    total_probability = sum(prices)

    # Allow small tolerance for floating point arithmetic
    TOLERANCE = 1e-6

    if total_probability > 1.0 + TOLERANCE:
        excess = total_probability - 1.0
        return ViolationInfo(
            rule_type="mutual_exclusivity",
            severity="critical",
            description=(
                f"Sum of mutually exclusive outcome prices ({total_probability:.4f}) "
                f"exceeds 100%. Excess: {excess:.4f}. "
                "Arbitrage: sell each outcome proportionally."
            ),
            implied_arbitrage=excess * 100,
        )

    return None
