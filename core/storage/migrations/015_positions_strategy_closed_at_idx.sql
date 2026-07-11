-- Migration 015: Composite index for per-strategy killswitch query.
-- _get_strategy_rolling_pnl in single_platform.py runs this query every
-- scheduler cycle for each active strategy:
--
--   SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0.0)
--   FROM positions
--   WHERE strategy=? AND pnl_model='realistic' AND status='closed' AND closed_at >= ?
--
-- Existing indexes cover status and opened_at but not closed_at or strategy.
-- This composite index lets SQLite seek directly to (strategy, 'closed') and
-- do a bounded range scan on closed_at without a full table scan.
-- Column ordering: equality on strategy (high cardinality) first, equality on
-- status (low cardinality) second, range predicate on closed_at last.
CREATE INDEX IF NOT EXISTS idx_positions_strategy_status_closed_at
    ON positions(strategy, status, closed_at);
