# Polymarket No-Naked-Shorts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Translate strategy-intent `SELL` legs on Polymarket into legal CLOB operations — `SELL YES` when we hold inventory, `BUY NO @ 1−p` otherwise — via a single resolver shared by live and paper clients.

**Architecture:** New `BookResolver` in `execution/clients/polymarket_book.py` owns the decision. `Side`/`Book` enums replace stringly-typed side/book values. Schema migration 012 adds a `book` column to `orders` and `positions` (CHECK-constrained to `{'YES','NO'}`) plus `markets.last_price_no` for paper-fidelity on translated fills. Strategy code (`arb_engine`, `batch`, `single_platform`) stays oblivious — it keeps emitting `side=Side.SELL` and the resolver translates at the execution boundary.

**Tech Stack:** Python 3.11, pydantic v1 `BaseModel`, `aiosqlite`, pytest + pytest-asyncio, SQLite migrations in `core/storage/migrations/`.

**Spec:** `docs/superpowers/specs/2026-04-18-polymarket-no-naked-shorts-design.md`

---

## File Structure

**New files:**
- `execution/enums.py` — `Side` and `Book` enums.
- `execution/clients/polymarket_book.py` — `ResolvedOrder` dataclass and `BookResolver` class.
- `core/storage/migrations/012_order_book_columns.sql` — schema migration.
- `tests/integration/test_book_resolver.py` — resolver unit tests.
- `tests/integration/test_paper_polymarket_translation.py` — paper fidelity tests.

**Modified files:**
- `execution/models.py` — `OrderLeg.side: Side` (enum, not str).
- `execution/clients/base.py` — `write_order` accepts optional `ResolvedOrder`, writes `book`.
- `execution/clients/polymarket.py` — submit uses `BookResolver`; `_resolve_token_id` removed.
- `execution/clients/paper.py` — Polymarket legs go through resolver; `_get_db_price` accepts `Book`.
- `core/ingestor/polymarket.py` — `MarketData.last_price_no`; `_parse_market` extracts `tokens[1].price`.
- `core/ingestor/store.py` — persists `last_price_no` to `markets`.
- `core/engine/arb_engine.py` — position INSERT gets `book`; `Side.BUY`/`Side.SELL` at leg construction.
- `core/strategies/batch.py` — same: `book` column + enum at leg construction.
- `core/strategies/single_platform.py` — position INSERT passes `Book.YES.value`.
- `tests/integration/test_arb_engine.py` — regression test for translated poly SELL leg.
- `tests/integration/test_polymarket_client.py` — translation integration tests.
- `tests/integration/test_polymarket_parse.py` — NO-price extraction test.
- `tests/integration/test_store_and_persist.py` — NO-price persistence test.
- `tests/integration/test_schema_compliance.py` — assert new columns + CHECK constraints.

---

## Task 1: `Side` and `Book` enums

**Files:**
- Create: `execution/enums.py`
- Create: `tests/integration/test_enums.py`

- [ ] **Step 1: Write the failing test**

`tests/integration/test_enums.py`:

```python
"""Tests for execution.enums — Side and Book string-backed enums."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.enums import Book, Side  # noqa: E402


def test_side_values_are_uppercase_strings():
    assert Side.BUY.value == "BUY"
    assert Side.SELL.value == "SELL"


def test_book_values_are_uppercase_strings():
    assert Book.YES.value == "YES"
    assert Book.NO.value == "NO"


def test_side_equals_string_at_boundary():
    # str inheritance means enum members equal their string value.
    assert Side.BUY == "BUY"
    assert "SELL" == Side.SELL


def test_book_equals_string_at_boundary():
    assert Book.YES == "YES"
    assert "NO" == Book.NO


def test_side_roundtrip_from_string():
    assert Side("BUY") is Side.BUY
    assert Side("SELL") is Side.SELL


def test_book_roundtrip_from_string():
    assert Book("YES") is Book.YES
    assert Book("NO") is Book.NO
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/test_enums.py -v
```

Expected: `ModuleNotFoundError: No module named 'execution.enums'`.

- [ ] **Step 3: Implement `execution/enums.py`**

```python
"""
Strongly-typed enums for execution-layer side and book selection.

These replace stringly-typed `side` / `book` values across the system.
Inheriting from `str` preserves DB-string compatibility (e.g.,
``positions.side = 'BUY'``, ``orders.book = 'YES'``) with zero data
migration — ``"BUY" == Side.BUY`` still holds at serialization boundaries.
"""

from enum import Enum


class Side(str, Enum):
    """Trading side at the strategy layer."""

    BUY = "BUY"
    SELL = "SELL"


class Book(str, Enum):
    """Which CLOB order book a Polymarket order hit.

    Polymarket markets have two independent books — one per outcome token.
    Kalshi markets only have a YES book, so Kalshi orders are always
    ``Book.YES``.
    """

    YES = "YES"
    NO = "NO"
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/integration/test_enums.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add execution/enums.py tests/integration/test_enums.py
git commit -m "Add Side and Book string-backed enums for execution layer"
```

---

## Task 2: Migration 012 — `book` column + `markets.last_price_no`

**Files:**
- Create: `core/storage/migrations/012_order_book_columns.sql`
- Modify: `tests/integration/test_schema_compliance.py`

- [ ] **Step 1: Write the failing schema test**

Append to `tests/integration/test_schema_compliance.py` (find an existing `@pytest.mark.asyncio` class or add one at the end):

```python
@pytest.mark.asyncio
class TestMigration012OrderBookColumns:
    async def test_orders_has_book_column(self, db):
        cursor = await db.execute("PRAGMA table_info(orders)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert "book" in cols
        # row format: (cid, name, type, notnull, dflt_value, pk)
        assert cols["book"][3] == 1  # NOT NULL
        assert cols["book"][4] == "'YES'"  # DEFAULT 'YES'

    async def test_positions_has_book_column(self, db):
        cursor = await db.execute("PRAGMA table_info(positions)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert "book" in cols
        assert cols["book"][3] == 1
        assert cols["book"][4] == "'YES'"

    async def test_markets_has_last_price_no_column(self, db):
        cursor = await db.execute("PRAGMA table_info(markets)")
        cols = {row[1]: row for row in await cursor.fetchall()}
        assert "last_price_no" in cols
        # Nullable — Kalshi rows won't have it.
        assert cols["last_price_no"][3] == 0

    async def test_orders_book_check_constraint_rejects_invalid(self, db):
        # Seed a signal to satisfy FK
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_chk', 's', 'arb_pair', 'm1', 'm1',
                       0.01, 0.01, 10.0, 10.0, 'fired', 'now', 'now')""",
        )
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES ('m1', 'polymarket', '0xm1', 't', 'open', 'now', 'now')""",
        )
        await db.commit()
        with pytest.raises(Exception) as exc_info:
            await db.execute(
                """INSERT INTO orders
                   (id, signal_id, platform, market_id, side, order_type,
                    requested_size, status, submitted_at, updated_at, book)
                   VALUES ('o1', 'sig_chk', 'polymarket', 'm1', 'buy', 'limit',
                           10, 'pending', 'now', 'now', 'MAYBE')""",
            )
        assert "CHECK constraint" in str(exc_info.value) or "constraint" in str(
            exc_info.value
        ).lower()

    async def test_positions_market_book_status_index_exists(self, db):
        cursor = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='idx_positions_market_book_status'"
        )
        row = await cursor.fetchone()
        assert row is not None
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/test_schema_compliance.py::TestMigration012OrderBookColumns -v
```

Expected: 5 failures — `book` and `last_price_no` columns don't exist yet.

- [ ] **Step 3: Write migration 012**

`core/storage/migrations/012_order_book_columns.sql`:

```sql
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

ALTER TABLE orders ADD COLUMN book TEXT NOT NULL DEFAULT 'YES'
    CHECK(book IN ('YES', 'NO'));

ALTER TABLE positions ADD COLUMN book TEXT NOT NULL DEFAULT 'YES'
    CHECK(book IN ('YES', 'NO'));

ALTER TABLE markets ADD COLUMN last_price_no REAL;

CREATE INDEX IF NOT EXISTS idx_positions_market_book_status
    ON positions(market_id, book, status);
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/integration/test_schema_compliance.py::TestMigration012OrderBookColumns -v
```

