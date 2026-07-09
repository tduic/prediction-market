-- Migration 014: Composite index for stuck pending order detection.
-- The reconciliation check queries WHERE status='pending' AND submitted_at < ?
-- on every reconciliation cycle. A composite (status, submitted_at) index lets
-- SQLite satisfy both predicates in one index seek-and-scan rather than scanning
-- the full idx_orders_status result set, then filtering on submitted_at.
-- Complements the TEXT comparison fix in _check_stuck_pending_orders that
-- replaced CAST(submitted_at AS INTEGER) so the range predicate is indexable.
CREATE INDEX IF NOT EXISTS idx_orders_status_submitted_at ON orders(status, submitted_at);
