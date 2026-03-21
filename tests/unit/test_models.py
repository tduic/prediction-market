"""
Unit tests for prediction model service.

Tests model training, prediction, calibration, and lifecycle management.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock
from typing import List, Any


# ============================================================================
# Model Service Classes (Mock Implementations)
# ============================================================================


class TrainingData:
    """Container for model training data."""

    def __init__(self, samples: list[dict[str, float]]):
        self.samples = samples
        self.count = len(samples)

    def is_sufficient(self, min_samples: int) -> bool:
        """Check if training data meets minimum sample requirement."""
        return self.count >= min_samples


class FOCMModel:
    """FOMC rate decision prediction model."""

    def __init__(self):
        self.is_trained = False
        self.feature_weights = {}
        self.accuracy = 0.0

    def train(self, training_data: TrainingData, min_samples: int = 30) -> bool:
        """
        Train the FOMC model.

        Args:
            training_data: Training data with sample histories
            min_samples: Minimum samples required

        Returns:
            True if training successful, False if insufficient data

        Raises:
            ValueError: If data is insufficient
        """
        if training_data.count < min_samples:
            raise ValueError(
                f"Insufficient data: {training_data.count} < {min_samples}"
            )

        # Mock training: compute simple feature weights
        self.feature_weights = {
            "inflation": 0.4,
            "employment": 0.3,
            "gdp_growth": 0.2,
            "forward_guidance": 0.1,
        }

        self.is_trained = True
        self.accuracy = 0.68  # Typical FOMC prediction accuracy

        return True

    def predict(self, features: dict[str, float]) -> float:
        """
        Predict probability of rate cut.

        Args:
            features: Feature dict with inflation, employment, etc.

        Returns:
            Probability of rate cut (0-1)
        """
        if not self.is_trained:
            raise RuntimeError("Model not trained")

        # Weighted sum of features
        prediction = sum(
            self.feature_weights.get(key, 0) * features.get(key, 0)
            for key in self.feature_weights
        )

        # Sigmoid-like squashing to 0-1
        return min(max(prediction, 0.0), 1.0)


class CalibrationModel:
    """Market price calibration model."""

    def __init__(self):
        self.calibration_curves = {}
        self.is_trained = False

    def compute_calibration_curves(
        self,
        resolved_market_history: list[dict[str, Any]],
    ) -> bool:
        """
        Compute calibration curves from resolved market outcomes.

        Args:
            resolved_market_history: List of resolved market data

        Returns:
            True if curves computed successfully
        """
        if not resolved_market_history:
            return False

        # Mock calibration: extract statistics
        prices = [m.get("resolved_price", 0.5) for m in resolved_market_history]
        outcomes = [m.get("resolved_outcome", 0) for m in resolved_market_history]

        if not prices or not outcomes:
            return False

        # Compute simple calibration metrics
        self.calibration_curves = {
            "mean_price": sum(prices) / len(prices),
            "resolution_rate": sum(outcomes) / len(outcomes),
            "price_range": (min(prices), max(prices)),
        }

        self.is_trained = True
        return True

    def get_calibration_curve(self) -> dict[str, Any]:
        """Retrieve computed calibration curve."""
        return self.calibration_curves


class ModelRegistry:
    """Manage model lifecycle: deploy, retire, versioning."""

    def __init__(self):
        self.models = {}
        self.active_model = None

    def deploy_model(
        self,
        model_id: str,
        model: Any,
        version: str = "1.0",
    ) -> bool:
        """
        Deploy a model to production.

        Args:
            model_id: Unique model identifier
            model: Model instance
            version: Model version

        Returns:
            True if deployment successful
        """
        self.models[model_id] = {
            "model": model,
            "version": version,
            "deployed_at": datetime.now(timezone.utc),
            "status": "active",
        }

        # Make this the active model
        self.active_model = model_id

        return True

    def retire_model(self, model_id: str) -> bool:
        """
        Retire a model from production.

        Args:
            model_id: Model to retire

        Returns:
            True if retirement successful
        """
        if model_id not in self.models:
            return False

        self.models[model_id]["status"] = "retired"

        # If retiring active model, clear it
        if self.active_model == model_id:
            self.active_model = None

        return True

    def get_active_model(self) -> Any:
        """Get currently active model."""
        if self.active_model:
            return self.models[self.active_model]["model"]
        return None

    def list_models(self, status: str = None) -> list[dict[str, Any]]:
        """
        List all models, optionally filtered by status.

        Args:
            status: Filter by status (active, retired)

        Returns:
            List of model metadata
        """
        if status:
            return [m for m in self.models.values() if m.get("status") == status]
        return list(self.models.values())


class WalkForwardValidator:
    """Walk-forward validation to prevent data leakage."""

    def __init__(self):
        self.train_window_end = None
        self.test_window_start = None

    def set_windows(
        self,
        train_window_end: datetime,
        test_window_start: datetime,
    ) -> bool:
        """
        Set training and test windows.

        Args:
            train_window_end: Last timestamp in training set
            test_window_start: First timestamp in test set

        Returns:
            True if windows properly ordered
        """
        if train_window_end >= test_window_start:
            return False

        self.train_window_end = train_window_end
        self.test_window_start = test_window_start

        return True

    def validate_no_leakage(
        self,
        training_data: list[dict[str, Any]],
        test_data: list[dict[str, Any]],
    ) -> bool:
        """
        Verify no data leakage between train/test windows.

        Args:
            training_data: Training samples
            test_data: Test samples

        Returns:
            True if no leakage detected
        """
        if not self.train_window_end or not self.test_window_start:
            return False

        # Check all training data is before window end
        for sample in training_data:
            ts = sample.get("timestamp")
            if ts and ts > self.train_window_end:
                return False

        # Check all test data is after window start
        for sample in test_data:
            ts = sample.get("timestamp")
            if ts and ts < self.test_window_start:
                return False

        return True


# ============================================================================
# Test Cases
# ============================================================================


class TestFOCMModel:
    """Test FOMC rate prediction model."""

    def test_fomc_model_rejects_insufficient_data(self, sample_config):
        """Model rejects training with insufficient data."""
        model = FOCMModel()

        # Only 10 samples, but minimum is 30
        insufficient_data = TrainingData(
            [{"inflation": 0.03, "employment": 0.04} for _ in range(10)]
        )

        with pytest.raises(ValueError):
            model.train(insufficient_data, min_samples=30)

    def test_fomc_model_trains_and_predicts(self, sample_config):
        """Model trains with sufficient data and makes predictions."""
        model = FOCMModel()

        # Generate 50 training samples
        training_data = TrainingData(
            [
                {
                    "inflation": 0.02 + i * 0.001,
                    "employment": 0.04,
                    "gdp_growth": 0.03,
                    "forward_guidance": 0.5,
                }
                for i in range(50)
            ]
        )

        result = model.train(training_data, min_samples=30)

        assert result is True
        assert model.is_trained is True

        # Make prediction
        prediction = model.predict(
            {
                "inflation": 0.03,
                "employment": 0.04,
                "gdp_growth": 0.03,
                "forward_guidance": 0.6,
            }
        )

        assert 0.0 <= prediction <= 1.0

    def test_fomc_model_predictions_vary_with_features(self):
        """Model produces different predictions for different features."""
        model = FOCMModel()

        training_data = TrainingData(
            [
                {
                    "inflation": 0.02 + i * 0.001,
                    "employment": 0.04,
                    "gdp_growth": 0.03,
                    "forward_guidance": 0.5,
                }
                for i in range(50)
            ]
        )

        model.train(training_data, min_samples=30)

        # High inflation should suggest higher rate cut probability
        pred_high_inflation = model.predict(
            {
                "inflation": 0.05,
                "employment": 0.04,
                "gdp_growth": 0.03,
                "forward_guidance": 0.5,
            }
        )

        # Low inflation
        pred_low_inflation = model.predict(
            {
                "inflation": 0.01,
                "employment": 0.04,
                "gdp_growth": 0.03,
                "forward_guidance": 0.5,
            }
        )

        # Should differ
        assert abs(pred_high_inflation - pred_low_inflation) > 0

    def test_fomc_model_untrained_rejection(self):
        """Untrained model rejects prediction requests."""
        model = FOCMModel()

        with pytest.raises(RuntimeError):
            model.predict({"inflation": 0.03, "employment": 0.04})

    def test_fomc_model_at_minimum_data(self):
        """Model trains with exactly minimum samples."""
        model = FOCMModel()

        data = TrainingData(
            [{"inflation": 0.03, "employment": 0.04} for _ in range(30)]
        )

        result = model.train(data, min_samples=30)

        assert result is True
        assert model.is_trained is True


class TestCalibrationModel:
    """Test market price calibration model."""

    def test_calibration_model_computes_curves(self):
        """Model computes calibration curves from history."""
        model = CalibrationModel()

        history = [
            {
                "market_id": "m_001",
                "resolved_price": 0.65,
                "resolved_outcome": 1,
            },
            {
                "market_id": "m_002",
                "resolved_price": 0.72,
                "resolved_outcome": 1,
            },
            {
                "market_id": "m_003",
                "resolved_price": 0.45,
                "resolved_outcome": 0,
            },
        ]

        result = model.compute_calibration_curves(history)

        assert result is True
        assert model.is_trained is True
        assert "mean_price" in model.calibration_curves
        assert "resolution_rate" in model.calibration_curves

    def test_calibration_model_empty_history(self):
        """Empty history produces no calibration curves."""
        model = CalibrationModel()

        result = model.compute_calibration_curves([])

        assert result is False
        assert model.is_trained is False

    def test_calibration_model_extracts_statistics(self):
        """Model correctly extracts statistics from history."""
        model = CalibrationModel()

        history = [
            {"resolved_price": 0.50, "resolved_outcome": 0},
            {"resolved_price": 0.70, "resolved_outcome": 1},
            {"resolved_price": 0.80, "resolved_outcome": 1},
        ]

        model.compute_calibration_curves(history)

        curves = model.get_calibration_curve()

        # Mean price = (0.50 + 0.70 + 0.80) / 3 = 0.667
        assert abs(curves["mean_price"] - (0.50 + 0.70 + 0.80) / 3) < 0.01

        # Resolution rate = 2/3
        assert abs(curves["resolution_rate"] - 2 / 3) < 0.01


class TestModelRegistry:
    """Test model lifecycle management."""

    def test_model_registry_deploy_and_retire(self):
        """Deploy and retire models through registry."""
        registry = ModelRegistry()
        model = FOCMModel()

        # Deploy
        result = registry.deploy_model("fomc_v1", model, version="1.0")

        assert result is True
        assert registry.active_model == "fomc_v1"

        # Retire
        result = registry.retire_model("fomc_v1")

        assert result is True
        assert registry.models["fomc_v1"]["status"] == "retired"
        assert registry.active_model is None

    def test_model_registry_multiple_versions(self):
        """Track multiple model versions."""
        registry = ModelRegistry()

        model_v1 = FOCMModel()
        model_v2 = FOCMModel()

        registry.deploy_model("fomc_v1", model_v1, version="1.0")
        registry.deploy_model("fomc_v2", model_v2, version="2.0")

        models = registry.list_models(status="active")

        assert len(models) == 2
        assert registry.active_model == "fomc_v2"

    def test_model_registry_get_active_model(self):
        """Retrieve active model."""
        registry = ModelRegistry()
        model = FOCMModel()

        registry.deploy_model("fomc_v1", model)

        active = registry.get_active_model()

        assert active is model

    def test_model_registry_retire_active_clears_reference(self):
        """Retiring active model clears active reference."""
        registry = ModelRegistry()
        model = FOCMModel()

        registry.deploy_model("fomc_v1", model)
        assert registry.active_model == "fomc_v1"

        registry.retire_model("fomc_v1")
        assert registry.active_model is None

    def test_model_registry_list_by_status(self):
        """List models filtered by status."""
        registry = ModelRegistry()

        model1 = FOCMModel()
        model2 = FOCMModel()

        registry.deploy_model("v1", model1)
        registry.deploy_model("v2", model2)
        registry.retire_model("v1")

        active = registry.list_models(status="active")
        retired = registry.list_models(status="retired")

        assert len(active) == 1
        assert len(retired) == 1

    def test_model_registry_retire_nonexistent(self):
        """Retiring nonexistent model returns False."""
        registry = ModelRegistry()

        result = registry.retire_model("nonexistent")

        assert result is False


class TestWalkForwardValidation:
    """Test walk-forward validation for data leakage prevention."""

    def test_walk_forward_no_data_leakage(self):
        """Properly separated windows have no leakage."""
        validator = WalkForwardValidator()

        now = datetime.now(timezone.utc)
        train_end = now - timedelta(days=1)
        test_start = now

        result = validator.set_windows(train_end, test_start)

        assert result is True

        training_data = [
            {"timestamp": now - timedelta(days=2)},
            {"timestamp": now - timedelta(days=1, hours=1)},
        ]

        test_data = [
            {"timestamp": now},
            {"timestamp": now + timedelta(days=1)},
        ]

        valid = validator.validate_no_leakage(training_data, test_data)

        assert valid is True

    def test_walk_forward_detects_train_leakage(self):
        """Training data after window end is detected."""
        validator = WalkForwardValidator()

        now = datetime.now(timezone.utc)
        train_end = now - timedelta(days=1)
        test_start = now

        validator.set_windows(train_end, test_start)

        # Training data leaks into test window
        training_data = [
            {"timestamp": now + timedelta(hours=1)},  # After train_end!
        ]

        test_data = [{"timestamp": now}]

        valid = validator.validate_no_leakage(training_data, test_data)

        assert valid is False

    def test_walk_forward_detects_test_leakage(self):
        """Test data before window start is detected."""
        validator = WalkForwardValidator()

        now = datetime.now(timezone.utc)
        train_end = now - timedelta(days=1)
        test_start = now

        validator.set_windows(train_end, test_start)

        training_data = [{"timestamp": now - timedelta(days=1, hours=1)}]

        # Test data leaks into training window
        test_data = [
            {"timestamp": now - timedelta(hours=1)},  # Before test_start!
        ]

        valid = validator.validate_no_leakage(training_data, test_data)

        assert valid is False

    def test_walk_forward_overlapping_windows_rejected(self):
        """Overlapping train/test windows are rejected."""
        validator = WalkForwardValidator()

        now = datetime.now(timezone.utc)
        train_end = now
        test_start = now - timedelta(hours=1)  # Overlaps!

        result = validator.set_windows(train_end, test_start)

        assert result is False

    def test_walk_forward_validation_without_windows(self):
        """Validation without set windows returns False."""
        validator = WalkForwardValidator()

        training_data = [{"timestamp": datetime.now(timezone.utc)}]
        test_data = [{"timestamp": datetime.now(timezone.utc)}]

        result = validator.validate_no_leakage(training_data, test_data)

        assert result is False
