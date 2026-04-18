# Polymarket No-Naked-Shorts: Inventory-Aware Book Resolution

**Status:** Design approved, pending spec review.
**Date:** 2026-04-18
**Scope:** One implementation plan. Orthogonal to Kalshi and to settlement/P&L.

## Motivation

Polymarket's CLOB does not permit naked shorts. A `SELL` on a token's order book requires that we already hold shares of that token. To *enter* a short position without inventory, the only legal expression is `BUY` on the opposing token's book at the complementary price (`BUY NO @ 1−p` instead of `SELL YES @ p`).

Today, `core/engine/arb_engine.py` fires balanced pairs: `BUY` on the cheap platform, `SELL` on the expensive one. When Polymarket is the expensive side (~50% of arb signals), the engine emits `OrderLeg(side=SELL, market_id=poly_<conditionId>)`. `PolymarketExecutionClient._resolve_token_id` always returns `yes_token_id`, so we send a naked-short YES to CLOB and it rejects.

Two consequences today:
1. Every arb signal where Polymarket is the expensive leg fails on the Polymarket side.
2. Future exit paths for single-platform strategies (P2–P5) that want to sell an acquired YES position have no path at all.

## Goal

Translate strategy-intent `SELL` legs on Polymarket into the correct CLOB operation based on inventory:

- **Inventory ≥ size** → `SELL` on the YES book (unwind existing long; frees capital).
- **Inventory < size** → `BUY` on the NO book at `1 − p` (fresh short; equivalent economic exposure).

All decisions live in a single resolver shared by live and paper clients. Strategy code stays oblivious.

## Non-Goals

- **NO-book price-aware routing** (option B2 from brainstorming). Comparing best-bid YES vs `1 − best-ask NO` to pick the cheaper fill is deferred. Requires subscribing to NO-token WS and new price-cache plumbing. Flagged as a follow-up (`TODO[B2]`).
- **Partial-inventory splitting.** If we hold 7 and want to SELL 10, we translate the whole order to `BUY NO 10` rather than split into `SELL YES 7 + BUY NO 3`. Single order path, simpler error handling.
- **Settlement / P&L branching on `book`.** Any downstream code that computes positional P&L against `resolution_outcome` must branch on YES vs NO. Not in scope for this spec beyond surfacing the column that downstream code will read.
- **Kalshi.** Not affected.

## Approach

### Enums

`execution/enums.py` (new):

```python
from enum import Enum

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class Book(str, Enum):
    YES = "YES"
    NO = "NO"
```

Inheriting from `str` preserves DB string compatibility (`positions.side='BUY'`, future `orders.book='YES'`) with zero data migration. In-memory comparisons use `is` / `==` against enum members; `"BUY" == Side.BUY` still holds at boundaries.

`OrderLeg.side` becomes `Side` (not `str`). Every construction site in `core/engine/arb_engine.py`, `core/strategies/batch.py`, and tests changes from `side="BUY"` to `side=Side.BUY`. Mypy now enforces the typing.

### BookResolver

`execution/clients/polymarket_book.py` (new):

```python
@dataclass(frozen=True)
class ResolvedOrder:
    token_id: str          # 77-digit CLOB id to send
    side: Side
    limit_price: float
    size: float
    book: Book
    translated: bool       # True ⇔ original SELL became BUY-NO

class BookResolver:
    def __init__(self, db: aiosqlite.Connection) -> None: ...

    async def resolve(
        self,
        market_id: str,
        side: Side,
        size: float,
        limit_price: float,
    ) -> ResolvedOrder | None:
        """
        Rules (B1):
          side=BUY                              → (yes_token, BUY, p,   YES, False)
          side=SELL, _yes_inventory() ≥ size    → (yes_token, SELL, p,  YES, False)
          side=SELL, _yes_inventory() < size    → (no_token,  BUY, 1−p, NO,  True)

        Returns None if tokens are missing, price/size is invalid, or
        the `POLYMARKET_ALLOW_SHORT_TRANSLATION` kill-switch is off.
        Caller writes a failed order with a clear error message.
        """

    async def _yes_inventory(self, market_id: str) -> float:
        cur = await self.db.execute(
            """
            SELECT COALESCE(SUM(entry_size - COALESCE(exit_size, 0)), 0)
            FROM positions
            WHERE market_id = ? AND side = 'BUY' AND status = 'open'
              AND book = 'YES'
            """,
            (market_id,),
        )
        row = await cur.fetchone()
        return float(row[0] or 0.0)
```

### Schema migration 012

`core/storage/migrations/012_order_book_columns.sql`:

