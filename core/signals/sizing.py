"""Kelly fraction and position sizing calculations."""

import logging

logger = logging.getLogger(__name__)


def compute_kelly_fraction(
    edge: float,
    odds: float,
    kelly_fraction: float = 0.25,
) -> float:
    """
    Compute Kelly fraction for position sizing.

    For a binary prediction market bet at price ``odds`` (the price you pay
    per share), the correct Kelly formula is:

        f* = edge / (1 - odds)     [for a long / YES bet]

    where ``edge = fair_value - market_price``.

    When ``odds >= 1.0`` (e.g. pure arb callers that don't have a single
    market reference price), the function falls back to the heuristic
    ``edge / (1 - edge)`` to preserve backward compatibility.

    Args:
        edge: Edge as probability difference (fair_value - market_price).
            Positive = go long, negative = go short.
        odds: Bet price (0–1). Pass the YES price for a BUY, the NO price
            (1 - yes_price) for a SELL. Pass 1.0 to use the legacy arb
            heuristic when no single market price is available.
        kelly_fraction: Fractional Kelly multiplier (default 0.25 = quarter-Kelly).

    Returns:
        Kelly fraction in [-0.5, 0.5].
    """
    edge = float(edge)
    _use_market_price = float(odds) < 1.0
    odds = float(min(max(odds, 0.01), 0.99))

    if edge > 0:
        if _use_market_price:
            denominator = 1.0 - odds
        else:
            denominator = 1.0 - edge
        kelly_raw = edge / denominator if denominator > 0 else 0.5
    else:
        # Short position (flipped probabilities)
        kelly_raw = edge / (1.0 + edge) if edge > -1 else -0.5

    # Apply fractional Kelly (default quarter-Kelly)
    kelly_f = kelly_raw * kelly_fraction

    # Hard cap at 0.5 for safety
    kelly_f = float(min(max(kelly_f, -0.5), 0.5))

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
        "Position sizing: kelly_f=%.4f, bankroll=%.2f, size=%.2f",
        kelly_f,
        bankroll,
        position_size,
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
        volatility = float(min(max(volatility, 0), 1))
        volatility_multiplier = 1.0 - (volatility * 0.5)  # Up to 50% reduction
        base_size *= volatility_multiplier

    # Apply confidence adjustment (lower confidence = smaller position)
    if confidence is not None:
        confidence = float(min(max(confidence, 0), 1))
        # Confidence should be > 0.5 to justify position
        if confidence < 0.5:
            base_size *= 0.5  # 50% reduction if confidence < 0.5
        else:
            base_size *= confidence

    # Final cap
    base_size = min(base_size, max_size)
    base_size = max(base_size, 0.0)

    return float(base_size)
