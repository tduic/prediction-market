"""Model training and evaluation pipeline."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np

from core.models.base import BaseModel
from core.models.evaluation import (
    brier_score,
    calibration_bins,
    classification_metrics,
    mean_absolute_error,
)
from core.models.features import build_feature_matrix, get_feature_names

logger = logging.getLogger(__name__)


class SimpleLogisticRegression:
    """
    Lightweight logistic regression implementation without sklearn.

    Fits via gradient descent with optional L2 regularization.
    """

    def __init__(
        self,
        learning_rate: float = 0.1,
        max_iterations: int = 1000,
        l2_reg: float = 0.01,
    ):
        """
        Initialize logistic regression.

        Args:
            learning_rate: Learning rate for gradient descent
            max_iterations: Maximum iterations for training
            l2_reg: L2 regularization coefficient
        """
        self.learning_rate = learning_rate
        self.max_iterations = max_iterations
        self.l2_reg = l2_reg
        self.weights: np.ndarray | None = None
        self.bias: float = 0.0
        self.loss_history: list[float] = []

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        """
        Fit logistic regression model.

        Args:
            X: Feature matrix (n_samples, n_features)
            y: Target vector (n_samples,)
        """
        n_samples, n_features = X.shape

        # Initialize weights and bias
        self.weights = np.zeros(n_features)
        self.bias = 0.0

        self.loss_history = []

        for iteration in range(self.max_iterations):
            # Forward pass: compute logits and probabilities
            logits = np.dot(X, self.weights) + self.bias
            predictions = self._sigmoid(logits)

            # Compute loss (binary cross-entropy + L2 regularization)
            # Avoid log(0) by clipping
            eps = 1e-15
            predictions = np.clip(predictions, eps, 1 - eps)

            ce_loss = -np.mean(
                y * np.log(predictions) + (1 - y) * np.log(1 - predictions)
            )
            l2_loss = (self.l2_reg / (2 * n_samples)) * np.sum(self.weights**2)
            loss = ce_loss + l2_loss

            self.loss_history.append(float(loss))

            # Compute gradients
            errors = predictions - y
            dw = (1 / n_samples) * np.dot(X.T, errors) + (
                self.l2_reg / n_samples
            ) * self.weights
            db = (1 / n_samples) * np.sum(errors)

            # Update parameters
            self.weights -= self.learning_rate * dw
            self.bias -= self.learning_rate * db

            # Early stopping if loss plateaus
            if iteration > 100 and len(self.loss_history) > 10:
                recent_loss = np.mean(self.loss_history[-10:])
                old_loss = np.mean(self.loss_history[-20:-10])
                if abs(recent_loss - old_loss) < 1e-6:
                    logger.debug(f"Early stopping at iteration {iteration}")
                    break

        logger.info(
            f"Logistic regression fitted. Final loss: {self.loss_history[-1]:.6f}"
        )

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """
        Predict probabilities.

        Args:
            X: Feature matrix

        Returns:
            Array of probabilities [0, 1]
        """
        if self.weights is None:
            raise RuntimeError("Model must be fitted before prediction")

        logits = np.dot(X, self.weights) + self.bias
        return self._sigmoid(logits)

    @staticmethod
    def _sigmoid(x: np.ndarray) -> np.ndarray:
        """Sigmoid activation function."""
        return 1.0 / (1.0 + np.exp(-np.clip(x, -500, 500)))


class ModelTrainer:
    """
    Model training pipeline with dataset preparation, splitting, and evaluation.

    Supports any BaseModel subclass and handles full training workflow.
    """

    def __init__(self, model: BaseModel, db_connection: Any = None):
        """
        Initialize trainer.

        Args:
            model: A BaseModel subclass instance
            db_connection: Database connection for fetching data and saving results
        """
        self.model = model
        self.db = db_connection
        self.X_train = None
        self.y_train = None
        self.X_test = None
        self.y_test = None
        self.train_indices = None
        self.test_indices = None
        self.evaluation_results = None
        self._sklearn_model = None  # For models that use sklearn internally

    def prepare_dataset(
        self, days: int = 90, limit: int | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """
        Fetch historical data and build feature matrix.

        Queries trade_outcomes and market_prices tables for the last N days,
        extracts features, and returns feature matrix and target vector.

        Args:
            days: Lookback period in days (default 90)
            limit: Maximum number of records to fetch (default None for no limit)

        Returns:
            Tuple of (X, y) where X is feature matrix and y is target vector

        Raises:
            RuntimeError: If database connection not available
            ValueError: If insufficient data
        """
        if not self.db:
            raise RuntimeError("Database connection required for dataset preparation")

        import sqlite3
        from datetime import timedelta

        # Calculate cutoff date
        cutoff_date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        # Query trade outcomes
        query = """
        SELECT
            id, signal_id, strategy, violation_id,
            market_id_a, market_id_b, predicted_edge, predicted_pnl,
            actual_pnl, fees_total, edge_captured_pct,
            signal_to_fill_ms, holding_period_ms,
            spread_at_signal, volume_at_signal, liquidity_at_signal,
            resolved_at, created_at
        FROM trade_outcomes
        WHERE resolved_at > ?
        ORDER BY created_at DESC
        """

        if limit:
            query += f" LIMIT {limit}"

        try:
            cursor = self.db.execute(query, (cutoff_date,))
            rows = cursor.fetchall()
        except (sqlite3.Error, AttributeError) as e:
            logger.error(f"Database query failed: {e}")
            raise RuntimeError(f"Failed to fetch training data: {e}")

        if not rows:
            raise ValueError(
                f"No trade outcomes found in last {days} days. Cannot prepare dataset."
            )

        # Convert rows to dicts if needed
        if rows and not isinstance(rows[0], dict):
            # aiosqlite returns Row objects, convert to dict
            columns = [
                "id",
                "signal_id",
                "strategy",
                "violation_id",
                "market_id_a",
                "market_id_b",
                "predicted_edge",
                "predicted_pnl",
                "actual_pnl",
                "fees_total",
                "edge_captured_pct",
                "signal_to_fill_ms",
                "holding_period_ms",
                "spread_at_signal",
                "volume_at_signal",
                "liquidity_at_signal",
                "resolved_at",
                "created_at",
            ]
            rows = [dict(zip(columns, row)) for row in rows]

        logger.info(f"Prepared dataset with {len(rows)} trade outcomes")

        # Build feature matrix
        X, y = build_feature_matrix(rows, normalize=True)

        logger.info(
            f"Feature matrix shape: {X.shape}, target distribution: {np.bincount(y.astype(int))}"
        )

        return X, y

    def train_test_split(
        self, test_ratio: float = 0.2
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """
        Temporal train-test split (not random).

        Most recent test_ratio% becomes test set, earlier data becomes training set.

        Args:
            test_ratio: Fraction of data for testing (0 to 1)

        Returns:
            Tuple of (X_train, X_test, y_train, y_test)

        Raises:
            RuntimeError: If dataset not prepared
        """
        if self.X_train is None:
            raise RuntimeError("Dataset must be prepared first via prepare_dataset()")

        n_samples = len(self.X_train)
        split_idx = int(n_samples * (1 - test_ratio))

        # Temporal split: earlier data is train, recent data is test
        self.train_indices = np.arange(0, split_idx)
        self.test_indices = np.arange(split_idx, n_samples)

        X_full = self.X_train
        y_full = self.y_train

        self.X_train = X_full[:split_idx]
        self.X_test = X_full[split_idx:]
        self.y_train = y_full[:split_idx]
        self.y_test = y_full[split_idx:]

        logger.info(
            f"Train-test split: {len(self.X_train)} train, {len(self.X_test)} test "
            f"(ratio {test_ratio:.1%})"
        )

        return self.X_train, self.X_test, self.y_train, self.y_test

    def train(self) -> None:
        """
        Fit model on training data.

        For simple probabilistic models, uses internal logistic regression.
        For advanced models (e.g., calibration), may use custom fitting.

        Raises:
            RuntimeError: If dataset not prepared
        """
        if self.X_train is None:
            raise RuntimeError("Dataset must be prepared and split first")

        # For most models, use simple logistic regression
        if not self._sklearn_model:
            self._sklearn_model = SimpleLogisticRegression(
                learning_rate=0.01, max_iterations=1000, l2_reg=0.001
            )

        self._sklearn_model.fit(self.X_train, self.y_train)
        logger.info(f"Model {self.model.name} trained successfully")

    def predict(self, X: np.ndarray) -> np.ndarray:
        """
        Generate predictions on new data.

        Args:
            X: Feature matrix

        Returns:
            Array of probabilities [0, 1]

        Raises:
            RuntimeError: If model not trained
        """
        if self._sklearn_model is None:
            raise RuntimeError("Model must be trained first via train()")

        return self._sklearn_model.predict_proba(X)

    def evaluate(self, threshold: float = 0.5) -> dict[str, Any]:
        """
        Evaluate model on test set.

        Computes accuracy, precision, recall, F1, Brier score, and calibration.

        Args:
            threshold: Probability threshold for classification metrics

        Returns:
            Dict with evaluation results:
            {
                'accuracy': float,
                'precision': float,
                'recall': float,
                'f1_score': float,
                'brier_score': float,
                'mae': float,
                'calibration_bins': list[dict],
                'train_samples': int,
                'test_samples': int,
                'threshold': float
            }

        Raises:
            RuntimeError: If model not trained or test set not available
        """
        if self.X_test is None:
            raise RuntimeError("Test set not available. Call train_test_split() first.")

        if self._sklearn_model is None:
            raise RuntimeError("Model must be trained first via train()")

        # Get predictions
        y_pred = self.predict(self.X_test)

        # Classification metrics
        clf_metrics = classification_metrics(y_pred, self.y_test, threshold=threshold)

        # Calibration
        brier = brier_score(y_pred, self.y_test)
        mae = mean_absolute_error(y_pred, self.y_test)
        cal_bins = calibration_bins(y_pred, self.y_test, n_bins=10)

        self.evaluation_results = {
            "accuracy": clf_metrics["accuracy"],
            "precision": clf_metrics["precision"],
            "recall": clf_metrics["recall"],
            "f1_score": clf_metrics["f1_score"],
            "brier_score": brier,
            "mae": mae,
            "calibration_bins": cal_bins,
            "train_samples": len(self.X_train),
            "test_samples": len(self.X_test),
            "threshold": threshold,
            "true_positives": clf_metrics["true_positives"],
            "false_positives": clf_metrics["false_positives"],
            "true_negatives": clf_metrics["true_negatives"],
            "false_negatives": clf_metrics["false_negatives"],
        }

        logger.info(
            f"Evaluation complete: accuracy={clf_metrics['accuracy']:.3f}, "
            f"precision={clf_metrics['precision']:.3f}, "
            f"recall={clf_metrics['recall']:.3f}, "
            f"brier={brier:.4f}"
        )

        return self.evaluation_results

    def save_metrics(self, db: Any) -> bool:
        """
        Save evaluation results to model_evaluations table.

        Args:
            db: Database connection

        Returns:
            True if save successful

        Raises:
            RuntimeError: If evaluation not completed
            sqlite3.Error: If database write fails
        """
        if not self.evaluation_results:
            raise RuntimeError("No evaluation results to save. Call evaluate() first.")

        if not db:
            raise RuntimeError("Database connection required")

        import sqlite3

        results = self.evaluation_results

        # Prepare data for insertion
        model_name = self.model.name
        trained_at = datetime.now(timezone.utc).isoformat()
        dataset_size = results["train_samples"] + results["test_samples"]
        test_size = results["test_samples"]
        accuracy = results["accuracy"]
        precision = results["precision"]
        recall = results["recall"]
        f1_score = results["f1_score"]
        brier = results["brier_score"]
        calibration_json = json.dumps(results["calibration_bins"])
        feature_importance_json = json.dumps({"features": get_feature_names()})
        notes = f"Temporal split {test_size}/{dataset_size}, threshold={results['threshold']}"

        query = """
        INSERT INTO model_evaluations (
            model_name, trained_at, dataset_size, test_size,
            accuracy, precision_score, recall, f1_score, brier_score,
            calibration_data, feature_importance, notes
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        try:
            db.execute(
                query,
                (
                    model_name,
                    trained_at,
                    dataset_size,
                    test_size,
                    accuracy,
                    precision,
                    recall,
                    f1_score,
                    brier,
                    calibration_json,
                    feature_importance_json,
                    notes,
                ),
            )
            db.commit()
            logger.info(f"Saved evaluation metrics for {model_name} to database")
            return True
        except sqlite3.Error as e:
            logger.error(f"Failed to save metrics: {e}")
            raise