- Add `book TEXT NOT NULL DEFAULT 'YES' CHECK(book IN ('YES', 'NO'))` to both `orders` and `positions`.
- Add `last_price_no REAL` (nullable) to `markets`. Populated by the ingestor from `tokens[1].price`. Read by paper execution during translated-SELL simulation. Nullable because Kalshi rows won't have it and old Polymarket rows fill in on next refresh.
- Add `CREATE INDEX IF NOT EXISTS idx_positions_market_book_status ON positions(market_id, book, status)` for the inventory query.
- Existing rows backfill `book` to `'YES'` via the `DEFAULT` — correct for everything written prior to this spec (all Polymarket positions to date are YES-book; Kalshi rows are unaffected semantically but still get the column).

### Submit path

`PolymarketExecutionClient.submit_order`:

- Replace `_resolve_token_id(leg.market_id)` with `self._book_resolver.resolve(leg.market_id, leg.side, leg.size, leg.limit_price or 0.50)`.
- On `None` → write failed order as today (and delete the old `_resolve_token_id` method).
- Pass `resolved` into `OrderArgs(token_id=resolved.token_id, side=resolved.side.value, price=resolved.limit_price, size=resolved.size)`.
- Pass `resolved` into `write_order`.

`PaperExecutionClient` (Polymarket legs only): same resolver call. Paper uses `resolved.limit_price` in its marketable-at-limit check, so slippage/rejection math matches live. For the translated NO-book path, paper must compare the translated limit against a NO-book reference price. This is a *snapshot price from the ingestor* — not a live WS feed (that would be B2).

Two code paths populate the NO-side price:

1. `core/ingestor/polymarket.py::PolymarketClient._parse_market` — extend to extract `tokens[1].price` and emit it on the returned `MarketData` (add `last_price_no: float | None` to the dataclass).
2. `core/ingestor/store.py` — extend the Polymarket row tuple (currently lines 267–336) and INSERT statement (currently line 452) to persist `last_price_no` into the new `markets.last_price_no` column.

Paper reads from `markets.last_price_no` with the same DB-backed pattern it uses for YES today. This is the one unavoidable NO-side plumbing change required by B1's paper-fidelity goal.

`BaseExecutionClient.write_order` gains an optional `resolved: ResolvedOrder | None` parameter:

- When `None` (Kalshi, paper-Kalshi), behavior is today's.
- When present, INSERT pulls `side`, `requested_price`, and new `book` column from `resolved`. `market_id`, `order_type` still from `leg`.

### Position writes

`core/engine/arb_engine.py:727`, `core/strategies/batch.py:207`, `core/strategies/single_platform.py:565` — each INSERT gets a new `book` column. For arb (always `status='closed'`), write the book of the BUY leg's resolved order (YES normally, NO when translated). Single-platform entries are YES-only today; pass `Book.YES.value` explicitly rather than relying on default.

### Kill-switch

Env var `POLYMARKET_ALLOW_SHORT_TRANSLATION` (default `true`). When `false`, resolver returns `None` on any SELL without inventory with error `"short translation disabled"`. Lets us flip to fail-fast in production without a redeploy if NO-book fills start misbehaving.

## Data flow

**Scenario A — Arb fire, Poly is the expensive leg:**

```
arb_engine emits poly_leg(side=SELL, price=0.62, size=10)
  → resolver returns ResolvedOrder(no_token_id, BUY, 0.38, 10, NO, translated=True)
  → CLOB receives OrderArgs(token_id=no_token_id, side="BUY", price=0.38, size=10)
  → orders row: side='BUY', book='NO', requested_price=0.38, signal_id=<sig>
```

Strategy intent (the `SELL` at 0.62) is preserved on `signals.*`. The orders row reflects what hit the exchange — the audit log stays truthful.

**Scenario B — Single-platform P3 exits a held YES position** (future, design-compatible):

```
strategy emits exit_leg(side=SELL, price=0.55, size=10)
  → resolver sees open BUY YES position of size 10 → ResolvedOrder(yes_token, SELL, 0.55, 10, YES, translated=False)
  → CLOB receives OrderArgs(..., side="SELL")
  → orders row: side='SELL', book='YES'
```

**Scenario C — Missing token ids:** resolver returns `None`; caller writes `orders` row with `status='failed'` and a message naming the missing column. Matches today's failure-row shape.

## Error handling

Resolver-layer rejections (pre-submit):

