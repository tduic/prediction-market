"""CPI print prediction model using Bayesian updates."""

import logging
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm

from core.models.base import BaseModel

logger = logging.getLogger(__name__)


class CPIModel(BaseModel):
    """
    CPI print model with Bayesian updating.

    Uses Cleveland Fed Nowcast as prior and updates with
    consensus forecast and recent market prices.
    """

    MODEL_NAME = "cpi_print"
    MODEL_VERSION = "1.0.0"

    REQUIRED_FEATURES = [
        "cleveland_nowcast",
        "consensus_forecast",
        "previous_print",
    ]

    def __init__(self, min_training_samples: int = 30, prior_std: float = 0.15):
        """
        Initialize CPI model.

        Args:
            min_training_samples: Minimum samples for training
            prior_std: Prior standard deviation (Nowcast uncertainty)
        """
        super().__init__(min_training_samples=min_training_samples)
        self.prior_std = prior_std
        self.calibration_factor = 1.0

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
            data: Training data

        Returns:
            True if valid
        """
        if not self._validate_min_samples(data):
            return False

        # Check required columns
        missing = set(self.REQUIRED_FEATURES) - set(data.columns)
        if missing:
            logger.error(f"Missing required columns: {missing}")
            return False

        # Check target column
        if "actual_cpi_print" not in data.columns:
            logger.error("Missing target column: actual_cpi_print")
            return False

        # Check data types
        try:
            for col in self.REQUIRED_FEATURES + ["actual_cpi_print"]:
                if not pd.api.types.is_numeric_dtype(data[col]):
                    logger.error(f"Column {col} is not numeric")
                    return False
        except KeyError as e:
            logger.error(f"Column validation error: {e}")
            return False

        return True

    def train(self, data: pd.DataFrame) -> None:
        """
        Train model by calibrating against historical nowcasts.

        Args:
            data: Training data with nowcasts and actuals

        Raises:
            ValueError: If data is invalid
        """
        if not self.validate_data(data):
            raise ValueError("Invalid training data")

        # Compute calibration factor
        nowcasts = data["cleveland_nowcast"].values
        actuals = data["actual_cpi_print"].values

        # Calibration = mean absolute error
        errors = np.abs(nowcasts - actuals)
        self.calibration_factor = float(np.mean(errors))

        logger.info(
            f"CPI model trained. Calibration factor: {self.calibration_factor:.4f}"
        )

        self._is_trained = True

    def predict(self, features: dict[str, Any]) -> float:
        """
        Generate CPI probability using Bayesian update.

        Interprets as probability that CPI print > consensus forecast.

        Args:
            features: Dict with cleveland_nowcast, consensus_forecast, previous_print

        Returns:
            Probability between 0 and 1

        Raises:
            RuntimeError: If model not trained
            ValueError: If features invalid
        """
        if not self._is_trained:
            raise RuntimeError("Model must be trained before prediction")

        try:
            nowcast = features.get("cleveland_nowcast", 0.0)
            consensus = features.get("consensus_forecast", 0.0)
            previous = features.get("previous_print", 0.0)
        except (KeyError, TypeError) as e:
            raise ValueError(f"Invalid features: {e}")

        # Use Nowcast as prior (normal distribution)
        prior_mean = nowcast
        prior_std = self.prior_std

        # Likelihood: consensus as observation
        likelihood_mean = consensus
        likelihood_std = self.calibration_factor

        # Bayesian update: compute posterior
        posterior_precision = (1 / (prior_std**2)) + (1 / (likelihood_std**2))
        posterior_std = 1 / np.sqrt(posterior_precision)

        posterior_mean = posterior_std**2 * (
            (prior_mean / (prior_std**2)) + (likelihood_mean / (likelihood_std**2))
        )

        # Probability that actual > consensus
        z = (consensus - posterior_mean) / posterior_std
        probability = float(norm.sf(z))  # Survival function = P(X > consensus)

        return float(np.clip(probability, 0.0, 1.0))

    def update_prior(self, new_nowcast_std: float) -> None:
        """
        Update prior uncertainty.

        Args:
            new_nowcast_std: New prior standard deviation
        """
        self.prior_std = max(0.01, new_nowcast_std)
        logger.info(f"CPI model prior updated to {self.prior_std:.4f}")
