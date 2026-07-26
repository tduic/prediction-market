-- Add composite index on orders(signal_id, market_id, submitted_at) for the
-- orphaned-positions reconciliation check. The check uses a window function
-- PARTITION BY signal_id, market_id ORDER BY submitted_at DESC to find the
-- latest order per position pair. This composite index allows SQLite to use
-- an index scan instead of a full table sort for the window function.
CREATE INDEX IF NOT EXISTS idx_orders_signal_market_submitted ON orders(signal_id, market_id, submitted_at);
