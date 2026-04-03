#!/usr/bin/env python3
"""
Model training CLI script.

Trains prediction models on historical trade outcomes and evaluates performance.
Supports training individual models or all registered models.

Usage:
    python scripts/train_models.py --model arbitrage --days 90 --db prediction_market.db
    python scripts/train_models.py --model all --days 180
    python scripts/train_models.py --model calibration
"""

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import get_config
from core.models.training import ModelTrainer

logger = logging.getLogger(__name__)


# Available models - load gracefully to handle missing dependencies
MODELS = {}

try:
    from core.models.calibration import CalibrationModel

    MODELS["calibration"] = CalibrationModel
except ImportError as e:
    logger.warning(f"Could not import CalibrationModel: {e}")

try:
    from core.models.cpi import CPIModel

    MODELS["cpi"] = CPIModel
except ImportError as e:
    logger.warning(f"Could not import CPIModel: {e}")

try:
    from core.models.fomc import FOMCModel

    MODELS["fomc"] = FOMCModel
except ImportError as e:
    logger.warning(f"Could not import FOMCModel: {e}")


def setup_logging(log_level: str = "INFO") -> None:
    """Configure logging."""
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )


def get_db_connection(db_path: str) -> sqlite3.Connection:
    """Open database connection."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def check_model_evaluations_table(db: sqlite3.Connection) -> bool:
    """Check if model_evaluations table exists, create if not."""
    cursor = db.execute("""
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='model_evaluations'
        """)

    if cursor.fetchone():
        return True

    # Create table if missing
    logger.warning("model_evaluations table not found, creating...")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS model_evaluations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_name TEXT NOT NULL,
            trained_at TEXT NOT NULL DEFAULT (datetime('now')),
            dataset_size INTEGER,
            test_size INTEGER,
            accuracy REAL,
            precision_score REAL,
            recall REAL,
            f1_score REAL,
            brier_score REAL,
            calibration_data TEXT,
            feature_importance TEXT,
            notes TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_model_evaluations_name ON model_evaluations(model_name);
        CREATE INDEX IF NOT EXISTS idx_model_evaluations_trained_at ON model_evaluations(trained_at);
        """)
    db.commit()
    return True


def train_model(
    model_name: str, db_path: str, days: int = 90, test_ratio: float = 0.2
) -> dict[str, Any]:
    """
    Train a single model.

    Args:
        model_name: Model identifier (must be in MODELS)
        db_path: Path to SQLite database
        days: Lookback period for training data
        test_ratio: Test set ratio

    Returns:
        Training results dict

    Raises:
        ValueError: If model not found or training fails
    """
    if model_name not in MODELS:
        raise ValueError(
            f"Unknown model: {model_name}. Available: {', '.join(MODELS.keys())}"
        )

    logger.info(f"Training model: {model_name}")

    # Initialize model
    ModelClass = MODELS[model_name]
    model = ModelClass()

    # Open database
    db = get_db_connection(db_path)

    try:
        # Create trainer
        trainer = ModelTrainer(model, db)

        # Prepare dataset
        logger.info(f"Preparing dataset (last {days} days)...")
        X, y = trainer.prepare_dataset(days=days, limit=10000)

        if len(X) < 10:
            raise ValueError(f"Insufficient data for training: {len(X)} samples")

        # Split data
        trainer.X_train = X
        trainer.y_train = y
        trainer.train_test_split(test_ratio=test_ratio)

        # Train
        logger.info("Training model...")
        trainer.train()

        # Evaluate
        logger.info("Evaluating model...")
        results = trainer.evaluate(threshold=0.5)

        # Save metrics
        logger.info("Saving metrics to database...")
        trainer.save_metrics(db)

        logger.info(f"Training complete for {model_name}")

        return {
            "model_name": model_name,
            "success": True,
            "results": results,
        }

    except Exception as e:
        logger.error(f"Training failed for {model_name}: {e}", exc_info=True)
        return {
            "model_name": model_name,
            "success": False,
            "error": str(e),
        }

    finally:
        db.close()


def train_all_models(
    db_path: str, days: int = 90, test_ratio: float = 0.2
) -> list[dict[str, Any]]:
    """
    Train all registered models.

    Args:
        db_path: Path to SQLite database
        days: Lookback period for training data
        test_ratio: Test set ratio

    Returns:
        List of training results
    """
    results = []
    for model_name in MODELS.keys():
        result = train_model(model_name, db_path, days=days, test_ratio=test_ratio)
        results.append(result)

    return results


