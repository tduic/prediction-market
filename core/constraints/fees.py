"""Fee estimation and management for prediction market platforms."""

from dataclasses import dataclass


@dataclass
class FeeConfig:
    """Configuration for platform-specific fees."""

    polymarket: float = 0.02
    kalshi: float = 0.02
    manifold: float = 0.01
    metaculus: float = 0.00


DEFAULT_FEE_CONFIG = FeeConfig()


class FeeEstimator:
    """Estimates trading fees per platform."""

    def __init__(self, config: FeeConfig = DEFAULT_FEE_CONFIG):
        """
        Initialize fee estimator.

        Args:
            config: FeeConfig with per-platform rates
        """
        self.config = config
        self._platform_rates: dict[str, float] = {
            "polymarket": config.polymarket,
            "kalshi": config.kalshi,
            "manifold": config.manifold,
            "metaculus": config.metaculus,
        }

    def estimate_fee(
        self, platform: str, side: str, price: float, size: float
    ) -> float:
        """
        Estimate fee for a single trade.

        Args:
            platform: Platform name (polymarket, kalshi, manifold, metaculus)
            side: Trade side (buy or sell)
            price: Market price (0.0 to 1.0 for prediction markets)
            size: Number of shares

        Returns:
            Estimated fee amount

        Raises:
            ValueError: If platform not recognized
        """
        if platform.lower() not in self._platform_rates:
            raise ValueError(
                f"Unknown platform '{platform}'. "
                f"Supported: {list(self._platform_rates.keys())}"
            )

        if not 0.0 <= price <= 1.0:
            raise ValueError(f"Price must be between 0.0 and 1.0, got {price}")

        if size < 0:
            raise ValueError(f"Size must be non-negative, got {size}")

        rate = self._platform_rates[platform.lower()]
        # Fee is typically calculated as percentage of notional value
        # Notional value = price * size
        fee = rate * price * size
        return fee

    def estimate_spread_cost(
        self,
        platform_a: str,
        side_a: str,
        price_a: float,
        platform_b: str,
        side_b: str,
        price_b: float,
        size: float,
    ) -> float:
        """
        Estimate total fee cost for a two-leg arbitrage.

        Args:
            platform_a: First platform
            side_a: Side on first platform (buy/sell)
            price_a: Price on first platform
            platform_b: Second platform
            side_b: Side on second platform
            price_b: Price on second platform
            size: Size for both legs

        Returns:
            Total fee cost
        """
        fee_a = self.estimate_fee(platform_a, side_a, price_a, size)
        fee_b = self.estimate_fee(platform_b, side_b, price_b, size)
        return fee_a + fee_b

    def calculate_net_spread(
        self,
        raw_spread: float,
        platform_a: str,
        side_a: str,
        price_a: float,
        platform_b: str,
        side_b: str,
        price_b: float,
        size: float,
    ) -> float:
        """
        Calculate net spread after accounting for fees.

        Args:
            raw_spread: Price difference before fees
            platform_a: First platform
            side_a: Side on first platform
            price_a: Price on first platform
            platform_b: Second platform
            side_b: Side on second platform
            price_b: Price on second platform
            size: Trade size

        Returns:
            Net spread (raw_spread - total_fees)
        """
        total_fees = self.estimate_spread_cost(
            platform_a, side_a, price_a, platform_b, side_b, price_b, size
        )
        return raw_spread - total_fees
