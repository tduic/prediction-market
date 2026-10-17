"""Kalshi titles are not unique; matching must not pair on an ambiguous title.

In a live listing (2026-10, 19k open markets) only ~12k titles are distinct:
e.g. "Will over 2.5 goals be scored?" is the title of 245 different soccer
games, and "HYPE price on Oct 17, 2026?" of 600 strikes. The strike lives in
``yes_sub_title`` and the matchup only in ``rules_primary``. Storing the bare
title made the matcher pair a Polymarket market with whichever of those
identical Kalshi markets it saw first.

The Kalshi samples below are real market objects (trimmed to the relevant
fields; quotes tightened so the price filter keeps them).
"""

from core.ingestor.store import kalshi_match_title, store_markets
from core.matching.engine import _find_matches_sync, find_matches

QATAR = {
    "ticker": "KXQSTARSTOTAL-26OCT18ALGALR-3",
    "event_ticker": "KXQSTARSTOTAL-26OCT18ALGALR",
    "title": "Will over 2.5 goals be scored?",
    "yes_sub_title": "Over 2.5 goals scored",
    "floor_strike": 2.5,
    "rules_primary": (
        "If over 2.5 goals are scored in the Al Gharafa vs Al Rayyan "
        "professional Qatar Stars League soccer game originally scheduled for "
        "Oct 18, 2026 after 90 minutes plus stoppage time (does not include "
        "extra time or penalties), then the market resolves to Yes."
    ),
    "yes_bid_dollars": "0.4500",
    "yes_ask_dollars": "0.4700",
    "status": "active",
}
MAINZ = {
    "ticker": "KXBUNDESLIGAWTOTAL-26OCT18M05WOB-3",
    "event_ticker": "KXBUNDESLIGAWTOTAL-26OCT18M05WOB",
    "title": "Will over 2.5 goals be scored?",
    "yes_sub_title": "Over 2.5 goals scored",
    "floor_strike": 2.5,
    "rules_primary": (
        "If over 2.5 goals are scored in the Mainz 05 vs Wolfsburg "
        "professional German Women's Bundesliga soccer game originally "
        "scheduled for Oct 18, 2026 after 90 minutes plus stoppage time (does "
        "not include extra time or penalties), then the market resolves to Yes."
    ),
    "yes_bid_dollars": "0.4500",
    "yes_ask_dollars": "0.4700",
    "status": "active",
}
HYPE_LOW = {
    "ticker": "KXHYPE-26OCT1714-T43.75",
    "event_ticker": "KXHYPE-26OCT1714",
    "title": "HYPE price on Oct 17, 2026?",
    "yes_sub_title": "$43.7499 or below",
    "rules_primary": (
        "If the simple average of the sixty seconds of CF Benchmarks' "
        "HYPEUSD_RTI before 2 PM EDT is below $43.75 at 2 PM EDT on Oct 17, "
        "2026, then the market resolves to Yes."
    ),
    "yes_bid_dollars": "0.4500",
    "yes_ask_dollars": "0.4700",
    "status": "active",
}
HYPE_HIGH = {
    **HYPE_LOW,
    "ticker": "KXHYPE-26OCT1714-T118.2499",
    "yes_sub_title": "$118.25 or above",
}


class TestKalshiMatchTitle:
    def test_strike_from_subtitle_is_included(self):
        lo, hi = kalshi_match_title(HYPE_LOW), kalshi_match_title(HYPE_HIGH)
        assert lo != hi
        assert "43.7499" in lo and "118.25" in hi
        assert lo.startswith("HYPE price on Oct 17, 2026?")

    def test_matchup_from_rules_is_included(self):
        q = kalshi_match_title(QATAR, with_matchup=True)
        m = kalshi_match_title(MAINZ, with_matchup=True)
        assert q != m
        assert "Al Gharafa vs Al Rayyan" in q
        assert "Mainz 05 vs Wolfsburg" in m

    def test_redundant_subtitle_is_not_repeated(self):
        # "Over 2.5 goals scored" adds nothing to "Will over 2.5 goals be scored?"
        assert kalshi_match_title(QATAR, with_matchup=True).count("2.5") == 1

    def test_plain_title_unchanged_when_nothing_to_add(self):
        m = {"title": "Will it rain?", "yes_sub_title": "", "rules_primary": ""}
        assert kalshi_match_title(m) == "Will it rain?"


class TestStoreUsesUnambiguousTitle:
    async def test_identical_kalshi_titles_stored_distinctly(self, db):
        await store_markets(db, [], [QATAR, MAINZ, HYPE_LOW, HYPE_HIGH])
        cur = await db.execute(
            "SELECT title FROM markets WHERE platform = 'kalshi' ORDER BY id"
        )
        titles = [r[0] for r in await cur.fetchall()]
        assert len(titles) == 4
        assert len(set(titles)) == 4

    async def test_matchup_only_added_when_title_is_shared(self, db):
        # A title that is already unique (e.g. a player prop) stays as-is:
        # the matchup is not in the Polymarket title and would only lower
        # the similarity of a correct match (and widen candidate sets).
        prop = {
            **QATAR,
            "ticker": "KXSERIEAGOAL-26OCT18INTROM-THURAM2",
            "title": "Marcus Thuram: 2+ goals",
            "yes_sub_title": "Marcus Thuram: 2+",
            "rules_primary": (
                "If Marcus Thuram scores 2+ goals in the Inter vs Roma "
                "professional Serie A soccer game, then the market resolves to Yes."
            ),
        }
        await store_markets(db, [], [QATAR, MAINZ, prop])
        cur = await db.execute(
            "SELECT id, title FROM markets WHERE platform = 'kalshi'"
        )
        titles = {r[0]: r[1] for r in await cur.fetchall()}
        assert titles[f"kal_{prop['ticker']}"] == "Marcus Thuram: 2+ goals"
        assert "Al Gharafa vs Al Rayyan" in titles[f"kal_{QATAR['ticker']}"]


class TestAmbiguousTieIsRefused:
    def test_tie_between_identical_kalshi_titles_yields_no_match(self):
        # Before the fix this returned a match to kal_a (first seen).
        poly = [("poly_1", "Will over 2.5 goals be scored?", 0.46)]
        kalshi = [
            ("kal_a", "Will over 2.5 goals be scored?", 0.46),
            ("kal_b", "Will over 2.5 goals be scored?", 0.46),
        ]
        assert _find_matches_sync(poly, kalshi, threshold=0.80) == []

    def test_unique_best_candidate_still_matches(self):
        poly = [("poly_1", "Will over 2.5 goals be scored?", 0.46)]
        kalshi = [
            ("kal_a", "Will over 2.5 goals be scored?", 0.46),
            ("kal_b", "Will over 2.5 goals be scored in extra time?", 0.46),
        ]
        matches = _find_matches_sync(poly, kalshi, threshold=0.80)
        assert [m["kalshi_id"] for m in matches] == ["kal_a"]

    async def test_end_to_end_bare_title_poly_does_not_pair(self, db):
        poly = [
            {
                "conditionId": "0x" + "1" * 64,
                "question": "Will over 2.5 goals be scored?",
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.46", "0.54"]',
                "clobTokenIds": '["1", "2"]',
            }
        ]
        await store_markets(db, poly, [QATAR, MAINZ])
        assert await find_matches(db, threshold=0.80) == []