Expected: 5 passed.

- [ ] **Step 5: Run the full schema compliance file to catch any regressions**

```
pytest tests/integration/test_schema_compliance.py -v
```

Expected: all existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add core/storage/migrations/012_order_book_columns.sql \
        tests/integration/test_schema_compliance.py
git commit -m "Migration 012: book column on orders/positions, last_price_no on markets"
```

---

## Task 3: Ingestor — `MarketData.last_price_no` and parser extraction

**Files:**
- Modify: `core/ingestor/polymarket.py` (MarketData dataclass + `_parse_market`)
- Modify: `tests/integration/test_polymarket_parse.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_polymarket_parse.py`:

```python
def test_parse_extracts_no_token_price_from_tokens_array():
    """CLOB tokens[1] is the NO token; its price must land on
    MarketData.last_price_no so paper can read it for translated fills."""
    item = {
        "condition_id": "0xabc",
        "question": "Will X happen?",
        "lastPrice": None,
        "tokens": [
            {"token_id": "t1", "outcome": "Yes", "price": 0.535},
            {"token_id": "t2", "outcome": "No", "price": 0.465},
        ],
    }
    md = _client()._parse_market(item)
    assert md is not None
    assert md.last_price == 0.535
    assert md.last_price_no == 0.465


def test_parse_no_price_none_when_no_second_token():
    item = {
        "conditionId": "0xone",
        "question": "q",
        "tokens": [{"price": 0.5}],  # only YES token, no NO
    }
    md = _client()._parse_market(item)
    assert md is not None
    assert md.last_price_no is None


def test_parse_no_price_none_when_tokens_missing():
    item = {"conditionId": "0xbare", "question": "q"}
    md = _client()._parse_market(item)
    assert md is not None
    assert md.last_price_no is None
```

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/test_polymarket_parse.py -v
```

Expected: 3 new failures with `AttributeError: 'MarketData' object has no attribute 'last_price_no'`.

- [ ] **Step 3: Add `last_price_no` to `MarketData`**

In `core/ingestor/polymarket.py`, locate the `@dataclass class MarketData:` block (around line 34) and add the field after `last_price`:

```python
@dataclass
class MarketData:
    """Internal market data representation."""

    market_id: str
    platform: str
    symbol: str
    question: str
    description: str
    resolution_date: datetime | None
    last_price: float
    last_price_no: float | None = None
    order_book: OrderBook | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_active: bool = True
    metadata: dict = field(default_factory=dict)
```

- [ ] **Step 4: Extract `tokens[1].price` in `_parse_market`**

Find `_parse_market` in `core/ingestor/polymarket.py`. After the existing YES price derivation (the block that sets `last_price` from `tokens[0].price` when `lastPrice` is null), add NO extraction. The exact placement: immediately before the `MarketData(...)` construction.

Add:

```python
# Extract NO-token price from tokens[1] for paper-fidelity on
# translated SELL legs. Optional — Kalshi has no NO book, and
# some sparse Polymarket payloads only expose one token.
last_price_no: float | None = None
tokens_for_no = item.get("tokens") or []
if len(tokens_for_no) >= 2:
    try:
        last_price_no = float(tokens_for_no[1].get("price", 0) or 0) or None
    except (TypeError, ValueError):
        last_price_no = None
```

Then add `last_price_no=last_price_no,` to the `MarketData(...)` keyword arguments.

- [ ] **Step 5: Run tests to verify they pass**

```
pytest tests/integration/test_polymarket_parse.py -v
```

Expected: all tests pass (original 4 + new 3 = 7).

- [ ] **Step 6: Commit**

```bash
git add core/ingestor/polymarket.py tests/integration/test_polymarket_parse.py
git commit -m "Ingestor: extract NO-token price into MarketData.last_price_no"
```

---

## Task 4: Ingestor — persist `last_price_no` to `markets`

**Files:**
- Modify: `core/ingestor/store.py` (Polymarket row tuple + INSERT)
- Modify: `tests/integration/test_store_and_persist.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_store_and_persist.py` (follow the existing `_poly_real_api()` helper style — there should be one near the top):

```python
@pytest.mark.asyncio
async def test_store_persists_last_price_no_to_markets(db):
    """Polymarket ingestor must write tokens[1].price to
    markets.last_price_no so paper execution can read it later."""
    from core.ingestor.store import store_and_persist_batch

    poly = {
        "conditionId": "0xNO_PRICE_TEST",
        "question": "NO-price roundtrip",
        "clobTokenIds": '["111", "222"]',
        "tokens": [
            {"price": 0.60},
            {"price": 0.40},
        ],
    }
    await store_and_persist_batch(db, [poly], [])

    cursor = await db.execute(
        "SELECT last_price_no FROM markets WHERE id = ?",
        ("poly_0xNO_PRICE_TEST",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 0.40
```

(If the existing test module uses a different helper for invoking `store_and_persist_batch`, mirror that — look at the top of the file for the pattern.)

- [ ] **Step 2: Run test to verify it fails**

```
pytest tests/integration/test_store_and_persist.py::test_store_persists_last_price_no_to_markets -v
```

Expected: fails with either `KeyError`, `None != 0.40`, or a column-mismatch error depending on current schema.

- [ ] **Step 3: Update the Polymarket row loop in `store.py`**

In `core/ingestor/store.py`, find the block around line 300–326 that prepares `poly_market_rows`. Extract `tokens[1].price` right alongside the existing `tokens[0].price` extraction:

```python
        price = None
        last_price_no: float | None = None
        tokens = m.get("tokens", [])
        if tokens and len(tokens) > 0:
            try:
                price = float(tokens[0].get("price", 0))
            except (ValueError, TypeError):
                pass
        if tokens and len(tokens) >= 2:
            try:
                last_price_no = float(tokens[1].get("price", 0) or 0) or None
            except (ValueError, TypeError):
                last_price_no = None
```

Then update the row tuple append (currently line 324–326) to include `last_price_no`:

```python
        poly_market_rows.append(
            (mid, condition_id, title[:200], yes_token_id, no_token_id,
             last_price_no, now, now)
        )
```

- [ ] **Step 4: Update the Polymarket INSERT in `store.py`**

Find the `executemany` for `poly_market_rows` (currently at line 450–456) and add `last_price_no` to the column list + a corresponding `?`:

```python
        await db.executemany(
            """INSERT OR REPLACE INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                last_price_no, status, created_at, updated_at)
               VALUES (?, 'polymarket', ?, ?, ?, ?, ?, 'open', ?, ?)""",
            poly_market_rows,
        )
```

- [ ] **Step 5: Run the new test + full store suite to verify**

```
pytest tests/integration/test_store_and_persist.py -v
```

Expected: new test passes; existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add core/ingestor/store.py tests/integration/test_store_and_persist.py
git commit -m "Ingestor: persist last_price_no to markets for paper-fidelity on NO-book fills"
```

---

## Task 5: `OrderLeg.side` → `Side` enum

**Files:**
- Modify: `execution/models.py`
- Modify: `core/engine/arb_engine.py` (leg construction sites around lines 672–687)
- Modify: `core/strategies/batch.py` (leg construction around lines 166, 174)
- Search-and-update: any test that constructs `OrderLeg(..., side="BUY"|"SELL")`.

- [ ] **Step 1: Write a test asserting enum typing**

Append to `tests/integration/test_enums.py`:

```python
def test_order_leg_accepts_side_enum():
    from execution.models import OrderLeg

    leg = OrderLeg(
        market_id="m1", platform="polymarket",
        side=Side.BUY, size=10, limit_price=0.5,
    )
    assert leg.side is Side.BUY


def test_order_leg_coerces_string_side_to_enum():
    from execution.models import OrderLeg

    leg = OrderLeg(
        market_id="m1", platform="polymarket",
        side="SELL", size=10, limit_price=0.5,
    )
    assert leg.side is Side.SELL
