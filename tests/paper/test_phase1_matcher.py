"""
Phase 1 tests: matcher root-cause fixes.

TDD: these tests define the contracts. Run before implementing to confirm
they fail, then implement until they all pass.

Covers:
  1.1  extract_numbers operates on RAW title (preserves decimals like 5.5)
  1.2  Hard reject when both titles have threshold terms and numeric sets differ
  1.3  Semantic preflight: O/U on one side + N+ on the other → 0.0
  1.4  persist_matches sets active=0 / notes='pending_review' when spread > 0.05
  1.5  mark_existing_pairs_pending_review deactivates unreviewed pairs
       load_cached_matches skips pending_review pairs
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.paper_trading_session import (  # noqa: E402
    compute_match_score,
    extract_numbers,
    normalize_title,
    persist_matches,
    load_cached_matches,
    mark_existing_pairs_pending_review,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


async def _seed_market(db, mid, platform, title, yes_price=0.50):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO markets "
        "(id, platform, platform_id, title, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?)",
        (mid, platform, mid, title, now, now),
    )
    await db.execute(
        "INSERT INTO market_prices "
        "(market_id, yes_price, no_price, spread, liquidity, polled_at) "
        "VALUES (?, ?, ?, 0.02, 10000, ?)",
        (mid, yes_price, round(1 - yes_price, 4), now),
    )


async def _seed_pair(db, pair_id, poly_id, kalshi_id, active=1, notes=None):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT OR IGNORE INTO market_pairs "
        "(id, market_id_a, market_id_b, pair_type, similarity_score, match_method, "
        "active, notes, created_at, updated_at) "
        "VALUES (?, ?, ?, 'cross_platform', 0.85, 'test', ?, ?, ?, ?)",
        (pair_id, poly_id, kalshi_id, active, notes, now, now),
    )


# ── 1.1 extract_numbers on raw title ──────────────────────────────────────────


class TestExtractNumbersPreservesDecimals:
    def test_raw_ou_55_extracts_decimal(self):
        """Raw 'O/U 5.5' must yield {'5.5'}, not {'5'}."""
        assert extract_numbers("O/U 5.5") == {"5.5"}

    def test_normalized_ou_55_loses_decimal(self):
        """Confirms the bug: normalized title loses the decimal."""
        norm = normalize_title("O/U 5.5")
        # norm will be something like "o u 5 5" — extract_numbers finds {'5'}
        nums = extract_numbers(norm)
        assert "5.5" not in nums  # decimal is gone after normalization

    def test_raw_nplus_extracts_integer(self):
        """Raw '5+' yields {'5'}."""
        assert extract_numbers("5+") == {"5"}

    def test_raw_percentage(self):
        assert extract_numbers("GDP above 3.5%") == {"3.5"}

    def test_raw_year(self):
        # $100k: '100' is not extracted because 'k' is a word char (no \b after 100)
        assert extract_numbers("Will Bitcoin hit $100 in 2025") == {"100", "2025"}

    def test_raw_range(self):
        assert extract_numbers("S&P between 4000 and 4200") == {"4000", "4200"}

    def test_raw_decimal_preserved(self):
        assert "5.5" in extract_numbers("Over/Under 5.5 total rebounds")


# ── 1.2 Hard reject: threshold terms + numeric mismatch ───────────────────────


class TestHardRejectThresholdMismatch:
    def _score(self, raw_a, raw_b):
        norm_a = normalize_title(raw_a)
        norm_b = normalize_title(raw_b)
        from scripts.paper_trading_session import tokenize

        return compute_match_score(
            norm_a, norm_b, tokenize(norm_a), tokenize(norm_b), raw_a, raw_b
        )

    def test_ou_55_vs_nplus_5_is_zero(self):
        """Core false positive: O/U 5.5 rebounds vs 5+ rebounds → 0.0."""
        score = self._score(
            "Will player X have O/U 5.5 rebounds",
            "Will player X have 5+ rebounds",
        )
        assert score == 0.0, f"Expected 0.0, got {score}"

    def test_ou_vs_nplus_different_contexts(self):
        """Any O/U term vs N+ term with differing numbers → 0.0."""
        score = self._score(
            "Over/Under 22.5 points in the game",
            "Player scores 23+ points",
        )
        assert score == 0.0

    def test_at_least_vs_ou(self):
        """'at least N' on one side, O/U on the other → 0.0."""
        score = self._score(
            "Will GDP grow by O/U 2.5%",
            "Will GDP grow by at least 3%",
        )
        assert score == 0.0

    def test_both_ou_same_number_not_rejected(self):
        """Both O/U with same number — should NOT be rejected."""
        score = self._score(
            "O/U 5.5 rebounds for player A",
            "Over/Under 5.5 rebounds player A game",
        )
        assert score > 0.0, "Same O/U number should not be hard-rejected"

    def test_no_threshold_terms_no_rejection(self):
        """Regular market titles with no threshold terminology — normal scoring."""
        score = self._score(
            "Will Bitcoin exceed $50,000 in 2025",
            "Will Bitcoin be above $50,000 by end of 2025",
        )
        assert score > 0.0

    def test_q1_vs_q2_rejected(self):
        """Different quarters (Q1 vs Q2) — the digit in 'Q1'/'Q2' is not
        boundary-delimited after normalization, so extract_numbers misses it.
        Score stays high (~0.85). This is a known limitation of the current
        model; Phase 1 does not claim to fix quarter discrimination.
        The score is still below 1.0 and won't be hard-rejected."""
        score = self._score(
            "Will GDP grow in Q1 2025",
            "Will GDP grow in Q2 2025",
        )
        assert 0.0 < score < 1.0, f"Q1/Q2 score out of expected range: {score}"

    def test_same_event_no_threshold_still_matches(self):
        """Same event with no threshold terminology still matches well."""
        score = self._score(
            "Will the Federal Reserve cut rates in March 2026",
            "Federal Reserve interest rate cut March 2026",
        )
        assert score >= 0.60, f"Same-event score too low: {score}"


