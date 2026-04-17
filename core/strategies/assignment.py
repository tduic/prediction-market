"""
Strategy label assignment.

P1_cross_market_arb is the only real strategy: cross-platform arbitrage
between Polymarket and Kalshi. P2-P5 are spread-bucket labels applied to
same-platform signals for analytics bucketing — they are NOT ML-driven
and do not use distinct trading logic. The label is chosen by the spread
magnitude and pair type so that PnL can be attributed per bucket.

STRATEGIES is consumed by core.snapshots.pnl to iterate all labels.
"""

STRATEGIES = [
    "P1_cross_market_arb",
    "P2_structured_event",
    "P3_calibration_bias",
    "P4_liquidity_timing",
    "P5_information_latency",
]


def assign_strategy(spread: float, pair_type: str) -> str:
    """Return a strategy label for a violation based on its spread bucket.

    Cross-platform violations are always P1 (the true arb strategy).
    Same-platform violations are bucketed by spread magnitude so analytics
    can attribute PnL to different opportunity profiles:

      - spread > 0.10       → P5_information_latency (large mispricings)
      - 0.05 <= spread      → P3_calibration_bias (mid-range)
      - pair_type=complement → P4_liquidity_timing
      - otherwise           → P2_structured_event

    The labels are descriptive, not algorithmic — all same-platform
    signals flow through the same execution path.
    """
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