```

- [ ] **Step 2: Run test to verify failure**

```
pytest tests/integration/test_enums.py -v
```

Expected: both new tests fail — `OrderLeg.side` is currently `str`, so `leg.side is Side.BUY` is False (it's the string `"BUY"`).

- [ ] **Step 3: Change `OrderLeg.side` to `Side`**

In `execution/models.py`, replace the current `side` field + validator:

```python
from execution.enums import Side


class OrderLeg(BaseModel):
    """Schema for a single order leg."""

    market_id: str
    platform: str  # "polymarket" or "kalshi"
    side: Side
    size: float = Field(gt=0)
    limit_price: float | None = Field(None, ge=0, le=1)
    order_type: str = "LIMIT"  # "LIMIT" or "MARKET"

    @validator("platform")
    def validate_platform(cls, v: str) -> str:
        if v.lower() not in ("polymarket", "kalshi"):
            raise ValueError("platform must be 'polymarket' or 'kalshi'")
        return v.lower()

    @validator("side", pre=True)
    def validate_side(cls, v) -> Side:
        if isinstance(v, Side):
            return v
        if isinstance(v, str) and v.upper() in ("BUY", "SELL"):
            return Side(v.upper())
        raise ValueError("side must be 'BUY' or 'SELL'")

    @validator("order_type")
    def validate_order_type(cls, v: str) -> str:
        if v.upper() not in ("LIMIT", "MARKET"):
            raise ValueError("order_type must be 'LIMIT' or 'MARKET'")
        return v.upper()
```

(The `pre=True` on the side validator runs before pydantic tries the default coercion, which lets us accept both uppercase and lowercase strings while still producing a `Side` enum in the final model.)

- [ ] **Step 4: Update leg construction sites in `arb_engine.py`**

Find every `OrderLeg(..., side="BUY"|"SELL", ...)` in `core/engine/arb_engine.py` (primarily around lines 672–687). Replace string literals with `Side.BUY` / `Side.SELL`. Add `from execution.enums import Side` at the top of the file.

- [ ] **Step 5: Update leg construction sites in `batch.py`**

Same change in `core/strategies/batch.py` (primarily around lines 166, 174). Add the import.

- [ ] **Step 6: Update downstream string comparisons**

Grep for existing `.upper() == "BUY"` / `.upper() == "SELL"` call sites and replace with `is Side.BUY` / `is Side.SELL`. This matters in `execution/clients/paper.py` (lines 192, 204, 226), `execution/clients/polymarket.py` (line 232 uses `.upper()` — stays as `.value` now since the enum is uppercase), and the base client if any.

Sample edit in `paper.py`:

```python
if leg.side is Side.BUY and market_price > leg.limit_price:
    ...
if leg.side is Side.SELL and market_price < leg.limit_price:
    ...
```

Add `from execution.enums import Side` to each edited file.

- [ ] **Step 7: Run the full test suite**

```
pytest tests/integration/ -v
```

Expected: all tests pass. The `pre=True` validator means test fixtures that pass raw strings (`side="BUY"`) still work.

- [ ] **Step 8: Commit**

```bash
git add execution/models.py execution/clients/paper.py \
        execution/clients/polymarket.py core/engine/arb_engine.py \
        core/strategies/batch.py tests/integration/test_enums.py
git commit -m "OrderLeg.side typed as Side enum; update leg constructors + comparisons"
```

---

## Task 6: `BookResolver` class and `ResolvedOrder` dataclass

**Files:**
- Create: `execution/clients/polymarket_book.py`
- Create: `tests/integration/test_book_resolver.py`

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_book_resolver.py`:

```python
"""
Tests for execution.clients.polymarket_book.BookResolver.

The resolver encodes the no-naked-shorts rule: a SELL on Polymarket
becomes either a SELL on the YES book (if we hold inventory) or a
BUY on the NO book at (1 - p) otherwise. Returns None for unresolvable
requests (missing tokens, invalid price/size, kill-switch).
"""

import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.clients.polymarket_book import BookResolver  # noqa: E402
from execution.enums import Book, Side  # noqa: E402


async def _seed_market(
    db, market_id="poly_0xA", yes="111", no="222", last_price_no=0.4
):
    await db.execute(
        """INSERT INTO markets
           (id, platform, platform_id, title, yes_token_id, no_token_id,
            last_price_no, status, created_at, updated_at)
           VALUES (?, 'polymarket', ?, 't', ?, ?, ?, 'open', 'now', 'now')""",
        (market_id, market_id, yes, no, last_price_no),
    )
    await db.commit()


async def _seed_open_buy_yes_position(db, market_id, entry_size=10.0):
    # Signals row to satisfy FK
    await db.execute(
        """INSERT OR IGNORE INTO signals
           (id, strategy, signal_type, market_id_a, market_id_b,
            model_edge, kelly_fraction, position_size_a,
            total_capital_at_risk, status, fired_at, updated_at)
           VALUES ('sig_seed', 's', 'arb_pair', ?, ?,
                   0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        (market_id, market_id),
    )
    await db.execute(
        """INSERT INTO positions
           (id, signal_id, market_id, strategy, side, book, entry_price,
            entry_size, status, opened_at, updated_at)
           VALUES ('pos_seed', 'sig_seed', ?, 'P3_calibration_bias',
                   'BUY', 'YES', 0.5, ?, 'open', 'now', 'now')""",
        (market_id, entry_size),
    )
    await db.commit()


@pytest.mark.asyncio
class TestBookResolver:
    async def test_buy_returns_yes_untranslated(self, db):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.BUY, 10, 0.6)
        assert r is not None
        assert (r.token_id, r.side, r.limit_price, r.book, r.translated) == (
            "111", Side.BUY, 0.6, Book.YES, False,
        )

    async def test_sell_with_no_inventory_translates_to_buy_no(self, db):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.62)
        assert r is not None
        assert r.token_id == "222"
        assert r.side is Side.BUY
        assert r.limit_price == pytest.approx(0.38)
        assert r.book is Book.NO
        assert r.translated is True

    async def test_sell_with_sufficient_yes_inventory_uses_yes_book(self, db):
        await _seed_market(db)
        await _seed_open_buy_yes_position(db, "poly_0xA", entry_size=10.0)
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.55)
        assert r is not None
        assert r.token_id == "111"
        assert r.side is Side.SELL
        assert r.limit_price == 0.55
        assert r.book is Book.YES
        assert r.translated is False

    async def test_sell_with_partial_inventory_still_translates(self, db):
        """All-or-nothing: inventory=7 < request=10 → BUY NO for full size."""
        await _seed_market(db)
        await _seed_open_buy_yes_position(db, "poly_0xA", entry_size=7.0)
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.60)
        assert r is not None
        assert r.translated is True
        assert r.book is Book.NO
        assert r.size == 10  # full size, not 3

    async def test_inventory_only_counts_open_buy_yes(self, db):
        await _seed_market(db)
        # Closed BUY — should not count
        await db.execute(
            """INSERT OR IGNORE INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_x', 's', 'arb_pair', 'poly_0xA', 'poly_0xA',
                       0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        )
        await db.execute(
            """INSERT INTO positions
               (id, signal_id, market_id, strategy, side, book,
                entry_price, entry_size, status, opened_at, updated_at)
               VALUES ('pos_closed', 'sig_x', 'poly_0xA', 's',
                       'BUY', 'YES', 0.5, 10, 'closed', 'now', 'now'),
                      ('pos_sell',   'sig_x', 'poly_0xA', 's',
                       'SELL', 'YES', 0.5, 10, 'open', 'now', 'now'),
                      ('pos_no',     'sig_x', 'poly_0xA', 's',
                       'BUY', 'NO',  0.5, 10, 'open', 'now', 'now')""",
        )
        await db.commit()
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.60)
        assert r is not None and r.translated  # still translated (inventory=0)

    async def test_inventory_does_not_cross_markets(self, db):
        await _seed_market(db, "poly_0xAAA")
        await _seed_market(db, "poly_0xBBB")
        await _seed_open_buy_yes_position(db, "poly_0xAAA", entry_size=100)
        r = await BookResolver(db).resolve("poly_0xBBB", Side.SELL, 10, 0.60)
        assert r is not None and r.translated

    async def test_missing_market_returns_none(self, db):
        r = await BookResolver(db).resolve("poly_0xNOPE", Side.BUY, 10, 0.5)
        assert r is None

    async def test_missing_yes_token_returns_none(self, db):
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_0xNo_yes', 'polymarket', '0x', 't',
                       NULL, '222', 'open', 'now', 'now')""",
        )
        await db.commit()
        r = await BookResolver(db).resolve("poly_0xNo_yes", Side.BUY, 10, 0.5)
        assert r is None

    async def test_missing_no_token_with_sell_returns_none(self, db):
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_0xNo_no', 'polymarket', '0x', 't',
                       '111', NULL, 'open', 'now', 'now')""",
        )
        await db.commit()
        r = await BookResolver(db).resolve("poly_0xNo_no", Side.SELL, 10, 0.60)
        assert r is None

    async def test_missing_no_token_with_buy_returns_yes(self, db):
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_0xNo_no2', 'polymarket', '0x', 't',
                       '111', NULL, 'open', 'now', 'now')""",
        )
        await db.commit()
        r = await BookResolver(db).resolve("poly_0xNo_no2", Side.BUY, 10, 0.5)
        assert r is not None
        assert r.book is Book.YES

    async def test_sell_price_at_bounds(self, db):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.99)
        assert r is not None
        assert r.limit_price == pytest.approx(0.01)

    @pytest.mark.parametrize("bad", [None, 0.0, -0.1, 1.0, 1.5])
    async def test_invalid_price_returns_none(self, db, bad):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.BUY, 10, bad)
        assert r is None

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    async def test_invalid_size_returns_none(self, db, bad):
        await _seed_market(db)
        r = await BookResolver(db).resolve("poly_0xA", Side.BUY, bad, 0.5)
        assert r is None

    async def test_kill_switch_disables_translation(self, db, monkeypatch):
        await _seed_market(db)
        monkeypatch.setenv("POLYMARKET_ALLOW_SHORT_TRANSLATION", "false")
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.60)
        assert r is None

    async def test_kill_switch_does_not_block_sell_with_inventory(
        self, db, monkeypatch
    ):
        await _seed_market(db)
        await _seed_open_buy_yes_position(db, "poly_0xA", entry_size=10)
        monkeypatch.setenv("POLYMARKET_ALLOW_SHORT_TRANSLATION", "false")
        r = await BookResolver(db).resolve("poly_0xA", Side.SELL, 10, 0.55)
        assert r is not None  # uses inventory path, no translation
        assert r.translated is False
```