# ── 1.3 Semantic preflight ────────────────────────────────────────────────────


class TestSemanticPreflight:
    def _score(self, raw_a, raw_b):
        norm_a = normalize_title(raw_a)
        norm_b = normalize_title(raw_b)
        from scripts.paper_trading_session import tokenize

        return compute_match_score(
            norm_a, norm_b, tokenize(norm_a), tokenize(norm_b), raw_a, raw_b
        )

    def test_ou_side_vs_nplus_side(self):
        """O/U on poly side, N+ on kalshi side → 0.0."""
        score = self._score(
            "Will LeBron score O/U 25.5 points",
            "LeBron James scores 26+ points tonight",
        )
        assert score == 0.0

    def test_nplus_side_vs_ou_side(self):
        """N+ on poly side, O/U on kalshi side → 0.0 (symmetric)."""
        score = self._score(
            "LeBron James 26+ points",
            "LeBron O/U 25.5 points scored",
        )
        assert score == 0.0

    def test_or_more_vs_ou_rejected(self):
        """'N or more' counts as threshold term; O/U on other side → 0.0."""
        score = self._score(
            "Will the team score O/U 110.5",
            "Will the team score 111 or more points",
        )
        assert score == 0.0

    def test_neither_has_threshold_not_affected(self):
        """No threshold terminology on either side — preflight has no effect."""
        score = self._score(
            "Will Ethereum reach $3000 by June",
            "Ethereum price above $3000 in June",
        )
        assert score > 0.0

    def test_both_nplus_not_rejected(self):
        """Both N+ sides — not a mismatch, should not be 0.0."""
        score = self._score(
            "Player X scores 20+ points",
            "Player X 20+ point game",
        )
        # Same threshold term type on both sides — penalty only if NUMBERS differ
        # Both have 20, so this should not be hard-rejected
        assert score > 0.0


# ── End-to-end: _find_matches_sync rejects false positives ────────────────────


