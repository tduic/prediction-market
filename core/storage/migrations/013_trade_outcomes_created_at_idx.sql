-- Add index on trade_outcomes(created_at) for daily-loss range queries.
-- The circuit breaker and risk checks query this column on every signal check.
CREATE INDEX IF NOT EXISTS idx_trade_outcomes_created_at ON trade_outcomes(created_at);
