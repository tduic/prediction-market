-- Add CLOB token IDs to markets table for Polymarket live execution.
--
-- Polymarket's CLOB API requires the ERC-1155 token_id (77-78 digit decimal)
-- for order placement. Gamma API returns these as the `clobTokenIds` JSON
-- array: [yes_token_id, no_token_id]. Previously we only stored the
-- gamma numeric `id` (or fell back to it when `conditionId` was missed due
-- to a camelCase/snake_case mismatch), which cannot be used for live orders
-- or for CLOB price lookups via `GET /markets/{conditionId}`.
--
-- Both columns are nullable; the ingestor populates them when available
-- and paper/live clients treat NULL as "no live trading info".

ALTER TABLE markets ADD COLUMN yes_token_id TEXT;
ALTER TABLE markets ADD COLUMN no_token_id TEXT;

CREATE INDEX IF NOT EXISTS idx_markets_yes_token_id ON markets(yes_token_id);
CREATE INDEX IF NOT EXISTS idx_markets_no_token_id ON markets(no_token_id);
