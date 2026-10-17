"""Polymarket token/price orientation must come from `outcomes`, not position.

Gamma markets are not all Yes/No: ~1.5% of a live sample were
["Up", "Down"] or two named teams/films. For those, clobTokenIds[0] and
outcomePrices[0] are not a YES leg, so storing them as yes_token_id /
yes_price would make P1 trade (and price) the wrong thing. The gate is
deny-by-default: only an exact ["Yes", "No"] binary with exactly two CLOB
tokens is stored, and a market that fails the gate has any previously
stored token ids cleared so BookResolver can no longer route to it.
"""

from datetime import UTC, datetime

import pytest

from core.ingestor.store import _POLY_KEEP_KEYS, _yes_first, store_markets


def _gamma(cid: str, outcomes: object = '["Yes", "No"]', tokens: object = None):
    m: dict = {
        "conditionId": cid,
        "question": f"Market {cid}?",
        "clobTokenIds": '["111", "222"]' if tokens is None else tokens,
        "outcomePrices": '["0.4", "0.6"]',
    }
    if outcomes is not None:
        m["outcomes"] = outcomes
    return m


async def _stored(db) -> list[tuple]:
    cur = await db.execute(
        "SELECT platform_id, yes_token_id, no_token_id FROM markets "
        "WHERE platform = 'polymarket' ORDER BY platform_id"
    )
    return [tuple(r) for r in await cur.fetchall()]


def test_fetch_keeps_outcomes_field():
    assert "outcomes" in _POLY_KEEP_KEYS


@pytest.mark.parametrize(
    "outcomes,ok",
    [
        ('["Yes", "No"]', True),
        (["yes", "no"], True),
        ('[" YES ", "No "]', True),
        ('["Up", "Down"]', False),
        ('["No", "Yes"]', False),
        ('["Yes", "Maybe"]', False),
        ('["Yes", "No", "Other"]', False),
        ('["Yes"]', False),
        (None, False),
        ("not json", False),
        (42, False),
    ],
)
def test_yes_first_requires_exact_yes_no_binary(outcomes, ok):
    assert _yes_first(outcomes) is ok


async def test_only_exact_yes_no_markets_are_stored(db):
    markets = [
        _gamma("0xyes"),
        _gamma("0xyeslower", '["yes", "no"]'),
        _gamma("0xlist", ["Yes", "No"]),
        _gamma("0xupdown", '["Up", "Down"]'),
        _gamma("0xteams", '["Chattogram Challengers", "Durbar Rajshahi"]'),
        _gamma("0xnoyes", '["No", "Yes"]'),
        _gamma("0xmissing", None),  # deny-by-default
        _gamma("0xthree", '["Yes", "No", "Void"]'),
    ]
    await store_markets(db, markets, [])
    assert await _stored(db) == [
        ("0xlist", "111", "222"),
        ("0xyes", "111", "222"),
        ("0xyeslower", "111", "222"),
    ]


@pytest.mark.parametrize("tokens", ['["111"]', '["1", "2", "3"]', "garbage", "[]"])
async def test_token_list_must_have_exactly_two_entries(db, tokens):
    await store_markets(db, [_gamma("0xbadtok", tokens=tokens)], [])
    assert await _stored(db) == []


async def test_gate_clears_tokens_of_previously_stored_rows(db):
    # A row stored by the old positional code for an Up/Down market.
    now = datetime.now(UTC).isoformat()
    await db.execute(
        """INSERT INTO markets
           (id, platform, platform_id, title, yes_token_id, no_token_id,
            status, created_at, updated_at)
           VALUES ('poly_0xupdown', 'polymarket', '0xupdown', 'old',
                   '111', '222', 'open', ?, ?)""",
        (now, now),
    )
    await db.commit()

    await store_markets(db, [_gamma("0xupdown", '["Up", "Down"]')], [])

    # Row kept (FK history), tokens cleared so no order can be routed.
    assert await _stored(db) == [("0xupdown", None, None)]
