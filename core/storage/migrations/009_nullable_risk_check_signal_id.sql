-- Migration 009: Make risk_check_log.signal_id nullable.
--
-- Root cause: risk checks run BEFORE the signal row is inserted into signals,
-- so the FK constraint on signal_id can never be satisfied at log time.
-- The _RiskSignal proxy also auto-generated its own UUID, diverging from the
-- arb engine's signal_id. Making the column nullable is the correct contract:
-- risk checks log with NULL signal_id, preserving audit records even when no
-- signal is ultimately persisted (e.g., rejected trades).

-- SQLite does not support ALTER COLUMN, so recreate the table.
ALTER TABLE risk_check_log RENAME TO risk_check_log_old;

CREATE TABLE IF NOT EXISTS risk_check_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT,                          -- NULL when signal not yet persisted
    violation_id TEXT,
    check_type TEXT NOT NULL,
    passed INTEGER NOT NULL,
    check_value REAL,
    threshold REAL,
    detail TEXT,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY (signal_id) REFERENCES signals(id) ON DELETE CASCADE,
    FOREIGN KEY (violation_id) REFERENCES violations(id) ON DELETE SET NULL
);

INSERT INTO risk_check_log
    SELECT id, signal_id, violation_id, check_type, passed,
           check_value, threshold, detail, evaluated_at
    FROM risk_check_log_old;

DROP TABLE risk_check_log_old;

CREATE INDEX IF NOT EXISTS idx_risk_check_signal_id ON risk_check_log(signal_id);
CREATE INDEX IF NOT EXISTS idx_risk_check_type ON risk_check_log(check_type);
CREATE INDEX IF NOT EXISTS idx_risk_check_evaluated_at ON risk_check_log(evaluated_at);