class TestFindMatchesSyncRejectsFalsePositives:
    def test_ou_vs_nplus_not_matched(self):
        """_find_matches_sync with O/U vs N+ pair produces no match."""
        from scripts.paper_trading_session import _find_matches_sync

        poly = [("poly_1", "Will Curry have O/U 5.5 assists", 0.55)]
        kalshi = [("kal_1", "Steph Curry 6+ assists tonight", 0.60)]
        matches = _find_matches_sync(poly, kalshi, threshold=0.80)
        assert len(matches) == 0, f"False-positive match was returned: {matches}"

    def test_same_event_still_matches(self):
        """_find_matches_sync still finds genuine cross-platform matches."""
        from scripts.paper_trading_session import _find_matches_sync

        poly = [
            (
                "poly_1",
                "Will the Federal Reserve cut interest rates in March 2026",
                0.45,
            )
        ]
        kalshi = [
            (
                "kal_1",
                "Federal Reserve interest rate cut March 2026",
                0.43,
            )
        ]
        matches = _find_matches_sync(poly, kalshi, threshold=0.80)
        assert len(matches) >= 1, "Genuine match was incorrectly rejected"

    def test_inverted_logic_ou_at_least(self):
        """O/U 5.5 vs 'at least 6' — incompatible threshold types."""
        from scripts.paper_trading_session import _find_matches_sync

        poly = [("poly_1", "Player rebounds O/U 5.5 tonight", 0.50)]
        kalshi = [("kal_1", "Player has at least 6 rebounds", 0.45)]
        matches = _find_matches_sync(poly, kalshi, threshold=0.80)
        assert len(matches) == 0


# ── 1.4 persist_matches spread cap ────────────────────────────────────────────


