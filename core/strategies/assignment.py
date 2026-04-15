"""
Strategy assignment logic.

Provides assign_strategy and the STRATEGIES constant for mapping
violation characteristics to trading strategy labels.
"""

STRATEGIES = [
    "P1_cross_market_arb",
    "P2_structured_event",
    "P3_calibration_bias",
    "P4_liquidity_timing",
    "P5_information_latency",
]


def assign_strategy(spread: float, pair_type: str) -> str:
    """Assign a strategy based on violation characteristics."""
    if pair_type == "cross_platform":
        return "P1_cross_market_arb"
    elif spread > 0.10:
        return "P5_information_latency"
    elif spread >= 0.05:
        return "P3_calibration_bias"
    elif pair_type == "complement":
        return "P4_liquidity_timing"
    else:
        return "P2_structured_event"
