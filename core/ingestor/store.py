"""
Market fetching and storage functions.

Provides fetch_polymarket_markets, fetch_kalshi_markets, and store_markets
for pulling market data from exchanges and persisting to the database.
"""

import asyncio
import json as _json
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import httpx

logger = logging.getLogger(__name__)


class MarketFetchError(RuntimeError):
    """A market listing could not be fetched completely.

    Raised instead of returning a partial list: callers store and match on
    whatever is returned as if it were the whole exchange, so a silently
    truncated list (e.g. only the oldest ~2,100 Polymarket markets) is worse
    than a visible failure that leaves the previous DB snapshot in place.
    """


# Indirection so tests can skip real backoff sleeps.
_sleep = asyncio.sleep

_RETRY_ATTEMPTS = 6
_RETRY_BASE_DELAY_S = 1.0
_RETRY_MAX_DELAY_S = 60.0


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse a numeric Retry-After header (seconds); None if absent/invalid."""
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, min(float(raw), _RETRY_MAX_DELAY_S))
    except ValueError:
        return None


async def _get_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    what: str,
    params: dict,
    headers_fn: Callable[[], dict] | None = None,
) -> httpx.Response:
    """GET with retry + exponential backoff on 429 / 5xx / transport errors.

    Honors ``Retry-After`` when present. Non-retryable statuses (other 4xx)
    and exhausted retries raise MarketFetchError. ``headers_fn`` is called per
    attempt so signed requests get a fresh timestamp.
    """
    last_err = ""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        retry_after: float | None = None
        try:
            resp = await client.get(
                url,
                params=params,
                headers=headers_fn() if headers_fn else None,
            )
        except httpx.TransportError as e:
            last_err = f"{type(e).__name__}: {e}"
        else:
            if resp.status_code == 200:
                return resp
            last_err = f"HTTP {resp.status_code} {resp.text[:200]}"
            if resp.status_code != 429 and resp.status_code < 500:
                raise MarketFetchError(f"{what}: {last_err}")
            retry_after = _retry_after_seconds(resp)
        if attempt == _RETRY_ATTEMPTS:
            break
        delay = (
            retry_after
            if retry_after is not None
            else min(_RETRY_BASE_DELAY_S * 2 ** (attempt - 1), _RETRY_MAX_DELAY_S)
        )
        logger.warning(
            "%s: %s — retry %d/%d in %.1fs",
            what,
            last_err,
            attempt,
            _RETRY_ATTEMPTS - 1,
            delay,
        )
        await _sleep(delay)
    raise MarketFetchError(
        f"{what}: giving up after {_RETRY_ATTEMPTS} attempts ({last_err})"
    )


GAMMA_KEYSET_URL = "https://gamma-api.polymarket.com/markets/keyset"

# Fields store_markets reads. Gamma market objects are ~6 KB each (events,
# descriptions, ...); with ~300k open markets the full objects would need
# gigabytes, so each record is trimmed to these keys while paginating.
_POLY_KEEP_KEYS = (
    "id",
    "conditionId",
    "condition_id",
    "question",
    "title",
    "slug",
    "groupItemTitle",
    "clobTokenIds",
    "clob_token_ids",
    "tokens",
    "outcomePrices",
    "bestBid",
    "bestAsk",
    "volume",
    "liquidity",
    "endDate",
    "negRisk",
)
_POLY_MAX_PAGES = 20_000  # 2M markets — beyond this assume a cursor loop


async def fetch_polymarket_markets(
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Fetch ALL active, open markets from the Polymarket Gamma API.

    Uses ``/markets/keyset`` cursor pagination (``after_cursor`` /
    ``next_cursor``). The offset-paginated ``/markets`` endpoint rejects
    offsets past 2000 with HTTP 422, which used to end pagination early and
    return only the oldest ~2,100 markets as if that were everything.

    Raises MarketFetchError if any page cannot be fetched after retries —
    never returns a partial list.
    """
    if client is None:
        async with httpx.AsyncClient(timeout=30) as owned:
            return await fetch_polymarket_markets(owned)

    all_markets: list[dict] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    page_num = 0
    while True:
        page_num += 1
        if page_num > _POLY_MAX_PAGES:
            raise MarketFetchError(
                f"Polymarket keyset: exceeded {_POLY_MAX_PAGES} pages "
                f"({len(all_markets)} markets) — refusing a possibly looping crawl"
            )
        params: dict[str, str | int] = {
            "limit": 100,  # server cap
            "active": "true",
            "closed": "false",
        }
        if cursor:
            params["after_cursor"] = cursor
        resp = await _get_with_retry(
            client,
            GAMMA_KEYSET_URL,
            what=f"Polymarket keyset page {page_num}",
            params=params,
        )
        try:
            data = resp.json()
            page = data["markets"]
        except (ValueError, KeyError, TypeError) as e:
            raise MarketFetchError(
                f"Polymarket keyset page {page_num}: unexpected body ({e}): "
                f"{resp.text[:200]}"
            ) from e

        all_markets.extend(
            {k: m[k] for k in _POLY_KEEP_KEYS if k in m}
            for m in page
            if isinstance(m, dict)
        )

        if page_num % 50 == 0:
            logger.info(
                "  Polymarket: %d markets fetched so far (page %d)...",
                len(all_markets),
                page_num,
            )

        next_cursor = data.get("next_cursor")
        if not next_cursor or not page:
            break
        if next_cursor in seen_cursors:
            raise MarketFetchError(
                f"Polymarket keyset: cursor repeated on page {page_num}"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    logger.info("Fetched %d total markets from Polymarket", len(all_markets))
    return all_markets


async def fetch_kalshi_markets(
    api_key: str,
    rsa_key_path: str,
    api_base: str,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Fetch ALL open markets from Kalshi API.

    Paginates through every page (1000 per page, cursor-based) until exhausted.
    Filters out multivariate/parlay combo markets.
    Typically ~5k-30k active markets.

    429 / 5xx / transport errors are retried with exponential backoff
    (honoring Retry-After). If a page still cannot be fetched, raises
    MarketFetchError rather than returning the pages fetched so far.
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
    MAX_MARKETS = 200_000  # Safety cap — Kalshi has ~20-30k open markets

    owned_client = client is None
    http = client if client is not None else httpx.AsyncClient(timeout=30)
    try:
        page_num = 0
        while True:
            page_num += 1
            path = "/trade-api/v2/markets"
            params: dict[str, str | int] = {
                "limit": 1000,
                "status": "open",
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor

            resp = await _get_with_retry(
                http,
                f"{api_base}/markets",
                what=f"Kalshi markets page {page_num}",
                params=params,
                headers_fn=lambda: sign("GET", path),
            )
            try:
                data = resp.json()
            except ValueError as e:
                raise MarketFetchError(
                    f"Kalshi markets page {page_num}: non-JSON body"
                ) from e
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

            # 4. Safety cap — refuse rather than return a truncated list
            if len(all_markets) >= MAX_MARKETS:
                raise MarketFetchError(
                    f"Kalshi: hit safety cap of {MAX_MARKETS} markets"
                )

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

    except MarketFetchError as e:
        logger.error("Kalshi market fetch FAILED (no partial result used): %s", e)
        raise
    except Exception as e:
        logger.error("Kalshi market fetch FAILED (no partial result used): %s", e)
        raise MarketFetchError(f"Kalshi: {type(e).__name__}: {e}") from e
    finally:
        if owned_client:
            await http.aclose()


_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_CAP_WORD = r"[A-Z0-9][\w.'&-]*"
# "... the Mainz 05 vs Wolfsburg professional ..." -> "Mainz 05 vs Wolfsburg":
# runs of capitalised words on each side of "vs".
_MATCHUP_RE = re.compile(rf"\b((?:{_CAP_WORD} )+?)vs\.? ((?:{_CAP_WORD}(?: |$))+)")
_SENTENCE_START_RE = re.compile(r"^(?:If|The|In|Will)\s+")


def _title_tokens(text: str) -> set[str]:
    return set(_TITLE_TOKEN_RE.findall(text.lower()))


def _kalshi_matchup(rules: str) -> str | None:
    m = _MATCHUP_RE.search(rules or "")
    if not m:
        return None
    home = _SENTENCE_START_RE.sub("", m.group(1).strip())
    away = m.group(2).strip()
    if not home or not away:
        return None
    return f"{home} vs {away}"


def kalshi_match_title(m: dict) -> str:
    """Build an unambiguous title for a Kalshi market.

    Kalshi ``title`` is shared by many markets: every strike of an event
    ("HYPE price on Oct 17, 2026?" x 600) and the same prop across events
    ("Will over 2.5 goals be scored?" x 245 games). The strike is in
    ``yes_sub_title`` and the matchup only in ``rules_primary``, so append
    whichever of those adds words the title lacks:

        "HYPE price on Oct 17, 2026? — $43.7499 or below"
        "Will over 2.5 goals be scored? (Mainz 05 vs Wolfsburg)"

    This is what gets stored in ``markets.title`` and matched on.
    """
    title = (m.get("title") or "").strip()
    sub = (m.get("yes_sub_title") or "").strip()
    if sub and not _title_tokens(sub) <= _title_tokens(title):
        title = f"{title} — {sub}"
    matchup = _kalshi_matchup(m.get("rules_primary") or "")
    if matchup and not _title_tokens(matchup) <= _title_tokens(title):
        title = f"{title} ({matchup})"
    return title


# Widest Kalshi YES bid/ask spread whose mid is still stored as the price.
KALSHI_MAX_MID_SPREAD = 0.20


def _opt_float(m: dict, *keys: str) -> float | None:
    """First of ``keys`` present in ``m`` that parses as a float, else None."""
    for key in keys:
        raw = m.get(key)
        if raw is None or raw == "":
            continue
        try:
            return float(raw)
        except (TypeError, ValueError):
            continue
    return None


async def store_markets(
    db: aiosqlite.Connection, poly_markets: list, kalshi_markets: list
):
    """Write fetched markets and prices to DB using batch inserts."""
    now = datetime.now(UTC).isoformat()
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
            except Exception as e:
                logger.debug("clobTokenIds parse failed for %r: %s", raw_tokens, e)

        price = None
        last_price_no: float | None = None
        tokens = m.get("tokens", [])
        if tokens and len(tokens) > 0:
            try:
                price = float(tokens[0].get("price", 0))
            except (ValueError, TypeError):
                pass
        if tokens and len(tokens) >= 2:
            try:
                last_price_no = float(tokens[1].get("price", 0) or 0) or None
            except (ValueError, TypeError):
                last_price_no = None
        if not price:
            try:
                op = m.get("outcomePrices", "")
                if op and isinstance(op, str):
                    prices = _json.loads(op)
                    price = float(prices[0]) if prices else None
            except Exception as e:
                logger.debug("outcomePrices parse failed for %r: %s", op, e)

        if not price or price <= 0.01 or price >= 0.99:
            continue

        # Use a stable prefix over the conditionId so the internal market_id
        # maps 1:1 to the on-chain market. Hex conditionIds are 66 chars; we
        # keep the whole value so downstream lookups (CLOB `/markets/{id}`)
        # can reconstruct it from platform_id.
        mid = f"poly_{condition_id}"
        poly_market_rows.append(
            (
                mid,
                condition_id,
                title[:200],
                yes_token_id,
                no_token_id,
                last_price_no,
                now,
                now,
            )
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
    kalshi_skipped = {
        "no_ticker": 0,
        "no_price": 0,
        "extreme_price": 0,
        "wide_spread": 0,
    }

    if kalshi_markets:
        sample = kalshi_markets[0]
        logger.info("Kalshi sample fields: %s", sorted(sample.keys()))

    for m in kalshi_markets:
        ticker = m.get("ticker", "")
        title = kalshi_match_title(m) if m.get("title") else ""
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
                    if ask_val - bid_val > KALSHI_MAX_MID_SPREAD + 1e-9:
                        # Placeholder book (e.g. 0.02 / 0.98): its mid is
                        # not a price anyone can trade, and neither is a
                        # stale last trade. Skip rather than invent one.
                        kalshi_skipped["wide_spread"] += 1
                        continue
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
                # Current listings carry no liquidity field: NULL, never a
                # made-up number. Sizes come as *_fp decimal strings.
                _opt_float(m, "liquidity_dollars", "liquidity"),
                _opt_float(m, "volume_24h_fp", "volume_24h"),
                _opt_float(m, "open_interest_fp", "open_interest"),
                now,
            )
        )

    # ── Batch write to DB ────────────────────────────────────────────────
    try:
        await db.executemany(
            # Upsert, never INSERT OR REPLACE: REPLACE deletes the old row,
            # and with foreign keys on that cascades through every child
            # table (orders, signals, positions, …). status and created_at
            # are kept so a refresh can't reopen a resolved market.
            """INSERT INTO markets
               (id, platform, platform_id, title, yes_token_id, no_token_id,
                last_price_no, status, created_at, updated_at)
               VALUES (?, 'polymarket', ?, ?, ?, ?, ?, 'open', ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   title = excluded.title,
                   yes_token_id = COALESCE(excluded.yes_token_id, yes_token_id),
                   no_token_id = COALESCE(excluded.no_token_id, no_token_id),
                   last_price_no = COALESCE(excluded.last_price_no, last_price_no),
                   updated_at = excluded.updated_at
               ON CONFLICT DO NOTHING""",
            poly_market_rows,
        )
        # Prices only for markets that exist: a row skipped above (legacy id
        # clash) would otherwise fail the FK and abort the rest of the batch.
        await db.executemany(
            """INSERT INTO market_prices
               (market_id, yes_price, no_price, spread, liquidity, polled_at)
               SELECT ?, ?, ?, ?, ?, ?
               WHERE EXISTS (SELECT 1 FROM markets WHERE id = ?)""",
            [(*row, row[0]) for row in poly_price_rows],
        )
        await db.executemany(
            """INSERT INTO markets
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES (?, 'kalshi', ?, ?, 'open', ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   title = excluded.title,
                   updated_at = excluded.updated_at
               ON CONFLICT DO NOTHING""",
            kalshi_market_rows,
        )
        await db.executemany(
            """INSERT INTO market_prices
               (market_id, yes_price, no_price, spread, liquidity,
                volume_24h, open_interest, polled_at)
               SELECT ?, ?, ?, ?, ?, ?, ?, ?
               WHERE EXISTS (SELECT 1 FROM markets WHERE id = ?)""",
            [(*row, row[0]) for row in kalshi_price_rows],
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