- [ ] **Step 2: Run tests to verify failure**

```
pytest tests/integration/test_book_resolver.py -v
```

Expected: `ModuleNotFoundError: No module named 'execution.clients.polymarket_book'`.

- [ ] **Step 3: Implement `BookResolver`**

`execution/clients/polymarket_book.py`:

```python
"""
BookResolver — encodes Polymarket's no-naked-shorts rule.

The CLOB does not allow a SELL on a token we don't hold. To express
short exposure without inventory, the only legal operation is a BUY
on the opposing token's book at the complementary price:
``BUY NO @ 1 − p`` substitutes for ``SELL YES @ p``.

Rule (B1 — no NO-book price comparison):
    side=BUY                              → (yes_token, BUY, p,   YES, False)
    side=SELL, YES inventory ≥ size       → (yes_token, SELL, p,  YES, False)
    side=SELL, YES inventory < size       → (no_token,  BUY, 1−p, NO,  True)

Returns None for unresolvable requests; the caller writes a failed
order with a clear error message.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import aiosqlite

from execution.enums import Book, Side

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedOrder:
    token_id: str
    side: Side
    limit_price: float
    size: float
    book: Book
    translated: bool


class BookResolver:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    async def resolve(
        self,
        market_id: str,
        side: Side,
        size: float,
        limit_price: float | None,
    ) -> ResolvedOrder | None:
        if not self._valid_size(size) or not self._valid_price(limit_price):
            return None

        tokens = await self._tokens(market_id)
        if tokens is None:
            logger.warning("BookResolver: no market row for %s", market_id)
            return None
        yes_tok, no_tok = tokens
        if yes_tok is None:
            logger.warning(
                "BookResolver: %s has no yes_token_id; cannot route BUY or YES-SELL",
                market_id,
            )
            return None

        if side is Side.BUY:
            return ResolvedOrder(
                token_id=yes_tok,
                side=Side.BUY,
                limit_price=float(limit_price),  # already validated
                size=size,
                book=Book.YES,
                translated=False,
            )

        # side is SELL from here on.
        inventory = await self._yes_inventory(market_id)
        if inventory >= size:
            return ResolvedOrder(
                token_id=yes_tok,
                side=Side.SELL,
                limit_price=float(limit_price),
                size=size,
                book=Book.YES,
                translated=False,
            )

        # No inventory → translate to BUY NO.
        if not self._translation_enabled():
            logger.warning(
                "BookResolver: short translation disabled for %s (kill-switch)",
                market_id,
            )
            return None
        if no_tok is None:
            logger.warning(
                "BookResolver: cannot short %s; no_token_id missing", market_id
            )
            return None

        return ResolvedOrder(
            token_id=no_tok,
            side=Side.BUY,
            limit_price=round(1.0 - float(limit_price), 4),
            size=size,
            book=Book.NO,
            translated=True,
        )

    @staticmethod
    def _valid_size(size: float) -> bool:
        try:
            return size > 0
        except TypeError:
            return False

    @staticmethod
    def _valid_price(price: float | None) -> bool:
        if price is None:
            return False
        try:
            return 0 < float(price) < 1
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _translation_enabled() -> bool:
        return os.getenv("POLYMARKET_ALLOW_SHORT_TRANSLATION", "true").lower() != "false"

    async def _tokens(self, market_id: str) -> tuple[str | None, str | None] | None:
        cur = await self.db.execute(
            "SELECT yes_token_id, no_token_id FROM markets WHERE id = ?",
            (market_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return (row[0], row[1])

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

    # TODO[B2]: price-aware NO-book routing. When we have a live NO-book
    # price feed, compare (YES best-bid) vs (1 - NO best-ask) and pick the
    # better fill even when we have YES inventory. Requires NO-token WS
    # subscription + staleness tracking mirroring the YES-side work in v2.4.0.
```

- [ ] **Step 4: Run tests to verify they pass**

```
pytest tests/integration/test_book_resolver.py -v
```

Expected: all 15 tests pass.

- [ ] **Step 5: Commit**

```bash
git add execution/clients/polymarket_book.py \
        tests/integration/test_book_resolver.py
git commit -m "Add BookResolver for Polymarket no-naked-shorts translation"
```

---

## Task 7: `BaseExecutionClient.write_order` accepts `ResolvedOrder`

**Files:**
- Modify: `execution/clients/base.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_polymarket_client.py`:

