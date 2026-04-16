"""Historical calibration curve model."""

import logging
from typing import Any

import numpy as np
import pandas as pd
from scipy.interpolate import interp1d

from core.models.base import BaseModel

logger = logging.getLogger(__name__)


class CalibrationModel(BaseModel):
    """
    Calibration curve model for probability adjustment.

    Groups resolved markets by category and probability decile,
    identifies systematic biases (tail underpricing, round number
    clustering), and returns calibrated probabilities.
    """

    MODEL_NAME = "calibration"
    MODEL_VERSION = "1.0.0"

    DECILES = 10

    def __init__(self, min_training_samples: int = 30):
        """Initialize calibration model."""
        super().__init__(min_training_samples=min_training_samples)
        self.calibration_curves: dict[str, Any] = {}
        self.bias_adjustments: dict[str, dict[str, float]] = {}

    @property
    def name(self) -> str:
        """Model name."""
        return self.MODEL_NAME

    @property
    def version(self) -> str:
        """Model version."""
        return self.MODEL_VERSION

    def validate_data(self, data: pd.DataFrame) -> bool:
        """
        Validate training data.

        Args:
            data: Historical resolved market data

        Returns:
            True if valid
        """
        if not self._validate_min_samples(data):
            return False

        required_cols = ["final_market_price", "resolved_outcome", "category"]
        missing = set(required_cols) - set(data.columns)
        if missing:
            logger.error(f"Missing required columns: {missing}")
            return False

        try:
            # Validate probability and outcome columns
            if not (data["final_market_price"].between(0, 1).all()):
                logger.error("final_market_price must be between 0 and 1")
                return False

            if not (data["resolved_outcome"].between(0, 1).all()):
                logger.error("resolved_outcome must be 0 or 1")
                return False

        except (KeyError, ValueError) as e:
            logger.error(f"Data validation error: {e}")
            return False

        return True

    def train(self, data: pd.DataFrame) -> None:
        """
        Train calibration curves by category.

        Args:
            data: Historical resolved market data

        Raises:
            ValueError: If data is invalid
        """
        if not self.validate_data(data):
            raise ValueError("Invalid training data")

        categories = data["category"].unique()

        for category in categories:
            cat_data = data[data["category"] == category]
            if len(cat_data) >= self.min_training_samples:
                curve, biases = self._compute_calibration(cat_data)
                self.calibration_curves[category] = curve
                self.bias_adjustments[category] = biases

        logger.info(
            f"Calibration model trained for {len(self.calibration_curves)} categories"
        )

        self._is_trained = bool(self.calibration_curves)

    def predict(self, features: dict[str, Any]) -> float:
        """
        Adjust probability based on calibration curve.

        Args:
            features: Dict with 'raw_probability' and optional 'category'

        Returns:
            Calibrated probability between 0 and 1

        Raises:
            RuntimeError: If model not trained
        """
        if not self._is_trained:
            raise RuntimeError("Model must be trained before prediction")

        raw_prob = features.get("raw_probability", 0.5)
        category = features.get("category", "default")

        # Clamp input
        raw_prob = float(np.clip(raw_prob, 0.0, 1.0))

        # Get category curve or use global
        if category in self.calibration_curves:
            curve = self.calibration_curves[category]
            try:
                adjusted = float(curve(raw_prob))
            except (ValueError, RuntimeError):
                adjusted = raw_prob
        else:
            adjusted = raw_prob

        # Apply bias adjustments conditionally based on probability regime
        if category in self.bias_adjustments:
            biases = self.bias_adjustments[category]
            if biases.get("apply_adjustment", False):
                if adjusted < 0.3 or adjusted > 0.7:
                    adjusted += biases.get("tail_bias", 0.0)
                if abs(adjusted - round(adjusted * 4) / 4) < 0.02:
                    adjusted += biases.get("round_bias", 0.0)

        return float(np.clip(adjusted, 0.0, 1.0))

    def _compute_calibration(
        self, cat_data: pd.DataFrame
    ) -> tuple[interp1d, dict[str, float]]:
        """
        Compute calibration curve for a category.

        Args:
            cat_data: Data for single category

        Returns:
            (interpolation function, bias dict)
        """
        try:
            # Bin by deciles
            cat_data = cat_data.copy()
            cat_data["decile"] = pd.qcut(
                cat_data["final_market_price"], q=self.DECILES, duplicates="drop"
            )

            # Compute bin statistics
            bin_means = []
            bin_outcomes = []

            for decile in cat_data["decile"].unique():
                bin_data = cat_data[cat_data["decile"] == decile]
                mean_price = bin_data["final_market_price"].mean()
                mean_outcome = bin_data["resolved_outcome"].mean()

                bin_means.append(mean_price)
                bin_outcomes.append(mean_outcome)

            sort_idx = np.argsort(bin_means)
            bin_means_arr = np.array(bin_means)[sort_idx]
            bin_outcomes_arr = np.array(bin_outcomes)[sort_idx]

            # Create interpolation function
            try:
                curve = interp1d(
                    bin_means_arr,
                    bin_outcomes_arr,
                    kind="linear",
                    fill_value="extrapolate",
                    bounds_error=False,
                )
            except ValueError:
                # Fallback if insufficient data
                def curve(x):
                    return x

            # Identify systematic biases
            biases = self._detect_biases(bin_means_arr, bin_outcomes_arr)

            return curve, biases
        except ValueError as e:
            logger.warning("pd.qcut failed for category, using identity curve: %s", e)
            return (lambda x: x), {}

    def _detect_biases(self, prices: np.ndarray, outcomes: np.ndarray) -> dict:
        """
        Detect systematic biases in calibration.

        Args:
            prices: Expected probabilities
            outcomes: Actual outcomes

        Returns:
            Bias adjustment dict
        """
        errors = outcomes - prices

        # Check tail bias (prices < 0.3 or > 0.7)
        tail_mask = (prices < 0.3) | (prices > 0.7)
        tail_error = float(np.mean(errors[tail_mask])) if tail_mask.any() else 0.0

        # Check round number bias (prices at 0.25, 0.5, 0.75)
        round_mask = np.abs(prices - np.round(prices * 4) / 4) < 0.02
        round_error = float(np.mean(errors[round_mask])) if round_mask.any() else 0.0

        return {
            "tail_bias": tail_error,
            "round_bias": round_error,
            "apply_adjustment": abs(tail_error) > 0.05 or abs(round_error) > 0.05,
        }
