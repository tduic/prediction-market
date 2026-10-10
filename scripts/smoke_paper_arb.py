"""Paper-mode smoke test for the P1 arb path against live public venue data.

Needs no credentials and never places an order. It:

  1. picks one liquid Polymarket market and one liquid Kalshi market from the
     public APIs and seeds them into a throwaway SQLite DB (real migrations);
  2. checks live fee discovery and executable depth for both;
  3. runs ArbitrageEngine.initial_sweep in paper mode on the synthetic pair
     (the markets are unrelated — this exercises the pipeline, not a real
     arb) and prints what the engine decided.

Usage:
    python scripts/smoke_paper_arb.py

Exits non-zero if fee or depth discovery fails, or the engine raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone, UTC
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["EXECUTION_MODE"] = "paper"
# Never alert from a smoke run.
os.environ["ALERT_DISCORD_WEBHOOK_URL"] = ""
os.environ["ALERT_SLACK_WEBHOOK_URL"] = ""

import httpx

from core.engine.arb_engine import ArbitrageEngine
from core.engine.execution_control import get_halt
from core.storage.db import Database
from execution.enums import Side
from execution.market_data import LiveMarketData, set_market_data
from execution.models import OrderLeg

KALSHI = os.getenv(
    "KALSHI_PUBLIC_API_BASE", "https://api.elections.kalshi.com/trade-api/v2"
)
MIGRATIONS = str(Path(__file__).resolve().parent.parent / "core/storage/migrations")


async def _pick_markets(http: httpx.AsyncClient) -> tuple[dict, dict]:
    poly = (
        await http.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "limit": 50,
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
            },
        )
    ).json()
    poly_market = next(
        m
        for m in poly
        if m.get("conditionId")
        and m.get("clobTokenIds")
        and 0.1 < float(json.loads(m.get("outcomePrices") or '["0"]')[0]) < 0.9
    )
    kalshi = (
        await http.get(
            f"{KALSHI}/markets",
            params={"limit": 1000, "status": "open", "mve_filter": "exclude"},
        )
    ).json()["markets"]
    kalshi_market = next(
        m
        for m in kalshi
        if float(m.get("yes_bid_dollars") or 0) > 0.1
        and float(m.get("yes_ask_dollars") or 1) < 0.9
    )
    return poly_market, kalshi_market


async def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    async with httpx.AsyncClient(timeout=15) as http:
        poly_market, kalshi_market = await _pick_markets(http)

    tmp = tempfile.mkdtemp(prefix="smoke_arb_")
    database = Database(os.path.join(tmp, "smoke.db"), migrations_dir=MIGRATIONS)
    await database.init()
    db = database._conn
    if db is None:
        raise RuntimeError("database failed to open")
    now = datetime.now(UTC).isoformat()
    cid = poly_market["conditionId"]
    yes_tok, no_tok = json.loads(poly_market["clobTokenIds"])[:2]
    ticker = kalshi_market["ticker"]
    poly_id, kal_id = f"poly_{cid}", f"kal_{ticker}"
    await db.execute(
        "INSERT INTO markets (id, platform, platform_id, title, yes_token_id,"
        " no_token_id, status, created_at, updated_at)"
        " VALUES (?, 'polymarket', ?, ?, ?, ?, 'open', ?, ?)",
        (poly_id, cid, poly_market["question"][:200], yes_tok, no_tok, now, now),
    )
    await db.execute(
        "INSERT INTO markets (id, platform, platform_id, title, status,"
        " created_at, updated_at) VALUES (?, 'kalshi', ?, ?, 'open', ?, ?)",
        (kal_id, ticker, kalshi_market.get("title", ticker)[:200], now, now),
    )
    await db.commit()

    md = LiveMarketData()
    set_market_data(md)
    failures = 0

    print(f"Polymarket: {poly_market['question'][:70]}  ({cid[:12]}…)")
    print(f"Kalshi:     {kalshi_market.get('title', ticker)[:70]}  ({ticker})")
    for market_id in (poly_id, kal_id):
        params = await md.fee_params(db, market_id)
        print(f"  fee_params[{market_id[:20]}…] = {params}")
        failures += params is None

    poly_ask = float(json.loads(poly_market["outcomePrices"])[0])
    kal_bid = float(kalshi_market["yes_bid_dollars"])
    for leg in (
        OrderLeg(
            market_id=poly_id,
            platform="polymarket",
            side=Side.BUY,
            size=10,
            limit_price=poly_ask,
        ),
        OrderLeg(
            market_id=kal_id,
            platform="kalshi",
            side=Side.SELL,
            size=10,
            limit_price=kal_bid,
        ),
    ):
        depth = await md.executable_depth(db, leg)
        print(f"  depth {leg.platform} {leg.side.value}@{leg.limit_price} = {depth}")
        failures += depth is None

    match = {
        "poly_id": poly_id,
        "kalshi_id": kal_id,
        "poly_title": poly_market["question"],
        "kalshi_title": kalshi_market.get("title", ticker),
        "poly_price": poly_ask,
        "kalshi_price": kal_bid,
        "similarity": 1.0,
    }
    engine = ArbitrageEngine(db, [match], min_spread=0.0, execution_mode="paper")
    await engine.initial_sweep()
    stats = engine.stats()
    print(
        "Engine:",
        {
            k: stats[k]
            for k in (
                "trade_count",
                "skipped_fee_unknown",
                "skipped_unprofitable",
                "skipped_depth_unknown",
                "skipped_thin_book",
                "skipped_halted",
            )
        },
    )
    halt_state = await get_halt(db)
    print("Halt:", halt_state)
    cursor = await db.execute("SELECT platform, side, book, status FROM orders")
    print("Paper orders:", [tuple(r) for r in await cursor.fetchall()])
    await database.close()
    print("SMOKE", "FAILED" if failures else "OK")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
