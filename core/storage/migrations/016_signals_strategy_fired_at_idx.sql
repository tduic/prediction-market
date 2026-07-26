-- Add composite index on signals(strategy, fired_at) for dashboard strategy queries.
-- The get_strategies endpoint groups by strategy and filters by fired_at >= ?
-- in two queries per request. A composite (strategy, fired_at) index covers
-- both the WHERE strategy = ? AND fired_at >= ? pattern and the
-- WHERE fired_at >= ? GROUP BY strategy aggregation.
CREATE INDEX IF NOT EXISTS idx_signals_strategy_fired_at ON signals(strategy, fired_at);
