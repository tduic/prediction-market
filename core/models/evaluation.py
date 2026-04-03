"""Evaluation metrics and calibration utilities for model validation."""

import logging
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


def brier_score(predictions: np.ndarray, actuals: np.ndarray) -> float:
    """
    Compute Brier score (mean squared error of probabilities).

    Lower is better. Range [0, 1], perfect score is 0.

    Args:
        predictions: Array of predicted probabilities [0, 1]
        actuals: Array of actual outcomes {0, 1}

    Returns:
        Brier score

    Raises:
        ValueError: If shapes don't match or values out of range
    """
    if predictions.shape != actuals.shape:
        raise ValueError(
            f"Shape mismatch: predictions {predictions.shape} vs actuals {actuals.shape}"
        )

    if len(predictions) == 0:
        return 0.0

    # Ensure predictions are in [0, 1]
    predictions = np.clip(predictions, 0.0, 1.0)

    # Compute MSE
    score = float(np.mean((predictions - actuals) ** 2))
    return score


def calibration_bins(
    predictions: np.ndarray, actuals: np.ndarray, n_bins: int = 10
) -> list[dict[str, Any]]:
    """
    Group predictions into bins and compute calibration metrics.

    Returns observed frequency vs predicted probability for each bin.

    Args:
        predictions: Array of predicted probabilities [0, 1]
        actuals: Array of actual outcomes {0, 1}
        n_bins: Number of bins to create

    Returns:
        List of dicts with bin statistics:
        [
            {
                'bin_index': 0,
                'pred_min': 0.0,
                'pred_max': 0.1,
                'pred_mean': 0.05,
                'observed_frequency': 0.12,
                'count': 42
            },
            ...
        ]
    """
    if len(predictions) == 0:
        return []

    predictions = np.clip(predictions, 0.0, 1.0)

    # Create bin edges
    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)

    calibration_data = []

    for i in range(n_bins):
        bin_min = bin_edges[i]
        bin_max = bin_edges[i + 1]

        # Find predictions in this bin
        mask = (predictions >= bin_min) & (predictions <= bin_max)
        count = int(np.sum(mask))

        if count == 0:
            continue

        bin_preds = predictions[mask]
        bin_actuals = actuals[mask]

        calibration_data.append(
            {
                "bin_index": i,
                "pred_min": float(bin_min),
                "pred_max": float(bin_max),
                "pred_mean": float(np.mean(bin_preds)),
                "observed_frequency": float(np.mean(bin_actuals)),
                "count": count,
            }
        )

    return calibration_data


def classification_metrics(
    predictions: np.ndarray, actuals: np.ndarray, threshold: float = 0.5
) -> dict[str, float]:
    """
    Compute classification metrics at a given probability threshold.

    Args:
        predictions: Array of predicted probabilities [0, 1]
        actuals: Array of actual outcomes {0, 1}
        threshold: Probability threshold for positive class

    Returns:
        Dict with metrics:
        {
            'accuracy': float,
            'precision': float,
            'recall': float,
            'f1_score': float,
            'true_positives': int,
            'false_positives': int,
            'true_negatives': int,
            'false_negatives': int
        }

    Raises:
        ValueError: If inputs invalid
    """
    if predictions.shape != actuals.shape:
        raise ValueError(
            f"Shape mismatch: predictions {predictions.shape} vs actuals {actuals.shape}"
        )

    if len(predictions) == 0:
        return {
            "accuracy": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1_score": 0.0,
            "true_positives": 0,
            "false_positives": 0,
            "true_negatives": 0,
            "false_negatives": 0,
        }

    predictions = np.clip(predictions, 0.0, 1.0)
    predicted_labels = (predictions >= threshold).astype(int)

    tp = int(np.sum((predicted_labels == 1) & (actuals == 1)))
    fp = int(np.sum((predicted_labels == 1) & (actuals == 0)))
    tn = int(np.sum((predicted_labels == 0) & (actuals == 0)))
    fn = int(np.sum((predicted_labels == 0) & (actuals == 1)))

    accuracy = (
        float((tp + tn) / (tp + tn + fp + fn)) if (tp + tn + fp + fn) > 0 else 0.0
    )

    precision = float(tp / (tp + fp)) if (tp + fp) > 0 else 0.0

    recall = float(tp / (tp + fn)) if (tp + fn) > 0 else 0.0

    f1 = (
        float(2 * (precision * recall) / (precision + recall))
        if (precision + recall) > 0
        else 0.0
    )

    return {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1_score": f1,
        "true_positives": tp,
        "false_positives": fp,
        "true_negatives": tn,
        "false_negatives": fn,
    }


def mean_absolute_error(predictions: np.ndarray, actuals: np.ndarray) -> float:
    """
    Compute mean absolute error.

    Args:
        predictions: Array of predictions
        actuals: Array of actual values

    Returns:
        MAE value
    """
    if len(predictions) == 0:
        return 0.0

    if predictions.shape != actuals.shape:
        raise ValueError(
            f"Shape mismatch: predictions {predictions.shape} vs actuals {actuals.shape}"
        )

    return float(np.mean(np.abs(predictions - actuals)))
