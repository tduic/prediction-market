"""Abstract base class for all prediction models."""

import logging
from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


class BaseModel(ABC):
    """Abstract base class for all prediction models."""

    def __init__(self, min_training_samples: int = 30):
        """
        Initialize model.

        Args:
            min_training_samples: Minimum samples required for training
        """
        self.min_training_samples = min_training_samples
        self._is_trained = False

    @property
    @abstractmethod
    def name(self) -> str:
        """Model name identifier."""
        pass

    @property
    @abstractmethod
    def version(self) -> str:
        """Model version string."""
        pass

    @abstractmethod
    def train(self, data: pd.DataFrame) -> None:
        """
        Train the model.

        Args:
            data: Training dataframe

        Raises:
            ValueError: If data is invalid
        """
        pass

    @abstractmethod
    def predict(self, features: dict[str, Any]) -> float:
        """
        Generate probability prediction.

        Args:
            features: Feature dict

        Returns:
            Probability between 0 and 1
        """
        pass

    @abstractmethod
    def validate_data(self, data: pd.DataFrame) -> bool:
        """
        Validate training data.

        Args:
            data: Data to validate

        Returns:
            True if data is valid
        """
        pass

    def is_trained(self) -> bool:
        """Check if model has been trained."""
        return self._is_trained

    def requires_retraining(self) -> bool:
        """Check if model needs retraining."""
        return not self._is_trained

    def _validate_min_samples(self, data: pd.DataFrame) -> bool:
        """Check if data has minimum required samples."""
        if len(data) < self.min_training_samples:
            logger.warning(
                f"Insufficient samples for {self.name}: {len(data)} < {self.min_training_samples}"
            )
            return False
        return True
