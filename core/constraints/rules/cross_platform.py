"""Cross-platform constraint rule for prediction markets.

Detects identical-event spread violations across different platforms.
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


@dataclass
class FeeConfig:
    """Fee configuration for net spread calculation."""

    polymarket: float = 0.02
    kalshi: float = 0.02
    manifold: float = 0.01
    metaculus: float = 0.00


def _get_platform_fee(platform: str, config: FeeConfig) -> float:
    """Get fee rate for a platform."""
    platform_lower = platform.lower()
    if platform_lower == "polymarket":
        return config.polymarket
    elif platform_lower == "kalshi":
        return config.kalshi
    elif platform_lower == "manifold":
        return config.manifold
    elif platform_lower == "metaculus":
        return config.metaculus
    else:
        raise ValueError(f"Unknown platform: {platform}")


def check(
    price_a: float,
    price_b: float,
    platform_a: str,
    platform_b: str,
    min_net_spread_threshold: float = 0.03,
    fee_config: Optional[FeeConfig] = None,
) -> Optional[ViolationInfo]:
    """
    Check cross-platform spread constraint for identical events.

    When the same event is traded on two platforms, the price difference
    (raw spread) minus estimated fees on both sides (net spread) should
    exceed the minimum profitable spread threshold.

    Args:
        price_a: Price on platform A (0.0 to 1.0)
        price_b: Price on platform B (0.0 to 1.0)
        platform_a: Name of first platform
        platform_b: Name of second platform
        min_net_spread_threshold: Minimum profitable spread threshold (default 0.03 = 3%)
        fee_config: FeeConfig for platform-specific fees

    Returns:
        ViolationInfo if net spread below threshold, None otherwise

    Raises:
        ValueError: If prices outside [0, 1] or platforms unknown
    """
    if not 0.0 <= price_a <= 1.0:
        raise ValueError(f"price_a must be in [0, 1], got {price_a}")
    if not 0.0 <= price_b <= 1.0:
        raise ValueError(f"price_b must be in [0, 1], got {price_b}")
    if min_net_spread_threshold < 0:
        raise ValueError(
            f"min_net_spread_threshold must be non-negative, "
            f"got {min_net_spread_threshold}"
        )

    if fee_config is None:
        fee_config = FeeConfig()

    # Raw spread between platforms
    raw_spread = abs(price_a - price_b)

    # Estimate fees on both platforms
    # Fees are charged on the notional amount transacted
    # For a cross-platform arbitrage, we need to pay fees on both sides
    fee_a = _get_platform_fee(platform_a, fee_config) * max(price_a, price_b)
    fee_b = _get_platform_fee(platform_b, fee_config) * max(price_a, price_b)
    total_fees = fee_a + fee_b

    # Net spread is raw spread minus fees
    net_spread = raw_spread - total_fees

    if net_spread < min_net_spread_threshold:
        return ViolationInfo(
            rule_type="cross_platform",
            severity="warning",
            description=(
                f"Cross-platform spread too tight for profitable arbitrage. "
                f"{platform_a}: {price_a:.4f}, {platform_b}: {price_b:.4f}. "
                f"Raw spread: {raw_spread:.4f}. Total fees: {total_fees:.4f}. "
                f"Net spread: {net_spread:.4f}. "
                f"Threshold: {min_net_spread_threshold:.4f}."
            ),
            implied_arbitrage=net_spread * 100,
        )

    return None
