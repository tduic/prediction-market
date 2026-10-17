"""Matcher must not pair markets whose stat qualifier or boundary differ.

Titles are the exact pairs the matcher produced on the full live listing
(2026-10): a "goals + assists" prop is a different market from a "goals"
prop, and "at least 5000" includes 5000 while "more than 5000" does not.
"""

import pytest

from core.matching.engine import (
    _find_matches_sync,
    compute_match_score,
    normalize_title,
    tokenize,
)


def score(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    return compute_match_score(na, nb, tokenize(na), tokenize(nb), a, b)


@pytest.mark.parametrize(
    "poly,kalshi",
    [
        ("Giovani Lo Celso: 1+ goals + assists", "Giovani Lo Celso: 1+ goals"),
        ("Harry Kane: 3+ goals + assists", "Harry Kane: 3+ goals"),
        ("Christopher Nkunku: 1+ goals + assists", "Christopher Nkunku: 1+ goals"),
        ("LeBron James: 10+ points and rebounds", "LeBron James: 10+ points"),
        ("Josh Allen: 250+ passing yards", "Josh Allen: 250+ rushing yards"),
    ],
)
def test_different_stat_qualifiers_rejected(poly, kalshi):
    assert score(poly, kalshi) == 0.0
    assert _find_matches_sync([("p", poly, 0.5)], [("k", kalshi, 0.5)], 0.8) == []


@pytest.mark.parametrize(
    "poly,kalshi",
    [
        (
            "Will there be at least 5000 measles cases in the U.S. in 2026?",
            "Will there be more than 5000 measles cases in 2026? — Above 5000",
        ),
        (
            "Will there be at least 7000 measles cases in the U.S. in 2026?",
            "Will there be more than 7000 measles cases in 2026?",
        ),
        ("Bitcoin 100000 or more on Friday?", "Bitcoin above 100000 on Friday?"),
    ],
)
def test_inclusive_vs_exclusive_boundary_rejected(poly, kalshi):
    assert score(poly, kalshi) == 0.0


@pytest.mark.parametrize(
    "poly,kalshi",
    [
        ("Darren Waller: 2+ Touchdowns", "Darren Waller: 2+ touchdowns"),
        ("Sander Berge: 1+ assists", "Sander Berge: 1+ assists?"),
        ("Marcus Thuram: 2+ goals", "Marcus Thuram: 2+ goals"),
        (
            "Will there be more than 5000 measles cases in the U.S. in 2026?",
            "Will there be more than 5000 measles cases in 2026? — Above 5000",
        ),
        (
            "Will Kristi Noem leave the Trump administration before 2027?",
            "Will Kristi Noem leave the Trump administration before Jan 1, 2027?",
        ),
    ],
)
def test_same_qualifier_still_matches(poly, kalshi):
    assert score(poly, kalshi) >= 0.8
