"""
Market fetching and storage functions.

Provides fetch_polymarket_markets, fetch_kalshi_markets, and store_markets
for pulling market data from exchanges and persisting to the database.
"""

import json as _json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite
import httpx

logger = logging.getLogger(__name__)


async def fetch_polymarket_markets() -> list[dict]:
    """Fetch ALL active markets from Polymarket Gamma API.

    Paginates through every page (100 per page) until exhausted.
    Typically ~5k-30k active markets.
    """
    all_markets = []
    offset = 0
    page_size = 100  # Gamma API max per page

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={
                        "limit": page_size,
                        "offset": offset,
                        "active": "true",
                        "closed": "false",
                    },
                )
                if resp.status_code != 200:
                    logger.error("Polymarket API error: HTTP %d", resp.status_code)
                    break

                page = resp.json()
                if not page:
                    break

                all_markets.extend(page)
                offset += page_size

                # Progress logging every 1000 markets
                if len(all_markets) % 1000 < page_size:
                    logger.info(
                        "  Polymarket: %d markets fetched so far...",
                        len(all_markets),
                    )

                if len(page) < page_size:
                    break  # Last page

        logger.info("Fetched %d total markets from Polymarket", len(all_markets))
    except Exception as e:
        logger.error("Failed to fetch Polymarket markets: %s", e)

    return all_markets


