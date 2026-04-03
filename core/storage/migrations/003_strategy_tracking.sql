-- Migration 003: Improve per-strategy performance tracking (idempotent)
--
-- NOTE: The ALTER TABLE below is extracted and executed separately by
-- db.py _apply_alter_statements() so it can handle "duplicate column"
-- errors gracefully. Tests that apply migrations via raw executescript
-- must handle this line too.

ALTER TABLE orders ADD COLUMN strategy TEXT;

CREATE INDEX IF NOT EXISTS idx_orders_strategy ON orders(strategy);

-- Normalized per-strategy PnL snapshots
CREATE TABLE IF NOT EXISTS strategy_pnl_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    realized_pnl REAL DEFAULT 0,
    unrealized_pnl REAL DEFAULT 0,
    fees REAL DEFAULT 0,
    trade_count INTEGER DEFAULT 0,
    win_count INTEGER DEFAULT 0,
    FOREIGN KEY (snapshot_id) REFERENCES pnl_snapshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_strategy_pnl_snapshot_id ON strategy_pnl_snapshots(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_strategy_pnl_strategy ON strategy_pnl_snapshots(strategy);
CREATE INDEX IF NOT EXISTS idx_strategy_pnl_composite ON strategy_pnl_snapshots(snapshot_id, strategy);
