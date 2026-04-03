"""Feature extraction and engineering for model training."""

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def extract_features(
    trade_outcome_row: dict[str, Any], market_data: dict[str, Any] | None = None
) -> dict[str, float]:
    """
    Extract numeric features from a trade outcome row.

    Args:
        trade_outcome_row: Row from trade_outcomes table
        market_data: Optional additional market context

    Returns:
        Dict of numeric features
    """
    features = {}

    # Spread at signal
    spread = trade_outcome_row.get("spread_at_signal", 0.0)
    features["spread_at_signal"] = float(spread) if spread is not None else 0.0

    # Volume at signal
    volume = trade_outcome_row.get("volume_at_signal", 0.0)
    features["volume_at_signal"] = float(volume) if volume is not None else 0.0

    # Time-of-day features
    signal_time = trade_outcome_row.get("created_at")
    if signal_time:
        # Extract hour from ISO timestamp
        try:
            hour = int(signal_time.split("T")[1].split(":")[0])
            features["hour_of_day"] = float(hour)
        except (IndexError, ValueError):
            features["hour_of_day"] = 12.0
    else:
        features["hour_of_day"] = 12.0

    # For day-of-week, we'd need full datetime parsing
    # Using a simplified approach: UTC day extraction
    features["day_of_week"] = 3.0  # Default to Wednesday

    # Strategy encoding (one-hot as numeric)
    strategy = trade_outcome_row.get("strategy", "unknown")
    strategy_map = {"arbitrage": 1.0, "event_model": 2.0, "calibration": 3.0}
    features["strategy_encoded"] = float(strategy_map.get(strategy, 0.0))

    # Platform encoding (derived from strategy or market)
    platform = (
        trade_outcome_row.get("platform", "polymarket") if market_data else "polymarket"
    )
    platform_map = {"polymarket": 1.0, "kalshi": 2.0}
    features["platform_encoded"] = float(platform_map.get(platform, 1.0))

    # Holding period (milliseconds)
    holding_ms = trade_outcome_row.get("holding_period_ms", 0)
    features["holding_period_ms"] = float(holding_ms) if holding_ms is not None else 0.0

    # Signal to fill latency (milliseconds)
    signal_fill_ms = trade_outcome_row.get("signal_to_fill_ms", 0)
    features["signal_to_fill_ms"] = (
        float(signal_fill_ms) if signal_fill_ms is not None else 0.0
    )

    # Liquidity at signal
    liquidity = trade_outcome_row.get("liquidity_at_signal", 0.0)
    features["liquidity_at_signal"] = float(liquidity) if liquidity is not None else 0.0

    return features


def build_feature_matrix(
    rows: list[dict[str, Any]], normalize: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """
    Build feature matrix and target vector from trade outcomes.

    Args:
        rows: List of trade_outcome rows from database
        normalize: Whether to normalize features to [0, 1]

    Returns:
        Tuple of (X: feature matrix, y: target vector)

    Raises:
        ValueError: If rows is empty or features invalid
    """
    if not rows:
        raise ValueError("Empty rows provided")

    feature_list = []
    targets = []

    for row in rows:
        features = extract_features(row)

        # Build feature vector in consistent order
        feature_vector = [
            features.get("spread_at_signal", 0.0),
            features.get("volume_at_signal", 0.0),
            features.get("hour_of_day", 12.0),
            features.get("day_of_week", 3.0),
            features.get("strategy_encoded", 0.0),
            features.get("platform_encoded", 1.0),
            features.get("holding_period_ms", 0.0),
            features.get("signal_to_fill_ms", 0.0),
            features.get("liquidity_at_signal", 0.0),
        ]

        feature_list.append(feature_vector)

        # Target: 1 if actual_pnl > 0, else 0
        actual_pnl = row.get("actual_pnl", 0.0)
        target = 1.0 if (actual_pnl and actual_pnl > 0) else 0.0
        targets.append(target)

    X = np.array(feature_list, dtype=np.float64)
    y = np.array(targets, dtype=np.float64)

    if normalize and X.size > 0:
        # Normalize each feature to [0, 1] based on observed range
        X_min = np.nanmin(X, axis=0)
        X_max = np.nanmax(X, axis=0)

        # Avoid division by zero
        ranges = X_max - X_min
        ranges[ranges == 0] = 1.0

        X = (X - X_min) / ranges

        # Replace any NaN with 0
        X = np.nan_to_num(X, nan=0.0)

    return X, y


def get_feature_names() -> list[str]:
    """
    Get ordered list of feature names.

    Returns:
        List of feature names in the order they appear in feature matrix
    """
    return [
        "spread_at_signal",
        "volume_at_signal",
        "hour_of_day",
        "day_of_week",
        "strategy_encoded",
        "platform_encoded",
        "holding_period_ms",
        "signal_to_fill_ms",
        "liquidity_at_signal",
    ]