async def fetch_kalshi_markets(
    api_key: str, rsa_key_path: str, api_base: str
) -> list[dict]:
    """Fetch ALL open markets from Kalshi API.

    Paginates through every page (200 per page, cursor-based) until exhausted.
    Filters out multivariate/parlay combo markets.
    Typically ~5k-30k active markets.
    """
    import base64

    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

    try:
        key_path = Path(rsa_key_path).expanduser()
        loaded_key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
        if not isinstance(loaded_key, RSAPrivateKey):
            logger.error(
                "Kalshi key is not an RSA private key (got %s)",
                type(loaded_key).__name__,
            )
            return []
        private_key: RSAPrivateKey = loaded_key
    except Exception as e:
        logger.error("Failed to load Kalshi RSA key: %s", e)
        return []

    def sign(method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        msg = (ts + method.upper() + path).encode("utf-8")
        sig = private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": api_key,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    all_markets = []
    seen_tickers = set()
    cursor = None
    seen_cursors: set[str] = set()
    MAX_MARKETS = 50_000  # Safety cap — Kalshi has ~30k open markets

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            page_num = 0
            while True:
                page_num += 1
                path = "/trade-api/v2/markets"
                headers = sign("GET", path)
                params: dict[str, str | int] = {
                    "limit": 1000,
                    "status": "open",
                    "mve_filter": "exclude",
                }
                if cursor:
                    params["cursor"] = cursor

                resp = await client.get(
                    f"{api_base}/markets",
                    headers=headers,
                    params=params,
                )
                if resp.status_code != 200:
                    logger.error(
                        "Kalshi API error: HTTP %d — %s",
                        resp.status_code,
                        resp.text[:200],
                    )
                    break

                data = resp.json()
                page = data.get("markets", [])
                if not page:
                    logger.info("  Kalshi: empty page on page %d, stopping", page_num)
                    break

                # Deduplicate: only add markets we haven't seen
                new_count = 0
                for m in page:
                    ticker = m.get("ticker", "")
                    if ticker and ticker not in seen_tickers:
                        seen_tickers.add(ticker)
                        all_markets.append(m)
                        new_count += 1

                # If we got zero new markets, the cursor has cycled
                if new_count == 0:
                    logger.info(
                        "  Kalshi: cursor cycled (0 new in page %d of %d), stopping",
                        page_num,
                        len(page),
                    )
                    break

                new_cursor = data.get("cursor", "")

                # Progress logging every 5000 markets
                if len(all_markets) % 5000 < 1000:
                    logger.info(
                        "  Kalshi: %d unique markets fetched so far (page %d)...",
                        len(all_markets),
                        page_num,
                    )

                # Stop conditions:
                # 1. No cursor returned or empty cursor → end of results
                if not new_cursor:
                    logger.info(
                        "  Kalshi: no cursor returned, stopping at %d markets",
                        len(all_markets),
                    )
                    break

                # 2. Page smaller than limit → last page
                if len(page) < 1000:
                    logger.info(
                        "  Kalshi: partial page (%d < 1000), stopping at %d markets",
                        len(page),
                        len(all_markets),
                    )
                    break

                # 3. Cursor already seen → cycle detected
                if new_cursor in seen_cursors:
                    logger.info(
                        "  Kalshi: cursor cycle detected (seen before), stopping at %d markets",
                        len(all_markets),
                    )
                    break

                # 4. Safety cap
                if len(all_markets) >= MAX_MARKETS:
                    logger.warning(
                        "  Kalshi: hit safety cap of %d markets, stopping", MAX_MARKETS
                    )
                    break

                seen_cursors.add(new_cursor)
                cursor = new_cursor

        # Post-filter: remove parlays/multivariate combos
        filtered = []
        parlays_mve = 0
        parlays_title = 0
        for m in all_markets:
            # Primary filter: mve_collection_ticker indicates a parlay/combo market
            mve = m.get("mve_collection_ticker")
            if mve:
                parlays_mve += 1
                continue

            # Secondary filter: title pattern for parlays missed by mve field
            title = m.get("title", "")
            if title.lower().startswith(("yes ", "no ")) and "," in title:
                parlays_title += 1
                continue

            filtered.append(m)

        logger.info(
            "Fetched %d total Kalshi markets → %d single-event "
            "(%d mve parlays, %d title-pattern parlays filtered)",
            len(all_markets),
            len(filtered),
            parlays_mve,
            parlays_title,
        )
        return filtered

    except Exception as e:
        logger.error("Failed to fetch Kalshi markets: %s", e)
        return []


async def store_markets(
    db: aiosqlite.Connection, poly_markets: list, kalshi_markets: list
):
    """Write fetched markets and prices to DB using batch inserts."""
    now = datetime.now(timezone.utc).isoformat()
    t_start = time.time()

    # ── Prepare Polymarket rows in-memory ────────────────────────────────
    poly_market_rows = []
    poly_price_rows = []

    for m in poly_markets:
        # Gamma API returns camelCase `conditionId` (66-char hex). The old
        # snake_case `condition_id` fallback is kept for legacy/mocked inputs,
        # and `id` (numeric gamma id) is the last resort — but that one cannot
        # be used for CLOB live trading or `/markets/{conditionId}` lookups.
        condition_id = (
            m.get("conditionId") or m.get("condition_id") or m.get("id") or ""
        )
        if not condition_id:
            continue
        condition_id = str(condition_id)
        title = m.get("question", m.get("title", ""))
        if not title:
            continue

        # clobTokenIds is a JSON-encoded string like
        # '["<yes_token_id>", "<no_token_id>"]'. Order is [YES, NO] per CLOB
        # convention. Accept already-parsed list form for test fixtures.
        yes_token_id: str | None = None
        no_token_id: str | None = None
        raw_tokens = m.get("clobTokenIds") or m.get("clob_token_ids")
        if raw_tokens:
            try:
                if isinstance(raw_tokens, str):
                    parsed = _json.loads(raw_tokens)
                else:
                    parsed = raw_tokens
                if isinstance(parsed, list) and len(parsed) >= 2:
                    yes_token_id = str(parsed[0]) if parsed[0] else None
                    no_token_id = str(parsed[1]) if parsed[1] else None
            except Exception:
                pass

        price = None
        tokens = m.get("tokens", [])
        if tokens and len(tokens) > 0:
            try:
                price = float(tokens[0].get("price", 0))
            except (ValueError, TypeError):
                pass
        if not price:
            try:
                op = m.get("outcomePrices", "")
                if op and isinstance(op, str):
                    prices = _json.loads(op)
                    price = float(prices[0]) if prices else None
            except Exception:
                pass

        if not price or price <= 0.01 or price >= 0.99:
            continue

        # Use a stable prefix over the conditionId so the internal market_id
        # maps 1:1 to the on-chain market. Hex conditionIds are 66 chars; we
        # keep the whole value so downstream lookups (CLOB `/markets/{id}`)
        # can reconstruct it from platform_id.
        mid = f"poly_{condition_id}"
        poly_market_rows.append(
            (mid, condition_id, title[:200], yes_token_id, no_token_id, now, now)
        )
        poly_price_rows.append(
            (
                mid,
                round(price, 4),
                round(1 - price, 4),
                0.02,
                float(m.get("volume", 10000)),
                now,
            )
        )

    # ── Prepare Kalshi rows in-memory ────────────────────────────────────
    kalshi_market_rows = []
    kalshi_price_rows = []
    kalshi_skipped = {"no_ticker": 0, "no_price": 0, "extreme_price": 0}

    if kalshi_markets:
        sample = kalshi_markets[0]
        logger.info("Kalshi sample fields: %s", sorted(sample.keys()))

    for m in kalshi_markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "")
        if not ticker or not title:
            kalshi_skipped["no_ticker"] += 1
            continue

        price = None
        bid_val = None
        ask_val = None

        # Method 1: _dollars string fields (already in 0-1 scale)
        yes_bid_d = m.get("yes_bid_dollars")
        yes_ask_d = m.get("yes_ask_dollars")
        last_price_d = m.get("last_price_dollars")

        if yes_bid_d is not None and yes_ask_d is not None:
            try:
                bid_val = float(yes_bid_d)
                ask_val = float(yes_ask_d)
                if bid_val > 0 and ask_val > 0:
                    price = (bid_val + ask_val) / 2.0
            except (ValueError, TypeError):
                pass

        if price is None and last_price_d is not None:
            try:
                lp = float(last_price_d)
                if lp > 0:
                    price = lp
            except (ValueError, TypeError):
                pass

        # Method 2: integer cent fields (need / 100)
        if price is None:
            yes_bid_c = m.get("yes_bid")
            yes_ask_c = m.get("yes_ask")
            last_price_c = m.get("last_price")

            if yes_bid_c is not None and yes_ask_c is not None:
                try:
                    bid_c = float(yes_bid_c)
                    ask_c = float(yes_ask_c)
                    if bid_c > 0 and ask_c > 0:
                        bid_val = bid_c / 100.0
                        ask_val = ask_c / 100.0
                        price = (bid_val + ask_val) / 2.0
                except (ValueError, TypeError):
                    pass

            if price is None and last_price_c is not None:
                try:
                    lp = float(last_price_c)
                    if lp > 0:
                        price = lp / 100.0 if lp > 1 else lp
                except (ValueError, TypeError):
                    pass

        # Method 3: fallback fields
        if price is None:
            for fallback_field in ["open_price_dollars", "close_price_dollars"]:
                val = m.get(fallback_field)
                if val is not None:
                    try:
                        fv = float(val)
                        if fv > 0:
                            price = fv
                            break
                    except (ValueError, TypeError):
                        pass

        if price is None or price <= 0:
            kalshi_skipped["no_price"] += 1
            continue

        if price <= 0.01 or price >= 0.99:
            kalshi_skipped["extreme_price"] += 1
            continue

        spread_val = 0.02
        if bid_val is not None and ask_val is not None and bid_val > 0 and ask_val > 0:
            spread_val = round(abs(ask_val - bid_val), 4)

        mid = f"kal_{ticker}"
        kalshi_market_rows.append((mid, ticker, title[:200], now, now))
        kalshi_price_rows.append(
            (
                mid,
                round(price, 4),
                round(1 - price, 4),
                spread_val,
                float(
                    m.get("liquidity_dollars", 0)
                    or m.get("volume", 0)
                    or m.get("volume_24h", 0)
                    or 10000
                ),
                now,
            )
        )

    # ── Batch write to DB ────────────────────────────────────────────────
    try:
        await db.executemany(
            """INSERT OR REPLACE INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                status, created_at, updated_at)
               VALUES (?, 'polymarket', ?, ?, ?, ?, 'open', ?, ?)""",
            poly_market_rows,
        )
        await db.executemany(
            """INSERT INTO market_prices
               (market_id, yes_price, no_price, spread, liquidity, polled_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            poly_price_rows,
        )
        await db.executemany(
            """INSERT OR REPLACE INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES (?, 'kalshi', ?, ?, 'open', ?, ?)""",
            kalshi_market_rows,
        )
        await db.executemany(
            """INSERT INTO market_prices
               (market_id, yes_price, no_price, spread, liquidity, polled_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            kalshi_price_rows,
        )
        await db.commit()
    except Exception as e:
        logger.error("Batch DB insert error: %s", e)
        await db.commit()

    stored = len(poly_market_rows) + len(kalshi_market_rows)
    elapsed = time.time() - t_start

    logger.info(
        "Stored %d markets (%d PM + %d KA) in %.1fs. Kalshi skipped: %s",
        stored,
        len(poly_market_rows),
        len(kalshi_market_rows),
        elapsed,
        kalshi_skipped,
    )
    return stored
