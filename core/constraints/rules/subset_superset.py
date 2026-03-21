"""Subset/superset constraint rule for prediction markets.

Enforces logical consistency: P(X in Q1) ≤ P(X in H1) ≤ P(X in 2026)
"""

from dataclasses import dataclass


@dataclass
class ViolationInfo:
    """Information about a constraint violation."""

    rule_type: str
    severity: str  # "critical" or "warning"
    description: str
    implied_arbitrage: float  # Estimated profit opportunity in percent


def check(
    market_a_price: float, market_b_price: float, relationship: str
) -> ViolationInfo | None:
    """
    Check subset/superset relationship between two markets.

    For a valid subset/superset relationship where market_a is a subset of market_b:
    P(A) ≤ P(B) must hold. If P(A) > P(B), there's an exploitable arbitrage.

    Args:
        market_a_price: Probability of subset market (0.0 to 1.0)
        market_b_price: Probability of superset market (0.0 to 1.0)
        relationship: Either "subset" or "superset" indicating which is which
                     If "subset", market_a is subset of market_b
                     If "superset", market_a is superset of market_b

    Returns:
        ViolationInfo if violation detected, None otherwise
    """
    if not 0.0 <= market_a_price <= 1.0:
        raise ValueError(f"market_a_price must be in [0, 1], got {market_a_price}")
    if not 0.0 <= market_b_price <= 1.0:
        raise ValueError(f"market_b_price must be in [0, 1], got {market_b_price}")

    if relationship.lower() == "subset":
        # market_a is subset of market_b
        # Should have: P(A) ≤ P(B)
        if market_a_price > market_b_price:
            spread = market_a_price - market_b_price
            return ViolationInfo(
                rule_type="subset_superset",
                severity="critical",
                description=(
                    f"Subset market price ({market_a_price:.4f}) exceeds "
                    f"superset market price ({market_b_price:.4f}). "
                    "Arbitrage: sell subset, buy superset."
                ),
                implied_arbitrage=spread * 100,
            )

    elif relationship.lower() == "superset":
        # market_a is superset of market_b
        # Should have: P(B) ≤ P(A)
        if market_b_price > market_a_price:
            spread = market_b_price - market_a_price
            return ViolationInfo(
                rule_type="subset_superset",
                severity="critical",
                description=(
                    f"Subset market price ({market_b_price:.4f}) exceeds "
                    f"superset market price ({market_a_price:.4f}). "
                    "Arbitrage: sell subset, buy superset."
                ),
                implied_arbitrage=spread * 100,
            )
    else:
        raise ValueError(
            f"relationship must be 'subset' or 'superset', got '{relationship}'"
        )

    return None
