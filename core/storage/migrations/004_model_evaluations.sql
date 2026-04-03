-- Model evaluation results tracking
-- Stores metrics from training runs for model performance monitoring

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
    calibration_data TEXT,  -- JSON blob of calibration bins
    feature_importance TEXT,  -- JSON blob
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_model_evaluations_name ON model_evaluations(model_name);
CREATE INDEX IF NOT EXISTS idx_model_evaluations_trained_at ON model_evaluations(trained_at);
CREATE INDEX IF NOT EXISTS idx_model_evaluations_accuracy ON model_evaluations(accuracy);