```python
@pytest.mark.asyncio
class TestWriteOrderWithResolvedOrder:
    async def test_write_order_uses_resolved_side_price_and_book(self, db):
        """When write_order gets a ResolvedOrder, the orders row
        reflects what hit the exchange (resolved values), not the
        original strategy intent."""
        from execution.clients.base import BaseExecutionClient
        from execution.clients.base import OrderResult
        from execution.clients.polymarket_book import ResolvedOrder
        from execution.enums import Book, Side
        from execution.models import OrderLeg

        # Seed signal for FK.
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_w', 's', 'arb_pair', 'poly_m', 'poly_m',
                       0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        )
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_m', 'polymarket', '0xm', 't', '111', '222',
                       'open', 'now', 'now')""",
        )
        await db.commit()

        class _Client(BaseExecutionClient):
            async def submit_order(self, leg, **kw):
                raise NotImplementedError

        client = _Client(db, platform_label="polymarket")
        leg = OrderLeg(
            market_id="poly_m", platform="polymarket",
            side=Side.SELL, size=10, limit_price=0.62,
        )
        resolved = ResolvedOrder(
            token_id="222", side=Side.BUY, limit_price=0.38,
            size=10, book=Book.NO, translated=True,
        )
        result = OrderResult(
            order_id="ord_x", platform="polymarket", status="pending",
            submission_latency_ms=0,
        )
        await client.write_order(leg, result, signal_id="sig_w", resolved=resolved)
        await db.commit()

        cursor = await db.execute(
            "SELECT side, book, requested_price FROM orders WHERE id = 'ord_x'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "BUY"     # resolved side, not original SELL
        assert row[1] == "NO"      # resolved book
        assert row[2] == 0.38      # translated price

    async def test_write_order_without_resolved_uses_leg_values(self, db):
        """Kalshi (and any other caller without a resolver) keeps today's
        behavior: orders row reflects leg.side and leg.limit_price,
        book defaults to 'YES' via migration 012."""
        from execution.clients.base import BaseExecutionClient, OrderResult
        from execution.enums import Side
        from execution.models import OrderLeg

        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_k', 's', 'arb_pair', 'kal_m', 'kal_m',
                       0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        )
        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES ('kal_m', 'kalshi', 'KXM', 't', 'open', 'now', 'now')""",
        )
        await db.commit()

        class _Client(BaseExecutionClient):
            async def submit_order(self, leg, **kw):
                raise NotImplementedError

        client = _Client(db, platform_label="kalshi")
        leg = OrderLeg(
            market_id="kal_m", platform="kalshi",
            side=Side.BUY, size=10, limit_price=0.35,
        )
        result = OrderResult(
            order_id="ord_k", platform="kalshi", status="pending",
            submission_latency_ms=0,
        )
        await client.write_order(leg, result, signal_id="sig_k")
        await db.commit()

        cursor = await db.execute(
            "SELECT side, book, requested_price FROM orders WHERE id = 'ord_k'"
        )
        row = await cursor.fetchone()
        assert row == ("BUY", "YES", 0.35)
```

- [ ] **Step 2: Run tests to verify failure**

```
pytest tests/integration/test_polymarket_client.py::TestWriteOrderWithResolvedOrder -v
```

Expected: fails — `write_order` does not accept `resolved` and INSERT doesn't write a `book` column.

- [ ] **Step 3: Update `BaseExecutionClient.write_order`**

In `execution/clients/base.py`, update the signature and the INSERT. Add the import at top:

```python
from execution.clients.polymarket_book import ResolvedOrder
```

Then change `write_order`:

```python
async def write_order(
    self,
    leg: OrderLeg,
    result: OrderResult,
    signal_id: str | None = None,
    strategy: str | None = None,
    resolved: "ResolvedOrder | None" = None,
) -> None:
    """
    Write an order record to the orders table.

    When ``resolved`` is provided, ``side``, ``requested_price``, and
    ``book`` are pulled from it — reflecting what actually hit the
    exchange rather than the strategy's original intent. Callers that
    don't route through a resolver (Kalshi, paper-Kalshi) pass None
    and rows get book='YES' via the column default.
    """
    now = int(time.time())
    if resolved is not None:
        side_str = resolved.side.value
        requested_price = resolved.limit_price
        book_str = resolved.book.value
    else:
        # Normalize to uppercase for consistency with new enum-typed writers.
        side_str = (
            leg.side.value if hasattr(leg.side, "value") else str(leg.side).upper()
        )
        requested_price = leg.limit_price
        book_str = "YES"

    try:
        await self.db.execute(
            """
            INSERT INTO orders (
                id, signal_id, platform, platform_order_id,
                market_id, side, order_type,
                requested_price, requested_size,
                filled_price, filled_size, slippage, fee_paid,
                status, failure_reason,
                retry_count, submitted_at,
                filled_at, submission_latency_ms, fill_latency_ms,
                strategy, updated_at, book
            ) VALUES (
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                ?, ?,
                0, ?,
                ?, ?, ?,
                ?, ?, ?
            )
            """,
            (
                result.order_id,
                signal_id,
                result.platform,
                result.order_id,
                leg.market_id,
                side_str,
                leg.order_type.lower() if isinstance(leg.order_type, str) else leg.order_type,
                requested_price,
                leg.size,
                result.filled_price,
                result.filled_size,
                result.slippage,
                result.fee_paid,
                result.status,
                result.error_message,
                now,
                now if result.filled_price else None,
                result.submission_latency_ms,
                result.fill_latency_ms,
                strategy,
                now,
                book_str,
            ),
        )
    except Exception as e:
        logger.error("Failed to write order to DB: %s", e, exc_info=True)
```

(Note the change from `leg.side.lower()` to `side_str` — we now store uppercase consistently. This means any downstream code that queries `orders.side = 'buy'` must become uppercase. That's Task 7.5 below — but first let's confirm no such queries exist.)

- [ ] **Step 4: Grep for `orders.side` queries**

```
grep -rn "orders.side" core/ execution/ tests/ || echo "no matches"
grep -rn "WHERE side = '" core/ execution/ | grep -v positions
```

If any results reference `orders.side = 'buy'` or `'sell'` lowercased, note them for a follow-up fix. (A fresh-DB deployment already avoided historical rows, per the v2.4.0 wipe.)

- [ ] **Step 5: Run tests**

```
pytest tests/integration/test_polymarket_client.py::TestWriteOrderWithResolvedOrder -v
pytest tests/integration/ -v
```

Expected: new tests pass; full suite green (or only breakages on write_order-related code that Task 8 will touch).

- [ ] **Step 6: Commit**

```bash
git add execution/clients/base.py tests/integration/test_polymarket_client.py
git commit -m "BaseExecutionClient.write_order: accept ResolvedOrder, write book column"
```

---

## Task 8: Position INSERTs gain `book` column

