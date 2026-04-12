-- Phase 7: Invariant violation log
-- Stores a record each time check_all_invariants detects a failure.
-- Never auto-cleared — requires human review.

CREATE TABLE IF NOT EXISTS invariant_violations (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    message    TEXT NOT NULL,
    severity   TEXT NOT NULL DEFAULT 'critical',
    violated_at TEXT NOT NULL
);
