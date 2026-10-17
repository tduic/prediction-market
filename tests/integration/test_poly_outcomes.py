"""Polymarket token/price orientation must come from `outcomes`, not position.

Gamma markets are not all Yes/No: ~1.5% of a live sample were
["Up", "Down"] or two named teams/films. For those, clobTokenIds[0] and
outcomePrices[0] are not a YES leg, so storing them as yes_token_id /
yes_price would make P1 trade (and price) the wrong thing.
"""

from core.ingestor.store import _POLY_KEEP_KEYS, store_markets


def _gamma(cid: str, outcomes: str | None) -> dict:
    m = {
        "conditionId": cid,
        "question": f"Market {cid}?",
        "clobTokenIds": '["111", "222"]',
        "outcomePrices": '["0.4", "0.6"]',
    }
    if outcomes is not None:
        m["outcomes"] = outcomes
    return m


def test_fetch_keeps_outcomes_field():
    assert "outcomes" in _POLY_KEEP_KEYS


async def test_only_yes_first_markets_are_stored(db):
    markets = [
        _gamma("0xyes", '["Yes", "No"]'),
        _gamma("0xyeslower", '["yes", "no"]'),
        _gamma("0xupdown", '["Up", "Down"]'),
        _gamma("0xteams", '["Chattogram Challengers", "Durbar Rajshahi"]'),
        _gamma("0xnoyes", '["No", "Yes"]'),
    ]
    await store_markets(db, markets, [])
    cur = await db.execute(
        "SELECT platform_id, yes_token_id, no_token_id FROM markets "
        "WHERE platform = 'polymarket' ORDER BY platform_id"
    )
    rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [("0xyes", "111", "222"), ("0xyeslower", "111", "222")]


async def test_outcomes_as_list_is_accepted(db):
    await store_markets(db, [_gamma("0xlist", None) | {"outcomes": ["Yes", "No"]}], [])
    cur = await db.execute("SELECT COUNT(*) FROM markets WHERE platform_id='0xlist'")
    assert (await cur.fetchone())[0] == 1
