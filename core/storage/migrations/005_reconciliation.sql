-- Reconciliation tracking table for balance and position reconciliation
-- Tracks discrepancies between local state and exchange state

CREATE TABLE IF NOT EXISTS reconciliation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    check_type TEXT NOT NULL,
    local_value REAL NOT NULL,
    exchange_value REAL,
    discrepancy REAL,
    status TEXT NOT NULL,
    detail TEXT,
    action_taken TEXT,
    checked_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reconciliation_log_platform ON reconciliation_log(platform);
CREATE INDEX IF NOT EXISTS idx_reconciliation_log_status ON reconciliation_log(status);
CREATE INDEX IF NOT EXISTS idx_reconciliation_log_checked_at ON reconciliation_log(checked_at);
