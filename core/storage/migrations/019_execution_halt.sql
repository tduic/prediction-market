-- Migration 019: persistent execution halt.
--
-- Unlike the daily-loss circuit breaker (which auto-resets at UTC midnight),
-- this halt is tripped by an unknown or unbalanced fill and stays set across
-- restarts until an operator clears it (scripts/clear_halt.py) after checking
-- both venues. Single row, id = 1.
CREATE TABLE IF NOT EXISTS execution_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    halted INTEGER NOT NULL DEFAULT 0 CHECK (halted IN (0, 1)),
    reason TEXT,
    component TEXT,
    halted_at TEXT,
    cleared_at TEXT,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO execution_control (id, halted, updated_at)
VALUES (1, 0, datetime('now'));
