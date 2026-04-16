"""Kelly fraction and position sizing calculations."""

import logging

import numpy as np

logger = logging.getLogger(__name__)


def compute_kelly_fraction(
    edge: float,
    odds: float,
    kelly_fraction: float = 0.25,
) -> float:
    """
    Compute Kelly fraction for position sizing.

    Kelly Criterion formula: f* = (bp - q) / b
    where:
        b = odds (win payout / stake)
        p = probability of winning
        _q = probability of losing (1 - p)
        f* = fraction of bankroll to bet

    We use fractional Kelly (quarter-Kelly) for safety.

    Args:
        edge: Edge as probability (fair_value - market_price)
            Positive = go long, negative = go short
        odds: Market price (0-1) interpreted as b+1 odds
        kelly_fraction: Fractional Kelly multiplier (default 0.25 = quarter-Kelly)
            Must be between 0 and 1

    Returns:
        Kelly fraction (0-0.5 hard cap for safety)
    """
    # Ensure odds are valid
    odds = float(np.clip(odds, 0.01, 0.99))
    edge = float(edge)

    # Interpret market price as probability
    # odds = p for binary outcome
    p = odds  # Probability if betting YES
    _q = 1 - p  # Probability if betting NO

    # If edge > 0, we want to go long (betting YES)
    # If edge < 0, we want to go short (betting NO)
    if edge > 0:
        # Long position
        # Kelly: f* = (p - q) / 1 = 2p - 1
        # Approximation when edge is probability difference
        kelly_raw = edge / (1.0 - edge) if edge < 1 else 0.5
    else:
        # Short position (flipped probabilities)
        kelly_raw = edge / (1.0 + edge) if edge > -1 else -0.5

    # Apply fractional Kelly (default quarter-Kelly)
    kelly_f = kelly_raw * kelly_fraction

    # Hard cap at 0.5 for safety
    kelly_f = float(np.clip(kelly_f, -0.5, 0.5))

    return kelly_f


def compute_position_size(
    kelly_f: float,
    bankroll: float,
    max_size: float = 10000.0,
) -> float:
    """
    Compute position size given Kelly fraction and bankroll.

    Args:
        kelly_f: Kelly fraction from compute_kelly_fraction
        bankroll: Total available capital (USD)
        max_size: Hard ceiling on position size (default $10k)

    Returns:
        Position size in USD (0 to max_size)

    Raises:
        ValueError: If bankroll is non-positive
    """
    if bankroll <= 0:
        logger.warning("Bankroll must be positive")
        return 0.0

    # Position size = Kelly fraction * bankroll
    # But cap at max_size absolute
    position_size = abs(kelly_f) * bankroll

    # Apply hard ceiling
    position_size = min(position_size, max_size)

    # Ensure non-negative
    position_size = max(position_size, 0.0)

    logger.debug(
        f"Position sizing: kelly_f={kelly_f:.4f}, bankroll={bankroll:.2f}, size={position_size:.2f}"
    )

    return float(position_size)


def compute_risk_adjusted_sizing(
    kelly_f: float,
    bankroll: float,
    max_size: float,
    volatility: float | None = None,
    confidence: float | None = None,
) -> float:
    """
    Compute position size with risk and confidence adjustments.

    Args:
        kelly_f: Kelly fraction
        bankroll: Total capital
        max_size: Position size ceiling
        volatility: Market volatility (0-1) - higher volatility reduces size
        confidence: Model confidence (0-1) - lower confidence reduces size

    Returns:
        Adjusted position size in USD
    """
    base_size = compute_position_size(kelly_f, bankroll, max_size)

    # Apply volatility adjustment (higher volatility = smaller position)
    if volatility is not None:
        volatility = float(np.clip(volatility, 0, 1))
        volatility_multiplier = 1.0 - (volatility * 0.5)  # Up to 50% reduction
        base_size *= volatility_multiplier

    # Apply confidence adjustment (lower confidence = smaller position)
    if confidence is not None:
        confidence = float(np.clip(confidence, 0, 1))
        # Confidence should be > 0.5 to justify position
        if confidence < 0.5:
            base_size *= 0.5  # 50% reduction if confidence < 0.5
        else:
            base_size *= confidence

    # Final cap
    base_size = min(base_size, max_size)
    base_size = max(base_size, 0.0)

    return float(base_size)
