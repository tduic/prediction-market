"""FOMC rate decision model."""

import logging
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression
from sklearn.model_selection import cross_val_score

from core.models.base import BaseModel

logger = logging.getLogger(__name__)


class FOMCModel(BaseModel):
    """
    FOMC rate decision model.

    Regresses final market price against CME FedWatch implied probability
    with additional features for Fed communications and calendar effects.
    """

    MODEL_NAME = "fomc_rate_decision"
    MODEL_VERSION = "1.0.0"

    REQUIRED_FEATURES = [
        "cme_implied_prob",
        "days_to_decision",
        "recent_fed_speakers_hawkish_pct",
    ]

    def __init__(self, min_training_samples: int = 30):
        """Initialize FOMC model."""
        super().__init__(min_training_samples=min_training_samples)
        self.regressor = LinearRegression()

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
        Validate training data has required columns and types.

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
        if "final_market_price" not in data.columns:
            logger.error("Missing target column: final_market_price")
            return False

        # Check data types and ranges
        try:
            for col in self.REQUIRED_FEATURES + ["final_market_price"]:
                if not pd.api.types.is_numeric_dtype(data[col]):
                    logger.error(f"Column {col} is not numeric")
                    return False

            # Validate probability ranges
            if not (data["cme_implied_prob"].between(0, 1).all()):
                logger.error("cme_implied_prob must be between 0 and 1")
                return False

            if not (data["final_market_price"].between(0, 1).all()):
                logger.error("final_market_price must be between 0 and 1")
                return False

        except (KeyError, ValueError) as e:
            logger.error(f"Data validation error: {e}")
            return False

        return True

    def train(self, data: pd.DataFrame) -> None:
        """
        Train model using walk-forward validation.

        Args:
            data: Training data with required features and target

        Raises:
            ValueError: If data is invalid
        """
        if not self.validate_data(data):
            raise ValueError("Invalid training data")

        X = data[self.REQUIRED_FEATURES].values
        y = data["final_market_price"].values

        # Fit model
        self.regressor.fit(X, y)

        # Validate with cross-validation
        cv_scores = cross_val_score(self.regressor, X, y, cv=5, scoring="r2")

        logger.info(
            f"FOMC model trained. CV R²: {cv_scores.mean():.4f} (+/- {cv_scores.std():.4f})"
        )

        self._is_trained = True

    def predict(self, features: dict[str, Any]) -> float:
        """
        Generate FOMC rate decision probability.

        Args:
            features: Dict with required features

        Returns:
            Probability between 0 and 1

        Raises:
            RuntimeError: If model not trained
            ValueError: If features invalid
        """
        if not self._is_trained:
            raise RuntimeError("Model must be trained before prediction")

        # Extract and validate features
        try:
            feature_values = np.array(
                [[features.get(f, 0.0) for f in self.REQUIRED_FEATURES]]
            )
        except (KeyError, TypeError) as e:
            raise ValueError(f"Invalid features: {e}")

        # Predict and constrain to [0, 1]
        prediction = self.regressor.predict(feature_values)[0]
        prediction = float(np.clip(prediction, 0.0, 1.0))

        return prediction

    def walk_forward_validation(
        self, data: pd.DataFrame, window: int = 60
    ) -> dict[str, float]:
        """
        Perform walk-forward validation for robustness.

        Args:
            data: Full historical data
            window: Training window size

        Returns:
            Dict with validation metrics
        """
        if not self.validate_data(data):
            raise ValueError("Invalid training data")

        predictions = []
        actuals = []

        for i in range(window, len(data)):
            train_data = data.iloc[i - window : i]
            test_data = data.iloc[i : i + 1]

            X_train = train_data[self.REQUIRED_FEATURES].values
            y_train = train_data["final_market_price"].values

            X_test = test_data[self.REQUIRED_FEATURES].values
            y_test = test_data["final_market_price"].values

            # Fit on training window
            model = LinearRegression()
            model.fit(X_train, y_train)

            # Predict on test point
            pred = model.predict(X_test)[0]
            pred = float(np.clip(pred, 0.0, 1.0))

            predictions.append(pred)
            actuals.append(y_test[0])

        # Calculate metrics
        predictions = np.array(predictions)
        actuals = np.array(actuals)

        mse = float(np.mean((predictions - actuals) ** 2))
        rmse = float(np.sqrt(mse))
        mae = float(np.mean(np.abs(predictions - actuals)))

        return {
            "mse": mse,
            "rmse": rmse,
            "mae": mae,
            "samples": len(predictions),
        }
