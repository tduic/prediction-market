-- Migration 012: order book columns
--
-- Polymarket CLOB markets have two independent books (YES / NO token).
-- Every Polymarket order we submit now records which book it hit, and
-- positions track which book they're long. This enables the
-- no-naked-shorts translation path: strategy-intent SELL on Polymarket
-- becomes a BUY on the NO book when we don't hold YES inventory.
--
-- Existing rows backfill to 'YES' via DEFAULT — correct for every
-- Polymarket row written before this migration (all YES-book), and
-- harmless for Kalshi rows (which only have a YES book anyway).
--
-- markets.last_price_no holds the NO-book snapshot price from
-- tokens[1].price for paper execution fidelity on translated fills.
-- Kalshi rows leave it NULL.

ALTER TABLE orders ADD COLUMN book TEXT NOT NULL DEFAULT 'YES' CHECK(book IN ('YES', 'NO'));

ALTER TABLE positions ADD COLUMN book TEXT NOT NULL DEFAULT 'YES' CHECK(book IN ('YES', 'NO'));

ALTER TABLE markets ADD COLUMN last_price_no REAL;

CREATE INDEX IF NOT EXISTS idx_positions_market_book_status ON positions(market_id, book, status);
