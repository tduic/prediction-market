-- Phase 4: Realistic paper fill model
-- Adds pnl_model column to positions.
-- Existing closed positions (written by the old synthetic fill model) are
-- retroactively tagged 'synthetic' so the dashboard can split the numbers.

ALTER TABLE positions ADD COLUMN pnl_model TEXT DEFAULT 'realistic';

UPDATE positions
   SET pnl_model = 'synthetic'
 WHERE closed_at IS NOT NULL
   AND pnl_model IS NULL;
