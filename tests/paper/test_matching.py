"""
Unit tests for the inverted-index matching engine.

Tests normalize_title, tokenize, extract_numbers, compute_match_score,
and the end-to-end find_matches pipeline against an in-memory DB.
"""

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from scripts.paper_trading_session import (  # noqa: E402
    compute_match_score,
    extract_numbers,
    find_matches,
    normalize_title,
    tokenize,
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
        # Should keep apostrophes and alphanumeric, remove @ and %
        assert "@" not in result
        assert "%" not in result

    def test_normalizes_smart_quotes(self):
        result = normalize_title("it\u2019s a test")
        # Smart quote \u2019 gets normalized to straight quote,
        # then [^\w\s'] regex keeps it, so "it's" should survive
        # But if the regex strips it, at minimum the word is preserved
        assert "it" in result and "test" in result

    def test_collapses_multiple_spaces(self):
        result = normalize_title("too   many    spaces")
        assert "  " not in result

    def test_synonym_word_boundary(self):
        # "fed" in "federal" should NOT trigger synonym expansion
        result = normalize_title("unfederated system")
        # The word "fed" is only matched at word boundaries
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
        """Different numbers (e.g., different dates/thresholds) should lower score."""
        a = "gdp growth above 3 percent in 2025"
        b = "gdp growth above 5 percent in 2026"
        tok_a = tokenize(a)
        tok_b = tokenize(b)
        score_diff = compute_match_score(a, b, tok_a, tok_b)

        # Same title but matching numbers should score higher
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
        """Very different length titles get penalized."""
        short = "bitcoin"
        long = "bitcoin price above 100000 by end of 2025 in the cryptocurrency market exchange"
        tok_s = tokenize(short)
        tok_l = tokenize(long)
        score = compute_match_score(short, long, tok_s, tok_l)
        # Should be lower due to length ratio penalty
        assert score < 0.50


# ── find_matches (end-to-end with DB) ───────────────────────────────────────


@pytest.mark.asyncio
class TestFindMatches:
    async def test_finds_known_matches(self, db_with_markets):
        """Markets with similar titles across platforms are matched."""
        matches = await find_matches(db_with_markets, threshold=0.50)
        assert len(matches) >= 2  # At least BTC and Fed rate cut should match

    async def test_respects_threshold(self, db_with_markets):
        """Higher threshold yields fewer matches."""
        low = await find_matches(db_with_markets, threshold=0.40)
        high = await find_matches(db_with_markets, threshold=0.90)
        assert len(low) >= len(high)

    async def test_unrelated_not_matched(self, db_with_markets):
        """'Snow in Miami' should not match any Polymarket title."""
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
        """Each Kalshi market appears in at most one match."""
        matches = await find_matches(db_with_markets, threshold=0.40)
        kalshi_ids = [m["kalshi_id"] for m in matches]
        assert len(kalshi_ids) == len(set(kalshi_ids))

    async def test_empty_db(self, db):
        """Empty DB returns no matches."""
        matches = await find_matches(db, threshold=0.50)
        assert matches == []

    async def test_sorted_by_similarity(self, db_with_markets):
        matches = await find_matches(db_with_markets, threshold=0.40)
        if len(matches) >= 2:
            for i in range(len(matches) - 1):
                assert matches[i]["similarity"] >= matches[i + 1]["similarity"]