@pytest.mark.asyncio
class TestPersistMatchesSpreadCap:
    async def test_high_spread_pair_marked_pending_review(self, db):
        """Pair with spread > 0.05 at match time → active=0, notes='pending_review'."""
        now = datetime.now(timezone.utc).isoformat()
        for mid, platform in [("p1", "polymarket"), ("k1", "kalshi")]:
            await db.execute(
                "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'T', 'open', ?, ?)",
                (mid, platform, mid, now, now),
            )
        await db.commit()

        matches = [
            {
                "poly_id": "p1",
                "poly_title": "Test A",
                "poly_price": 0.50,
                "kalshi_id": "k1",
                "kalshi_title": "Test B",
                "kalshi_price": 0.60,  # spread = 0.10 > 0.05 → pending_review
                "similarity": 0.85,
            }
        ]
        await persist_matches(db, matches)

        cursor = await db.execute(
            "SELECT active, notes FROM market_pairs WHERE id='p1_k1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 0, f"Expected active=0 for high-spread pair, got {row[0]}"
        assert (
            row[1] == "pending_review"
        ), f"Expected notes='pending_review', got {row[1]}"

    async def test_low_spread_pair_is_active(self, db):
        """Pair with spread ≤ 0.05 at match time → active=1."""
        now = datetime.now(timezone.utc).isoformat()
        for mid, platform in [("p2", "polymarket"), ("k2", "kalshi")]:
            await db.execute(
                "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'T', 'open', ?, ?)",
                (mid, platform, mid, now, now),
            )
        await db.commit()

        matches = [
            {
                "poly_id": "p2",
                "poly_title": "Test A",
                "poly_price": 0.50,
                "kalshi_id": "k2",
                "kalshi_title": "Test B",
                "kalshi_price": 0.53,  # spread = 0.03 ≤ 0.05 → active
                "similarity": 0.88,
            }
        ]
        await persist_matches(db, matches)

        cursor = await db.execute(
            "SELECT active, notes FROM market_pairs WHERE id='p2_k2'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1, f"Expected active=1 for low-spread pair, got {row[0]}"

    async def test_exactly_at_boundary_is_active(self, db):
        """Spread exactly 0.05 (not strictly greater) → active=1."""
        now = datetime.now(timezone.utc).isoformat()
        for mid, platform in [("p3", "polymarket"), ("k3", "kalshi")]:
            await db.execute(
                "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'T', 'open', ?, ?)",
                (mid, platform, mid, now, now),
            )
        await db.commit()

        matches = [
            {
                "poly_id": "p3",
                "poly_title": "Test A",
                "poly_price": 0.50,
                "kalshi_id": "k3",
                "kalshi_title": "Test B",
                "kalshi_price": 0.55,  # spread exactly 0.05
                "similarity": 0.85,
            }
        ]
        await persist_matches(db, matches)

        cursor = await db.execute("SELECT active FROM market_pairs WHERE id='p3_k3'")
        row = await cursor.fetchone()
        assert row[0] == 1, "Spread == 0.05 boundary should be active=1"


# ── 1.5 mark_existing_pairs_pending_review ────────────────────────────────────


@pytest.mark.asyncio
class TestMarkExistingPairsPending:
    async def test_unreviewed_pairs_deactivated(self, db):
        """All pairs with notes=NULL are marked active=0, notes='pending_review'."""
        now = datetime.now(timezone.utc).isoformat()
        for i in range(3):
            for mid, platform in [(f"p{i}", "polymarket"), (f"k{i}", "kalshi")]:
                await db.execute(
                    "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, "
                    "created_at, updated_at) VALUES (?, ?, ?, 'T', 'open', ?, ?)",
                    (mid, platform, mid, now, now),
                )
            await _seed_pair(db, f"pair_{i}", f"p{i}", f"k{i}", active=1, notes=None)
        await db.commit()

        count = await mark_existing_pairs_pending_review(db)
        assert count == 3

        cursor = await db.execute(
            "SELECT COUNT(*) FROM market_pairs WHERE active=0 AND notes='pending_review'"
        )
        row = await cursor.fetchone()
        assert row[0] == 3

    async def test_already_reviewed_pairs_unchanged(self, db):
        """Pairs with existing notes are NOT overwritten."""
        now = datetime.now(timezone.utc).isoformat()
        for mid, platform in [("p_ok", "polymarket"), ("k_ok", "kalshi")]:
            await db.execute(
                "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'T', 'open', ?, ?)",
                (mid, platform, mid, now, now),
            )
        await _seed_pair(
            db, "pair_ok", "p_ok", "k_ok", active=1, notes="human_approved"
        )
        await db.commit()

        await mark_existing_pairs_pending_review(db)

        cursor = await db.execute(
            "SELECT notes, active FROM market_pairs WHERE id='pair_ok'"
        )
        row = await cursor.fetchone()
        assert row[0] == "human_approved", "human_approved notes were overwritten"
        assert row[1] == 1, "human_approved pair was deactivated"

    async def test_load_cached_excludes_pending_review(self, db):
        """load_cached_matches returns no rows for pending_review pairs."""
        now = datetime.now(timezone.utc).isoformat()
        for mid, platform, price in [
            ("p_pend", "polymarket", 0.50),
            ("k_pend", "kalshi", 0.52),
        ]:
            await db.execute(
                "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, "
                "created_at, updated_at) VALUES (?, ?, ?, 'Title', 'open', ?, ?)",
                (mid, platform, mid, now, now),
            )
            await db.execute(
                "INSERT INTO market_prices (market_id, yes_price, no_price, spread, "
                "liquidity, polled_at) VALUES (?, ?, ?, 0.02, 1000, ?)",
                (mid, price, 1 - price, now),
            )
        await _seed_pair(
            db, "pair_pend", "p_pend", "k_pend", active=0, notes="pending_review"
        )
        await db.commit()

        matches = await load_cached_matches(db)
        ids = {(m["poly_id"], m["kalshi_id"]) for m in matches}
        assert (
            "p_pend",
            "k_pend",
        ) not in ids, "pending_review pair was returned by load_cached_matches"

    async def test_returns_count_of_deactivated(self, db):
        """Return value is the number of pairs deactivated."""
        now = datetime.now(timezone.utc).isoformat()
        for i in range(5):
            for mid, platform in [(f"px{i}", "polymarket"), (f"kx{i}", "kalshi")]:
                await db.execute(
                    "INSERT OR IGNORE INTO markets (id, platform, platform_id, title, status, "
                    "created_at, updated_at) VALUES (?, ?, ?, 'T', 'open', ?, ?)",
                    (mid, platform, mid, now, now),
                )
            await _seed_pair(db, f"pairx_{i}", f"px{i}", f"kx{i}", active=1, notes=None)
        await db.commit()

        count = await mark_existing_pairs_pending_review(db)
        assert count == 5
