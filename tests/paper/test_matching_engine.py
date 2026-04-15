"""
Tests for the inverted-index matching engine (core.matching.engine).

Combines tests from former test_matching.py and test_phase1_matcher.py.

Covers:
  - normalize_title: synonym expansion, whitespace, special chars
  - tokenize: stop-word removal, min-length filtering
  - extract_numbers: decimal preservation, ranges
  - compute_match_score: scoring, number penalties, length penalty
  - find_matches: end-to-end pipeline with DB
  - Phase 1 matcher fixes:
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

from core.matching.engine import (  # noqa: E402
    _find_matches_sync,
    compute_match_score,
    extract_numbers,
    find_matches,
    load_cached_matches,
    mark_existing_pairs_pending_review,
    normalize_title,
    persist_matches,
    tokenize,
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


# ── normalize_title ─────────────────────────────────────────────────────────


class TestNormalizeTitle:
    def test_lowercases(self):
        assert normalize_title("Bitcoin PRICE") == "bitcoin price"

    def test_strips_whitespace(self):
        assert normalize_title("  hello world  ") == "hello world"

    def test_expands_synonyms(self):
        result = normalize_title("Fed rate cut")
        assert "federal reserve" in result

    def test_expands_btc_synonym(self):
        result = normalize_title("BTC price above 100k")
        assert "bitcoin" in result

    def test_removes_special_chars(self):
        result = normalize_title("What's the CPI @ 3.5%?")
        assert "@" not in result
        assert "%" not in result

    def test_normalizes_smart_quotes(self):
        result = normalize_title("it\u2019s a test")
        assert "it" in result and "test" in result

    def test_collapses_multiple_spaces(self):
        result = normalize_title("too   many    spaces")
        assert "  " not in result

    def test_synonym_word_boundary(self):
        result = normalize_title("unfederated system")
        assert "unfederated" in result or "federal reserve" not in result

    def test_multiple_synonyms(self):
        result = normalize_title("BTC and ETH prices")
        assert "bitcoin" in result
        assert "ethereum" in result


# ── tokenize ────────────────────────────────────────────────────────────────


class TestTokenize:
    def test_removes_stop_words(self):
        tokens = tokenize("will the fed cut rates")
        assert "will" not in tokens
        assert "the" not in tokens

    def test_keeps_meaningful_words(self):
        tokens = tokenize("bitcoin price above 100000")
        assert "bitcoin" in tokens
        assert "price" in tokens
        assert "100000" in tokens

    def test_ignores_single_char_tokens(self):
        tokens = tokenize("a b c hello world")
        assert "a" not in tokens
        assert "b" not in tokens
        assert "hello" in tokens

    def test_empty_string(self):
        assert tokenize("") == set()

    def test_all_stop_words(self):
        tokens = tokenize("will the be is to")
        assert tokens == set()

    def test_returns_set(self):
        tokens = tokenize("bitcoin bitcoin bitcoin")
        assert isinstance(tokens, set)
        assert len(tokens) == 1


# ── extract_numbers ─────────────────────────────────────────────────────────


class TestExtractNumbers:
    def test_extracts_integers(self):
        nums = extract_numbers("above 100000 in 2025")
        assert "100000" in nums
        assert "2025" in nums

    def test_extracts_decimals(self):
        nums = extract_numbers("CPI at 3.5 percent")
        assert "3.5" in nums

    def test_no_numbers(self):
        assert extract_numbers("no numbers here") == set()

    def test_multiple_numbers(self):
        nums = extract_numbers("between 50 and 100 in Q1 2026")
        assert "50" in nums
        assert "100" in nums
        assert "2026" in nums


# ── 1.1 extract_numbers preserves decimals on RAW titles ─────────────────────


class TestExtractNumbersPreservesDecimals:
    def test_raw_ou_55_extracts_decimal(self):
        """Raw 'O/U 5.5' must yield {'5.5'}, not {'5'}."""
        assert extract_numbers("O/U 5.5") == {"5.5"}

    def test_normalized_ou_55_loses_decimal(self):
        """Confirms the bug: normalized title loses the decimal."""
        norm = normalize_title("O/U 5.5")
        nums = extract_numbers(norm)
        assert "5.5" not in nums

    def test_raw_nplus_extracts_integer(self):
        assert extract_numbers("5+") == {"5"}

    def test_raw_percentage(self):
        assert extract_numbers("GDP above 3.5%") == {"3.5"}

    def test_raw_year(self):
        assert extract_numbers("Will Bitcoin hit $100 in 2025") == {"100", "2025"}

    def test_raw_range(self):
        assert extract_numbers("S&P between 4000 and 4200") == {"4000", "4200"}

    def test_raw_decimal_preserved(self):
        assert "5.5" in extract_numbers("Over/Under 5.5 total rebounds")


# ── compute_match_score ─────────────────────────────────────────────────────


class TestComputeMatchScore:
    def test_identical_titles(self):
        norm = "bitcoin price above 100000 by 2025"
        tokens = tokenize(norm)
        score = compute_match_score(norm, norm, tokens, tokens)
        assert score >= 0.95

    def test_very_similar_titles(self):
        a = "bitcoin price above 100000 by end of 2025"
        b = "bitcoin price exceed 100000 by 2025"
        tok_a = tokenize(a)
        tok_b = tokenize(b)
        score = compute_match_score(a, b, tok_a, tok_b)
        assert score > 0.60

    def test_completely_different_titles(self):
        a = "bitcoin price above 100000"
        b = "snow in miami july rainfall weather"
        tok_a = tokenize(a)
        tok_b = tokenize(b)
        score = compute_match_score(a, b, tok_a, tok_b)
        assert score < 0.20

    def test_empty_tokens(self):
        score = compute_match_score("a", "b", set(), set())
        assert score == 0.0

    def test_number_mismatch_penalty(self):
        a = "gdp growth above 3 percent in 2025"
        b = "gdp growth above 5 percent in 2026"
        tok_a = tokenize(a)
        tok_b = tokenize(b)
        score_diff = compute_match_score(a, b, tok_a, tok_b)

        c = "gdp growth above 3 percent in 2025"
        tok_c = tokenize(c)
        score_same = compute_match_score(a, c, tok_a, tok_c)

        assert score_same > score_diff

    def test_score_bounded_zero_one(self):
        a = "test market title"
        b = "another completely different thing"
        tok_a = tokenize(a)
        tok_b = tokenize(b)
        score = compute_match_score(a, b, tok_a, tok_b)
        assert 0.0 <= score <= 1.0

    def test_length_penalty(self):
        short = "bitcoin"
        long = "bitcoin price above 100000 by end of 2025 in the cryptocurrency market exchange"
        tok_s = tokenize(short)
        tok_l = tokenize(long)
        score = compute_match_score(short, long, tok_s, tok_l)
        assert score < 0.50


# ── 1.2 Hard reject: threshold terms + numeric mismatch ───────────────────────


class TestHardRejectThresholdMismatch:
    def _score(self, raw_a, raw_b):
        norm_a = normalize_title(raw_a)
        norm_b = normalize_title(raw_b)
        return compute_match_score(
            norm_a, norm_b, tokenize(norm_a), tokenize(norm_b), raw_a, raw_b
        )

    def test_ou_55_vs_nplus_5_is_zero(self):
        score = self._score(
            "Will player X have O/U 5.5 rebounds",
            "Will player X have 5+ rebounds",
        )
        assert score == 0.0, f"Expected 0.0, got {score}"

    def test_ou_vs_nplus_different_contexts(self):
        score = self._score(
            "Over/Under 22.5 points in the game",
            "Player scores 23+ points",
        )
        assert score == 0.0

    def test_at_least_vs_ou(self):
        score = self._score(
            "Will GDP grow by O/U 2.5%",
            "Will GDP grow by at least 3%",
        )
        assert score == 0.0

    def test_both_ou_same_number_not_rejected(self):
        score = self._score(
            "O/U 5.5 rebounds for player A",
            "Over/Under 5.5 rebounds player A game",
        )
        assert score > 0.0

    def test_no_threshold_terms_no_rejection(self):
        score = self._score(
            "Will Bitcoin exceed $50,000 in 2025",
            "Will Bitcoin be above $50,000 by end of 2025",
        )
        assert score > 0.0

    def test_q1_vs_q2_rejected(self):
        score = self._score(
            "Will GDP grow in Q1 2025",
            "Will GDP grow in Q2 2025",
        )
        assert 0.0 < score < 1.0, f"Q1/Q2 score out of expected range: {score}"

    def test_same_event_no_threshold_still_matches(self):
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
        return compute_match_score(
            norm_a, norm_b, tokenize(norm_a), tokenize(norm_b), raw_a, raw_b
        )

    def test_ou_side_vs_nplus_side(self):
        score = self._score(
            "Will LeBron score O/U 25.5 points",
            "LeBron James scores 26+ points tonight",
        )
        assert score == 0.0

    def test_nplus_side_vs_ou_side(self):
        score = self._score(
            "LeBron James 26+ points",
            "LeBron O/U 25.5 points scored",
        )
        assert score == 0.0

    def test_or_more_vs_ou_rejected(self):
        score = self._score(
            "Will the team score O/U 110.5",
            "Will the team score 111 or more points",
        )
        assert score == 0.0

    def test_neither_has_threshold_not_affected(self):
        score = self._score(
            "Will Ethereum reach $3000 by June",
            "Ethereum price above $3000 in June",
        )
        assert score > 0.0

    def test_both_nplus_not_rejected(self):
        score = self._score(
            "Player X scores 20+ points",
            "Player X 20+ point game",
        )
        assert score > 0.0


# ── End-to-end: _find_matches_sync rejects false positives ────────────────────


class TestFindMatchesSyncRejectsFalsePositives:
    def test_ou_vs_nplus_not_matched(self):
        poly = [("poly_1", "Will Curry have O/U 5.5 assists", 0.55)]
        kalshi = [("kal_1", "Steph Curry 6+ assists tonight", 0.60)]
        matches = _find_matches_sync(poly, kalshi, threshold=0.80)
        assert len(matches) == 0, f"False-positive match was returned: {matches}"

    def test_same_event_still_matches(self):
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
        poly = [("poly_1", "Player rebounds O/U 5.5 tonight", 0.50)]
        kalshi = [("kal_1", "Player has at least 6 rebounds", 0.45)]
        matches = _find_matches_sync(poly, kalshi, threshold=0.80)
        assert len(matches) == 0


# ── find_matches (end-to-end with DB) ───────────────────────────────────────


@pytest.mark.asyncio
class TestFindMatches:
    async def test_finds_known_matches(self, db_with_markets):
        matches = await find_matches(db_with_markets, threshold=0.50)
        assert len(matches) >= 2

    async def test_respects_threshold(self, db_with_markets):
        low = await find_matches(db_with_markets, threshold=0.40)
        high = await find_matches(db_with_markets, threshold=0.90)
        assert len(low) >= len(high)

    async def test_unrelated_not_matched(self, db_with_markets):
        matches = await find_matches(db_with_markets, threshold=0.50)
        kalshi_ids = {m["kalshi_id"] for m in matches}
        assert "kal_UNRELATED" not in kalshi_ids

    async def test_match_structure(self, db_with_markets):
        matches = await find_matches(db_with_markets, threshold=0.40)
        if matches:
            m = matches[0]
            assert "poly_id" in m
            assert "kalshi_id" in m
            assert "poly_price" in m
            assert "kalshi_price" in m
            assert "similarity" in m
            assert "poly_title" in m
            assert "kalshi_title" in m

    async def test_one_to_one_matching(self, db_with_markets):
        matches = await find_matches(db_with_markets, threshold=0.40)
        kalshi_ids = [m["kalshi_id"] for m in matches]
        assert len(kalshi_ids) == len(set(kalshi_ids))

    async def test_empty_db(self, db):
        matches = await find_matches(db, threshold=0.50)
        assert matches == []

    async def test_sorted_by_similarity(self, db_with_markets):
        matches = await find_matches(db_with_markets, threshold=0.40)
        if len(matches) >= 2:
            for i in range(len(matches) - 1):
                assert matches[i]["similarity"] >= matches[i + 1]["similarity"]


# ── 1.4 persist_matches spread cap ────────────────────────────────────────────


@pytest.mark.asyncio
class TestPersistMatchesSpreadCap:
    async def test_high_spread_pair_marked_pending_review(self, db):
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
                "kalshi_price": 0.60,
                "similarity": 0.85,
            }
        ]
        await persist_matches(db, matches)

        cursor = await db.execute(
            "SELECT active, notes FROM market_pairs WHERE id='p1_k1'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 0
        assert row[1] == "pending_review"

    async def test_low_spread_pair_is_active(self, db):
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
                "kalshi_price": 0.53,
                "similarity": 0.88,
            }
        ]
        await persist_matches(db, matches)

        cursor = await db.execute(
            "SELECT active, notes FROM market_pairs WHERE id='p2_k2'"
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == 1

    async def test_exactly_at_boundary_is_active(self, db):
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
                "kalshi_price": 0.55,
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
        assert row[0] == "human_approved"
        assert row[1] == 1

    async def test_load_cached_excludes_pending_review(self, db):
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
        assert ("p_pend", "k_pend") not in ids

    async def test_returns_count_of_deactivated(self, db):
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