- `markets` row missing → `None`.
- `yes_token_id` NULL → `None` (both tokens required; NO alone is insufficient for the BUY path on that market).
- `no_token_id` NULL + SELL + insufficient inventory → `None` with WARN-level log.
- `no_token_id` NULL + BUY → YES-side `ResolvedOrder` (BUY path doesn't need NO book).
- `limit_price` is `None`, `≤ 0`, or `≥ 1` (either side) → `None` with error log. CLOB would reject these anyway; catching them here makes the failure legible in our orders table rather than surfacing as an opaque upstream 4xx.
- `size ≤ 0` (either side) → `None` with error log.
- Inventory query DB error → raises; existing `submit_order` try/except writes a failed row.

Submit-layer: unchanged from today (CLOB 4xx/5xx, signing, rate-limit). `write_order` records `resolved` values on failed rows so the audit log reflects the attempted send, not the strategy's intent.

Paper:

- With B1, translation always succeeds (BUY NO is always legal given tokens exist). Paper doesn't simulate the naked-short rejection as a separate failure mode, so no paper-vs-live divergence is introduced.
- Paper's marketable-at-limit check uses `resolved.limit_price` (the `1 − p` value when translated) against the cached NO-book price.

Invariants enforced at the schema level:

- `orders.book`, `positions.book` ∈ `{'YES','NO'}` — CHECK constraint.
- Both columns `NOT NULL` — default covers existing and new writes.

## Testing

**Unit tests — `tests/integration/test_book_resolver.py` (new):**

- `test_buy_returns_yes_untranslated`
- `test_sell_with_no_inventory_translates_to_buy_no`
- `test_sell_with_sufficient_yes_inventory_uses_yes_book`
- `test_sell_with_partial_inventory_still_translates` (all-or-nothing semantics)
- `test_inventory_only_counts_open_buy_yes` (closed, SELL, NO-book rows all ignored)
- `test_inventory_does_not_cross_markets`
- `test_missing_market_returns_none`
- `test_missing_yes_token_returns_none`
- `test_missing_no_token_with_sell_returns_none`
- `test_missing_no_token_with_buy_returns_yes`
- `test_sell_price_at_bounds` (price=0.99 → translated price=0.01, no arithmetic floor)
- `test_invalid_price_returns_none` (None / 0 / 1.5 / negative)
- `test_invalid_size_returns_none`
- `test_kill_switch_disables_translation`

**Integration tests — `tests/integration/test_polymarket_client.py` (extend):**

- `test_submit_order_translates_sell_to_buy_no_when_no_inventory` (CLOB call + orders row)
- `test_submit_order_preserves_sell_yes_when_inventory_present`
- `test_submit_order_writes_signal_lineage_on_translated_orders`
- existing `test_refuses_when_no_token_id_on_file` still passes

**Integration tests — `tests/integration/test_paper_polymarket_translation.py` (new):**

- `test_paper_simulates_translated_buy_no_fill` (uses NO-book price from `markets.last_price_no`)
- `test_paper_rejects_when_translated_price_above_no_ask`

**Ingestor tests — `tests/integration/test_polymarket_parse.py` + `test_store_and_persist.py` (extend):**

- `test_parse_extracts_no_token_price_from_tokens_array` — when `tokens[1].price = 0.465`, returned `MarketData.last_price_no == 0.465`.
- `test_store_persists_last_price_no_to_markets` — after a store round-trip, `SELECT last_price_no FROM markets WHERE id = ...` returns the expected value.

**Arb-engine regression — `tests/integration/test_arb_engine.py` (extend):**

- `test_arb_fire_with_sell_poly_leg_translates` — engine fire with kalshi < poly, mock exec client records submitted orders, assert translated poly leg shape.

**Schema tests — `tests/integration/test_schema_compliance.py` (extend):**

- Both `book` columns exist with `NOT NULL` + CHECK.
- `idx_positions_market_book_status` exists.
- INSERT with `book='MAYBE'` raises.

**Migration test:**

- Fresh DB: migration 012 runs, columns present, CHECK enforced.
- Pre-migration snapshot + migration 012 → all existing rows have `book='YES'` post-migration.

## Out of scope / follow-ups

- **`TODO[B2]` — price-aware NO-book routing.** Subscribe to NO-token WS; compare best-bid YES vs `1 − best-ask NO`; pick better fill. Requires new price cache + staleness guard mirroring the YES-side work shipped in v2.4.0.
- **Partial-inventory splitting.** Hold 7, want SELL 10 → `SELL YES 7 + BUY NO 3`. Adds a second-order dispatch, partial-fail handling. Revisit if residual inventory becomes observed.
- **Settlement / P&L branching on `book`.** Resolution code in `core/resolution/` (and wherever `resolution_outcome` is consumed) will need to compute payouts with book-awareness: `book=NO` position with `resolution_outcome='NO'` pays out, not the other way around. Separate spec.
- **Standardizing `orders.side` casing.** Historically lowercase, positions uppercase; DB was wiped in v2.4.0 so no data migration is needed, but new enum-backed writes should consistently store uppercase. Not a separate work item — falls out of the `Side` enum adoption.