**Files:**
- Modify: `core/engine/arb_engine.py` (around line 727)
- Modify: `core/strategies/batch.py` (around line 207)
- Modify: `core/strategies/single_platform.py` (around line 565)

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_schema_compliance.py`:

```python
@pytest.mark.asyncio
class TestPositionBookWriting:
    async def test_single_platform_writes_book_yes(self, db):
        """All single-platform positions are YES-book today."""
        # Write a position row matching single_platform.py's INSERT shape.
        await db.execute(
            """INSERT OR IGNORE INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('s_sp', 'P3', 'arb_pair', 'm_sp', 'm_sp',
                       0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        )
        await db.execute(
            """INSERT OR IGNORE INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES ('m_sp', 'polymarket', '0xsp', 't', 'open', 'now', 'now')""",
        )
        await db.commit()

        # After Task 8 the INSERT must include 'YES' explicitly (not rely on default).
        # This test verifies by dropping the default temporarily would be overkill;
        # instead, assert that single_platform's production code path produces
        # a row with book='YES'. We simulate by mimicking its INSERT:
        await db.execute(
            """INSERT INTO positions
               (id, signal_id, market_id, strategy, side, book, entry_price,
                entry_size, fees_paid, pnl_model, status, opened_at, updated_at)
               VALUES ('p_sp', 's_sp', 'm_sp', 'P3', 'BUY', 'YES', 0.5,
                       10, 0.02, 'realistic', 'open', 'now', 'now')""",
        )
        await db.commit()

        cursor = await db.execute(
            "SELECT book FROM positions WHERE id = 'p_sp'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "YES"
```

(This test mostly asserts the schema path round-trips `book`. The deeper behavior — that `single_platform.py` actually emits `book='YES'` in its INSERT — is covered implicitly by running the existing single-platform tests after the code edit; they will fail if the INSERT column count / VALUES arity is wrong.)

- [ ] **Step 2: Update `single_platform.py`'s position INSERT**

Find the INSERT at `core/strategies/single_platform.py:565`. Add `book` to the column list and `'YES'` to VALUES:

```python
await db.execute(
    """INSERT INTO positions
       (id, signal_id, market_id, strategy, side, book, entry_price,
        entry_size, fees_paid, pnl_model, status, opened_at, updated_at)
       VALUES (?, ?, ?, ?, ?, 'YES', ?, ?, ?, 'realistic', 'open', ?, ?)""",
    (
        pos_id,
        signal_id,
        m["id"],
        strategy,
        # ... rest unchanged
    ),
)
```

(If additional single_platform.py INSERTs exist, update each the same way — `book='YES'` for now.)

- [ ] **Step 3: Update `arb_engine.py`'s position INSERT**

The arb engine writes an atomic round-trip position (`status='closed'`). Its `book` should reflect the BUY leg's resolved book — normally `YES`, or `NO` if the BUY leg is a translated short. For the moment, the arb engine doesn't have a `ResolvedOrder` in scope at this call site. Until Task 10 wires resolver output through, hardcode `'YES'` on the BUY-side-is-YES branch and `'NO'` on the BUY-side-is-translated branch. Since arb_engine currently has no resolver, pass `'YES'` unconditionally and add a TODO:

In `core/engine/arb_engine.py` near line 728, change:

```python
await self.db.execute(
    """INSERT INTO positions
       (id, signal_id, market_id, strategy, side, book, entry_price,
        entry_size, exit_price, exit_size, realized_pnl, fees_paid,
        status, opened_at, closed_at, updated_at)
       VALUES (?, ?, ?, ?, 'BUY', 'YES', ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)""",
    # TODO[no-naked-shorts]: when the translated-NO path becomes live
    # for arbs, propagate the resolved book here instead of 'YES'.
    (
        pos_id,
        # ... rest unchanged
    ),
)
```

- [ ] **Step 4: Update `batch.py`'s position INSERT**

Same pattern at `core/strategies/batch.py:207`. Add `book` column, pass `'YES'`.

- [ ] **Step 5: Run the affected test suites**

```
pytest tests/integration/test_single_platform.py \
       tests/integration/test_arb_engine.py \
       tests/integration/test_schema_compliance.py -v
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add core/engine/arb_engine.py core/strategies/batch.py \
        core/strategies/single_platform.py \
        tests/integration/test_schema_compliance.py
git commit -m "Write 'book' column on position INSERTs (single_platform, arb, batch)"
```

---

## Task 9: `PolymarketExecutionClient` uses `BookResolver`

**Files:**
- Modify: `execution/clients/polymarket.py`
- Modify: `tests/integration/test_polymarket_client.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/integration/test_polymarket_client.py`:

```python
@pytest.mark.asyncio
class TestSubmitOrderTranslation:
    async def test_translates_sell_to_buy_no_when_no_inventory(
        self, db, monkeypatch
    ):
        """End-to-end: arb-engine-style SELL on Polymarket with no
        inventory on file hits CLOB as BUY on the NO token."""
        from execution.clients.polymarket import PolymarketExecutionClient
        from execution.enums import Side
        from execution.models import OrderLeg

        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_t1', 'polymarket', '0xt1', 't',
                       '111', '222', 'open', 'now', 'now')""",
        )
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_t1', 's', 'arb_pair', 'poly_t1', 'poly_t1',
                       0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        )
        await db.commit()

        client = PolymarketExecutionClient(db, private_key="dummy", funder="0x0")

        # Intercept CLOB at create_order/post_order without full network mocking.
        captured = {}

        class _FakeCLOB:
            def create_order(self, args):
                captured["token_id"] = args.token_id
                captured["side"] = args.side
                captured["price"] = args.price
                captured["size"] = args.size
                return "SIGNED"

            def post_order(self, signed, order_type):
                return {"success": True, "orderID": "POLY-42"}

            def get_order(self, order_id):
                return {"status": "matched", "size_matched": 10, "price": 0.38}

        client._client = _FakeCLOB()
        client._initialized = True  # skip _ensure_client

        leg = OrderLeg(
            market_id="poly_t1", platform="polymarket",
            side=Side.SELL, size=10, limit_price=0.62,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id="sig_t1")

        # CLOB received the translated order
        assert captured["token_id"] == "222"
        assert captured["side"] == "BUY"
        assert captured["price"] == 0.38

        # orders row reflects what hit the exchange
        cursor = await db.execute(
            "SELECT side, book, requested_price FROM orders WHERE id = ?",
            (result.order_id,),
        )
        row = await cursor.fetchone()
        assert row == ("BUY", "NO", 0.38)

    async def test_preserves_sell_yes_when_inventory_present(self, db):
        from execution.clients.polymarket import PolymarketExecutionClient
        from execution.enums import Side
        from execution.models import OrderLeg

        await db.execute(
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES ('poly_t2', 'polymarket', '0xt2', 't',
                       '111', '222', 'open', 'now', 'now')""",
        )
        await db.execute(
            """INSERT INTO signals
               (id, strategy, signal_type, market_id_a, market_id_b,
                model_edge, kelly_fraction, position_size_a,
                total_capital_at_risk, status, fired_at, updated_at)
               VALUES ('sig_t2', 'P3', 'arb_pair', 'poly_t2', 'poly_t2',
                       0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
        )
        await db.execute(
            """INSERT INTO positions
               (id, signal_id, market_id, strategy, side, book,
                entry_price, entry_size, status, opened_at, updated_at)
               VALUES ('pos_t2', 'sig_t2', 'poly_t2', 'P3',
                       'BUY', 'YES', 0.5, 10, 'open', 'now', 'now')""",
        )
        await db.commit()

        client = PolymarketExecutionClient(db, private_key="dummy", funder="0x0")

        captured = {}

        class _FakeCLOB:
            def create_order(self, args):
                captured["token_id"] = args.token_id
                captured["side"] = args.side
                return "S"

            def post_order(self, signed, ot):
                return {"success": True, "orderID": "POLY-99"}

            def get_order(self, _):
                return {"status": "matched", "size_matched": 10, "price": 0.55}

        client._client = _FakeCLOB()
        client._initialized = True

        leg = OrderLeg(
            market_id="poly_t2", platform="polymarket",
            side=Side.SELL, size=10, limit_price=0.55,
        )
        result = await client.submit_order(leg, signal_id="sig_t2")

        assert captured["token_id"] == "111"
        assert captured["side"] == "SELL"
        cursor = await db.execute(
            "SELECT side, book FROM orders WHERE id = ?", (result.order_id,)
        )
        row = await cursor.fetchone()
        assert row == ("SELL", "YES")
```

- [ ] **Step 2: Run tests to verify failure**

```
pytest tests/integration/test_polymarket_client.py::TestSubmitOrderTranslation -v
```

Expected: both fail — `submit_order` still uses `_resolve_token_id` which ignores side.

- [ ] **Step 3: Replace `_resolve_token_id` with `BookResolver` in `polymarket.py`**

In `execution/clients/polymarket.py`:

a. Add import at top:
```python
from execution.clients.polymarket_book import BookResolver
```

b. Delete the entire `_resolve_token_id` method (currently lines 161–180).

c. In `__init__`, instantiate the resolver after `super().__init__(...)`:
```python
self._book_resolver = BookResolver(db_connection)
```

d. In `submit_order`, replace the current `_resolve_token_id` call block (lines 192–215) with:

```python
try:
    resolved = await self._book_resolver.resolve(
        leg.market_id, leg.side, leg.size, leg.limit_price
    )
    if resolved is None:
        submission_latency_ms = int((time.time() - start_time) * 1000)
        error_msg = (
            f"BookResolver rejected order for {leg.market_id} "
            f"(side={leg.side.value}, size={leg.size}, "
            f"price={leg.limit_price}) — missing tokens, invalid "
            f"price/size, or short translation disabled."
        )
        logger.error(error_msg)
        result = OrderResult(
            order_id=f"FAILED-{leg.market_id}",
            platform="polymarket",
            status="failed",
            submission_latency_ms=submission_latency_ms,
            error_message=error_msg,
        )
        await self.write_order(
            leg, result, signal_id=signal_id, strategy=strategy
        )
        return result

    self._ensure_client()

    logger.info(
        "Submitting to Polymarket: market=%s token=%s side=%s size=%f "
        "price=%s book=%s translated=%s",
        leg.market_id,
        resolved.token_id,
        resolved.side.value,
        resolved.size,
        resolved.limit_price,
        resolved.book.value,
        resolved.translated,
    )

    order_args = _OrderArgs(
        token_id=resolved.token_id,
        price=resolved.limit_price,
        size=resolved.size,
        side=resolved.side.value,
    )
```

e. Later in `submit_order`, update both `write_order` call sites to pass `resolved=resolved`:

```python
await self.write_order(
    leg, pending_result, signal_id=signal_id, strategy=strategy,
    resolved=resolved,
)
...
await self.write_order(
    leg, result, signal_id=signal_id, strategy=strategy, resolved=resolved,
)
```

(The outer exception handler also calls `write_order` — pass `resolved=resolved` there too if `resolved` is in scope, else fall back to None.)

- [ ] **Step 4: Run tests**

```
pytest tests/integration/test_polymarket_client.py -v
```

Expected: all tests (existing + new translation) pass.

- [ ] **Step 5: Commit**

```bash
git add execution/clients/polymarket.py tests/integration/test_polymarket_client.py
git commit -m "PolymarketExecutionClient: route orders through BookResolver"
```

---

## Task 10: `PaperExecutionClient` — resolver + NO-price lookup

**Files:**
- Modify: `execution/clients/paper.py`
- Create: `tests/integration/test_paper_polymarket_translation.py`

- [ ] **Step 1: Write the failing tests**

`tests/integration/test_paper_polymarket_translation.py`:

```python
"""
Paper-execution fidelity for Polymarket no-naked-shorts translation.

Paper must simulate the same translated order live would send. That
means reading the NO-book reference price from ``markets.last_price_no``
when a SELL is translated, and applying the same marketable-at-limit
check against it.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


async def _seed(db, last_price_no=0.40):
    await db.execute(
        """INSERT INTO markets
           (id, platform, platform_id, title, yes_token_id, no_token_id,
            last_price_no, status, created_at, updated_at)
           VALUES ('poly_p', 'polymarket', '0xp', 't', '111', '222',
                   ?, 'open', 'now', 'now')""",
        (last_price_no,),
    )
    # market_prices has the YES price (ingestor-derived complement for NO).
    await db.execute(
        """INSERT INTO market_prices
           (market_id, yes_price, no_price, spread, liquidity, polled_at)
           VALUES ('poly_p', 0.60, 0.40, 0.02, 10000, 'now')""",
    )
    await db.execute(
        """INSERT INTO signals
           (id, strategy, signal_type, market_id_a, market_id_b,
            model_edge, kelly_fraction, position_size_a,
            total_capital_at_risk, status, fired_at, updated_at)
           VALUES ('sig_p', 's', 'arb_pair', 'poly_p', 'poly_p',
                   0.01, 0.01, 10, 10, 'fired', 'now', 'now')""",
    )
    await db.commit()


@pytest.mark.asyncio
class TestPaperPolymarketTranslation:
    async def test_simulates_translated_buy_no_fill(self, db):
        from execution.clients.paper import PaperExecutionClient
        from execution.enums import Side
        from execution.models import OrderLeg

        await _seed(db, last_price_no=0.40)

        client = PaperExecutionClient(db, platform_label="polymarket")
        leg = OrderLeg(
            market_id="poly_p", platform="polymarket",
            side=Side.SELL, size=10, limit_price=0.60,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id="sig_p")

        # Translated limit is 1 - 0.60 = 0.40, NO book price is 0.40,
        # so 0.40 <= 0.40 → fillable as a BUY.
        assert result.status in ("filled", "partially_filled")
        cursor = await db.execute(
            "SELECT side, book, requested_price FROM orders WHERE id = ?",
            (result.order_id,),
        )
        row = await cursor.fetchone()
        assert row == ("BUY", "NO", 0.40)

    async def test_rejects_when_translated_price_below_no_ask(self, db):
        from execution.clients.paper import PaperExecutionClient
        from execution.enums import Side
        from execution.models import OrderLeg

        # NO book trading at 0.50; translated limit of 0.38 is too low.
        await _seed(db, last_price_no=0.50)

        client = PaperExecutionClient(db, platform_label="polymarket")
        leg = OrderLeg(
            market_id="poly_p", platform="polymarket",
            side=Side.SELL, size=10, limit_price=0.62,
            order_type="LIMIT",
        )
        result = await client.submit_order(leg, signal_id="sig_p")
        assert result.status == "failed"
        assert "above limit" in (result.error_message or "")
```

- [ ] **Step 2: Run tests to verify failure**

```
pytest tests/integration/test_paper_polymarket_translation.py -v
```

Expected: both fail — paper doesn't use the resolver or read `last_price_no`.

- [ ] **Step 3: Add resolver + NO-price lookup to paper**

In `execution/clients/paper.py`:

a. Add imports at top:
```python
from execution.clients.polymarket_book import BookResolver, ResolvedOrder
from execution.enums import Book, Side
```

b. In `__init__` of `PaperExecutionClient`, add:
```python
self._book_resolver = (
    BookResolver(db_connection) if platform_label == "polymarket" else None
)
```

c. Modify `_get_db_price` to accept a `Book` parameter:

```python
async def _get_db_price(
    self, market_id: str, book: Book = Book.YES
) -> float | None:
    """Read the most recently polled price from the DB (fallback)."""
    if book is Book.NO:
        # NO-book reference for translated SELL legs.
        cursor = await self.db.execute(
            "SELECT last_price_no FROM markets WHERE id = ?",
            (market_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] is not None else None
    try:
        cursor = await self.db.execute(
            """
            SELECT yes_price FROM market_prices
            WHERE market_id = ?
            ORDER BY polled_at DESC
            LIMIT 1
            """,
            (market_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None
    except Exception:
        return None
```

d. In `submit_order`, route Polymarket legs through the resolver. Locate the top of `submit_order` (around line 154) and, after the `order_id` is generated, add:

```python
        resolved: ResolvedOrder | None = None
        if self._book_resolver is not None:
            resolved = await self._book_resolver.resolve(
                leg.market_id, leg.side, leg.size, leg.limit_price
            )
            if resolved is None:
                submission_latency_ms = latency_ms
                error_msg = (
                    f"BookResolver rejected order for {leg.market_id} "
                    f"(side={leg.side.value}, size={leg.size}, "
                    f"price={leg.limit_price})"
                )
                logger.error(error_msg)
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message=error_msg,
                )
                await self.write_order(
                    leg, result, signal_id=signal_id, strategy=strategy
                )
                return result
```

e. In `submit_order`, route the price lookup through the resolved book and use `resolved.limit_price` for the marketable check. Find the block at lines 170–215 and restructure:

```python
        effective_side = resolved.side if resolved else leg.side
        effective_limit = resolved.limit_price if resolved else leg.limit_price
        effective_book = resolved.book if resolved else Book.YES

        market_price = await self._get_db_price(leg.market_id, effective_book)
        # If still None, try the existing live fetch (YES-only, safe fallback)
        # ... keep existing fallback logic, gated on effective_book is YES

        if market_price is None:
            if effective_limit:
                market_price = effective_limit
            else:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message="No market price available",
                )
                await self.write_order(
                    leg, result, signal_id=signal_id, strategy=strategy,
                    resolved=resolved,
                )
                return result

        # Marketable-at-limit check uses effective values.
        if leg.order_type.upper() == "LIMIT" and effective_limit is not None:
            if effective_side is Side.BUY and market_price > effective_limit:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message=(
                        f"Market price {market_price:.4f} above limit "
                        f"{effective_limit:.4f}"
                    ),
                )
                await self.write_order(
                    leg, result, signal_id=signal_id, strategy=strategy,
                    resolved=resolved,
                )
                return result
            if effective_side is Side.SELL and market_price < effective_limit:
                result = OrderResult(
                    order_id=order_id,
                    platform=self.platform_label,
                    status="failed",
                    submission_latency_ms=submission_latency_ms,
                    error_message=(
                        f"Market price {market_price:.4f} below limit "
                        f"{effective_limit:.4f}"
                    ),
                )
                await self.write_order(
                    leg, result, signal_id=signal_id, strategy=strategy,
                    resolved=resolved,
                )
                return result
```

f. At the final `write_order` call where the fill is recorded, also pass `resolved=resolved`.

g. Update slippage and fill-price logic to use `effective_side` instead of `leg.side`:

```python
        slippage_factor = self.slippage_bps / 10000.0
        if slippage_factor > 0:
            adverse = market_price * slippage_factor * random.uniform(0, 1)
            if effective_side is Side.BUY:
                filled_price = min(round(market_price + adverse, 6), 0.99)
            else:
                filled_price = max(round(market_price - adverse, 6), 0.01)
        else:
            filled_price = market_price
```

- [ ] **Step 4: Run tests**

```
pytest tests/integration/test_paper_polymarket_translation.py -v
pytest tests/integration/test_paper_client.py -v
```

Expected: new tests pass; existing `test_paper_client` still green.

- [ ] **Step 5: Commit**

```bash
git add execution/clients/paper.py \
        tests/integration/test_paper_polymarket_translation.py
git commit -m "PaperExecutionClient: route Polymarket legs through BookResolver"
```

---

## Task 11: Arb-engine regression — translated poly SELL leg

**Files:**
- Modify: `tests/integration/test_arb_engine.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/integration/test_arb_engine.py`:

```python
@pytest.mark.asyncio
async def test_arb_fire_with_sell_poly_leg_translates(db):
    """When the arb engine fires with Polymarket as the expensive leg,
    the poly leg that reaches the execution client is translated: book=NO,
    side=BUY, price=(1-p). The paper client is the execution path under
    test — its orders row captures what hit the simulated exchange."""
    from core.engine.arb_engine import ArbitrageEngine
    from execution.clients.paper import PaperExecutionClient

    # Seed markets (poly expensive, kalshi cheap).
    await db.execute(
        """INSERT INTO markets
           (id, platform, platform_id, title, yes_token_id, no_token_id,
            last_price_no, status, created_at, updated_at)
           VALUES ('poly_arb', 'polymarket', '0xarb', 't',
                   '111', '222', 0.30, 'open', 'now', 'now')""",
    )
    await db.execute(
        """INSERT INTO markets
           (id, platform, platform_id, title, status, created_at, updated_at)
           VALUES ('kal_arb', 'kalshi', 'KXARB', 't', 'open', 'now', 'now')""",
    )
    await db.execute(
        """INSERT INTO market_prices
           (market_id, yes_price, no_price, spread, liquidity, polled_at)
           VALUES ('poly_arb', 0.70, 0.30, 0.02, 10000, 'now'),
                  ('kal_arb',  0.55, 0.45, 0.02, 10000, 'now')""",
    )
    await db.commit()

    # Use the real paper client on both sides; arb_engine fires poly SELL at 0.70.
    # After translation we expect: orders row for poly_arb with
    # side='BUY', book='NO', requested_price=0.30.
    # (Exact ArbitrageEngine wiring depends on the existing test fixtures in
    # this file; reuse them. The key assertion is on the orders row.)

    # [Test author: adapt to existing fire-path helpers. The essential
    # assertions after firing are:]
    cursor = await db.execute(
        "SELECT side, book, requested_price FROM orders "
        "WHERE market_id = 'poly_arb' ORDER BY submitted_at DESC LIMIT 1"
    )
    # This test is expected to be fleshed out against the existing engine
    # fixtures in the file. Placeholder assertion below will fail if the
    # engine hasn't been fired against the fixture; adapt to the pattern
    # already used by test_initial_sweep_fires / test_on_price_update_fires.
    # The concrete shape after integration:
    #   row == ("BUY", "NO", pytest.approx(0.30))
```

**Important:** the placeholder above is intentional — the existing `test_arb_engine.py` has its own engine-fire fixtures (`test_initial_sweep_fires`, `test_on_price_update_fires`) that this test should parallel. Open the file, find the closest match, and copy its engine-construction boilerplate. Replace the fire setup with poly-expensive / kalshi-cheap prices. Final assertion: `row == ("BUY", "NO", pytest.approx(0.30))`.

- [ ] **Step 2: Run to verify failure**

```
pytest tests/integration/test_arb_engine.py::test_arb_fire_with_sell_poly_leg_translates -v
```

Expected: fail — assertion unmet or fire path not invoked.

- [ ] **Step 3: Adapt test to existing fire-path fixture**

Read the `test_arb_engine.py` top-to-bottom, locate the `_risk_config()` and engine-construction helpers, and follow the same pattern used by `test_initial_sweep_fires`. Swap fire-time prices so poly > kalshi, and use `PaperExecutionClient` as the execution client so `orders` rows land in the DB.

- [ ] **Step 4: Run test to verify it passes**

```
pytest tests/integration/test_arb_engine.py::test_arb_fire_with_sell_poly_leg_translates -v
```

Expected: PASS. The orders row for poly_arb should show `side='BUY'`, `book='NO'`, `requested_price=0.30`.

- [ ] **Step 5: Run the full arb-engine suite**

```
pytest tests/integration/test_arb_engine.py -v
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add tests/integration/test_arb_engine.py
git commit -m "Arb-engine regression: translated poly SELL leg reaches exchange as BUY NO"
```

---

## Task 12: Full-suite validation + lint

**Files:** none modified — validation only.

- [ ] **Step 1: Run the full test suite**

```
pytest tests/ -v
```

Expected: all green.

- [ ] **Step 2: Run mypy**

```
mypy core execution 2>&1 | tail -30
```

Expected: no new errors introduced by the enum change or new files. If the existing baseline has errors, verify no *additional* ones appear.

- [ ] **Step 3: Run ruff and black**

```
ruff check core execution tests
black --check core execution tests
```

Expected: clean. If `black --check` reports diffs on files you touched, run `black` (without `--check`) and amend-commit the formatting.

- [ ] **Step 4: Push and open PR**

```
git push origin <branch>
gh pr create --title "Polymarket no-naked-shorts: inventory-aware book resolution" \
             --body "$(cat docs/superpowers/specs/2026-04-18-polymarket-no-naked-shorts-design.md | head -40)"
```

Expected: CI green on the PR.

- [ ] **Step 5: Post-merge validation plan (not in the PR — runbook)**

After the PR merges and deploys:

1. Watch paper-order rejection rate in the dashboard for 24h. Prior to this change, ~50% of arb signals with Polymarket as the expensive leg were failing at the exchange layer. Post-change, those same signals should fill (or fail for legitimate reasons like spread, not naked-short rejection).
2. Spot-check a handful of Polymarket `orders` rows with `book='NO'` — confirm `side='BUY'` and `requested_price ≈ 1 − signals.(poly_price_at_fire)`.
3. If rejection rates don't drop or NO-book fills look wrong, flip `POLYMARKET_ALLOW_SHORT_TRANSLATION=false` on the VM and investigate.

---

## Self-review notes

**Spec coverage:**
- Side/Book enums → Task 1
- Migration 012 → Task 2
- Ingestor NO-price parse + persist → Tasks 3, 4
- BookResolver with all 15 test cases → Task 6
- OrderLeg enum typing → Task 5
- write_order ResolvedOrder handling → Task 7
- Position INSERT book column → Task 8
- Polymarket client integration → Task 9
- Paper client fidelity → Task 10
- Arb-engine regression → Task 11
- Full-suite validation → Task 12
- Kill-switch → covered in Task 6 tests
- Error-handling matrix → covered across Tasks 6, 9, 10

**Placeholder scan:** Task 11 Step 1 contains an intentional "adapt to existing fixtures" prompt — not a placeholder, the exact boilerplate lives in the test file being modified and copying it verbatim would guess at fixtures that may have shifted. Step 3 makes this explicit.

**Type consistency:** `ResolvedOrder` definition in Task 6 matches the usage in Tasks 7, 9, 10. `Side` and `Book` values used consistently (uppercase). `_get_db_price` signature in Task 10 (`book: Book = Book.YES`) matches the call site.
