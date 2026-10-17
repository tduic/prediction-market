"""store_markets: Kalshi mid / liquidity from the current listing schema.

Live /markets objects (2026-10) have no liquidity field at all; sizes come
as ``volume_fp`` / ``volume_24h_fp`` / ``open_interest_fp`` decimal strings.
The old code stored a fabricated liquidity of 10000 for every such market,
and stored the bid/ask mid even for placeholder books like 0.02 / 0.98 —
a "price" of 0.50 that nobody can trade and that the matcher and arb engine
then compare against Polymarket.
"""

import pytest

from core.ingestor.store import store_markets


def _kalshi(ticker: str, bid: str, ask: str, **extra) -> dict:
    # Shape of a real listing entry, trimmed.
    return {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "title": f"Test market {ticker}?",
        "yes_sub_title": "",
        "status": "active",
        "yes_bid_dollars": bid,
        "yes_ask_dollars": ask,
        "last_price_dollars": "0.0000",
        "volume_fp": "0.00",
        "volume_24h_fp": "0.00",
        "open_interest_fp": "0.00",
        **extra,
    }


async def _row(db, ticker):
    cur = await db.execute(
        "SELECT yes_price, spread, liquidity, volume_24h, open_interest "
        "FROM market_prices WHERE market_id = ?",
        (f"kal_{ticker}",),
    )
    return await cur.fetchone()


async def test_absent_liquidity_is_null_not_fake_10000(db):
    m = _kalshi(
        "KXA-1",
        "0.4500",
        "0.4700",
        volume_fp="900.00",
        volume_24h_fp="120.00",
        open_interest_fp="50.00",
    )
    await store_markets(db, [], [m])
    yes, spread, liq, vol24, oi = await _row(db, "KXA-1")
    assert yes == pytest.approx(0.46)
    assert spread == pytest.approx(0.02)
    assert liq is None
    assert vol24 == pytest.approx(120.0)
    assert oi == pytest.approx(50.0)


async def test_liquidity_dollars_used_when_present(db):
    m = _kalshi("KXB-1", "0.4500", "0.4700", liquidity_dollars="1500.50")
    await store_markets(db, [], [m])
    _, _, liq, _, _ = await _row(db, "KXB-1")
    assert liq == pytest.approx(1500.5)


async def test_placeholder_wide_book_mid_is_not_stored(db):
    # Real example: KXQSTARSTOTAL-26OCT18ALGALR-3 quoted 0.02 / 0.98.
    wide = _kalshi("KXQSTARSTOTAL-26OCT18ALGALR-3", "0.0200", "0.9800")
    ok = _kalshi("KXC-1", "0.3000", "0.3400")
    await store_markets(db, [], [wide, ok])
    assert await _row(db, "KXQSTARSTOTAL-26OCT18ALGALR-3") is None
    assert (await _row(db, "KXC-1"))[0] == pytest.approx(0.32)


async def test_spread_at_limit_is_kept(db):
    m = _kalshi("KXD-1", "0.4000", "0.6000")  # 0.20 wide: still usable
    await store_markets(db, [], [m])
    assert (await _row(db, "KXD-1"))[0] == pytest.approx(0.50)


async def test_one_sided_book_is_not_priced_from_stale_last_trade(db):
    # Review example: bid 0, ask 0.13, last trade 0.45 used to store 0.45.
    one_sided = _kalshi("KXE-1", "0.0000", "0.1300", last_price_dollars="0.4500")
    no_ask = _kalshi("KXF-1", "0.3000", "0.0000", last_price_dollars="0.3000")
    empty = _kalshi("KXG-1", "0.0000", "0.0000", last_price_dollars="0.5000")
    await store_markets(db, [], [one_sided, no_ask, empty])
    for t in ("KXE-1", "KXF-1", "KXG-1"):
        assert await _row(db, t) is None
    cur = await db.execute("SELECT COUNT(*) FROM markets WHERE platform='kalshi'")
    assert (await cur.fetchone())[0] == 0