def print_results_table(results: list[dict[str, Any]]) -> None:
    """Print training results as formatted table."""
    print("\n" + "=" * 100)
    print("MODEL TRAINING RESULTS")
    print("=" * 100)

    for result in results:
        model_name = result["model_name"]
        success = result["success"]

        print(f"\nModel: {model_name}")
        print("-" * 100)

        if not success:
            print("Status: FAILED")
            print(f"Error: {result.get('error', 'Unknown error')}")
            continue

        res = result["results"]
        print("Status: SUCCESS")
        print(f"  Dataset size: {res['train_samples'] + res['test_samples']}")
        print(f"  Train/Test split: {res['train_samples']} / {res['test_samples']}")
        print(f"  Accuracy: {res['accuracy']:.4f}")
        print(f"  Precision: {res['precision']:.4f}")
        print(f"  Recall: {res['recall']:.4f}")
        print(f"  F1 Score: {res['f1_score']:.4f}")
        print(f"  Brier Score: {res['brier_score']:.4f}")
        print(f"  MAE: {res['mae']:.4f}")

        # Confusion matrix
        print("\n  Confusion Matrix:")
        print(f"    True Positives:  {res['true_positives']:6d}")
        print(f"    False Positives: {res['false_positives']:6d}")
        print(f"    True Negatives:  {res['true_negatives']:6d}")
        print(f"    False Negatives: {res['false_negatives']:6d}")

        # Calibration bins
        if res["calibration_bins"]:
            print("\n  Calibration Bins (showing first 5):")
            for i, bin_data in enumerate(res["calibration_bins"][:5]):
                print(
                    f"    Bin {bin_data['bin_index']}: "
                    f"pred={bin_data['pred_mean']:.3f}, "
                    f"obs={bin_data['observed_frequency']:.3f}, "
                    f"n={bin_data['count']}"
                )

    print("\n" + "=" * 100 + "\n")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Train prediction models on historical trade outcomes"
    )

    parser.add_argument(
        "--model",
        type=str,
        default="all",
        help=f"Model to train: 'all' or one of {{{', '.join(MODELS.keys())}}}",
    )

    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Lookback period in days (default: 90)",
    )

    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database (default: from config)",
    )

    parser.add_argument(
        "--test-ratio",
        type=float,
        default=0.2,
        help="Test set ratio (default: 0.2)",
    )

    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Logging level (default: INFO)",
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)

    # Get database path
    if args.db:
        db_path = args.db
    else:
        config = get_config()
        db_path = config.database.db_path

    logger.info(f"Using database: {db_path}")

    # Verify database exists
    if not Path(db_path).exists():
        logger.error(f"Database not found: {db_path}")
        sys.exit(1)

    # Open DB and check for model_evaluations table
    db = get_db_connection(db_path)
    check_model_evaluations_table(db)
    db.close()

    # Train models
    if args.model == "all":
        logger.info(
            f"Training all models (days={args.days}, test_ratio={args.test_ratio})"
        )
        results = train_all_models(db_path, days=args.days, test_ratio=args.test_ratio)
    else:
        logger.info(f"Training model: {args.model} (days={args.days})")
        results = [
            train_model(args.model, db_path, days=args.days, test_ratio=args.test_ratio)
        ]

    # Output results
    if args.json:
        # JSON output
        json_results = []
        for result in results:
            json_result = {
                "model_name": result["model_name"],
                "success": result["success"],
            }
            if result["success"]:
                res = result["results"]
                json_result["metrics"] = {
                    "accuracy": res["accuracy"],
                    "precision": res["precision"],
                    "recall": res["recall"],
                    "f1_score": res["f1_score"],
                    "brier_score": res["brier_score"],
                    "mae": res["mae"],
                    "train_samples": res["train_samples"],
                    "test_samples": res["test_samples"],
                }
            else:
                json_result["error"] = result.get("error")

            json_results.append(json_result)

        print(json.dumps(json_results, indent=2))
    else:
        # Table output
        print_results_table(results)

    # Exit with error code if any training failed
    if any(not r["success"] for r in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
