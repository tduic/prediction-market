-- Phase 0 baseline snapshot: captures system state at T0 before any fixes.
-- One row per snapshot call; each is a point-in-time record.
-- Used to compare "before / after" across remediation phases.

CREATE TABLE IF NOT EXISTS phase0_baseline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_timestamp TEXT NOT NULL,
    pair_count INTEGER NOT NULL DEFAULT 0,
    active_pair_count INTEGER NOT NULL DEFAULT 0,
    p1_realized_pnl REAL DEFAULT 0,
    p2_realized_pnl REAL DEFAULT 0,
    p3_realized_pnl REAL DEFAULT 0,
    p4_realized_pnl REAL DEFAULT 0,
    p5_realized_pnl REAL DEFAULT 0,
    total_realized_pnl REAL DEFAULT 0,
    total_trade_count INTEGER DEFAULT 0,
    notes TEXT DEFAULT 'phase0'
);

CREATE INDEX IF NOT EXISTS idx_phase0_baseline_ts ON phase0_baseline(snapshot_timestamp);
