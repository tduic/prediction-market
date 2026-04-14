"""
Paper trading session: fetches real markets, detects real violations,
generates signals, executes through paper client, and reports analytics.

This runs the full pipeline end-to-end using live market data but without
placing any real orders. All data is written to the same DB tables as
live trading, so the dashboard and analytics work identically.

Usage:
    python scripts/paper_trading_session.py --refresh     # Fetch all markets, match, persist, trade once
    python scripts/paper_trading_session.py --once        # Use cached matches, trade once
    python scripts/paper_trading_session.py --stream      # Use cached matches + websocket prices, trade continuously
    python scripts/paper_trading_session.py --stream --dashboard  # Stream + analytics dashboard on :8000
"""

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import typing
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

print("[startup] Loading environment...", flush=True)
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# Setup path for imports
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

print("[startup] Importing dependencies...", flush=True)
import aiosqlite  # noqa: E402
import httpx  # noqa: E402

# Import directly from submodules to avoid core/__init__.py which eagerly
# loads EventBus, Database, etc. and can hang on first compilation.
print("[startup] Loading config...", flush=True)
from core.config import RiskControlConfig, get_config  # noqa: E402

print("[startup] Loading storage...", flush=True)
from core.storage.db import Database  # noqa: E402

print("[startup] Loading analytics...", flush=True)
from core.analytics import StrategyScorecard  # noqa: E402

print("[startup] Loading logging config...", flush=True)
from core.logging_config import configure_from_env  # noqa: E402

print("[startup] All imports complete.", flush=True)

logger = logging.getLogger(__name__)

# Lazy imports of risk/sizing/circuit-breaker to keep startup fast.
# These are only used inside ArbitrageEngine and ScheduledStrategyRunner.
from core.signals.risk import run_all_checks  # noqa: E402
from core.signals.sizing import (  # noqa: E402
    compute_kelly_fraction,
    compute_position_size,
)

# Circuit-breaker: if actual_pnl > size * this ratio the DB write is skipped.
# P1 false-positive trades book ~40% of size; 10% catches fakes without blocking
# any legitimate arb (typical real spread is 2–5%).
_PNL_SANITY_CAP_RATIO = 0.10

# Phase 1 matcher: semantic guard patterns.
# Matches O/U style terminology (over/under, O/U, over-under).
_OU_PAT = re.compile(r"\b(o/?u|over.?under|over|under)\b", re.IGNORECASE)
# Matches N+ / "at least N" / "N or more" threshold terminology.
_N_PLUS_PAT = re.compile(r"\d\+|at\s+least\s+\d|\d\s+or\s+more", re.IGNORECASE)
# Either threshold type (used for "does this title have threshold terminology?").
_THRESHOLD_ANY_PAT = re.compile(
    r"\b(o/?u|over.?under|over|under)\b|\d\+|at\s+least\s+\d|\d\s+or\s+more",
    re.IGNORECASE,
)


# ── Risk-check duck-typed signal proxy ───────────────────────────────────────
# run_all_checks() uses duck typing (signal: Any). These lightweight dataclasses
# satisfy its interface without requiring the full Signal dataclass from
# core/signals/generator.py (which pulls in Redis and other heavy deps).


from dataclasses import dataclass, field  # noqa: E402 (after dotenv setup)


@dataclass
class _RiskLeg:
    """Minimal leg proxy for run_all_checks duck-typing."""

    market_id: str
    limit_price: float
    size: float
    side: str = "BUY"


@dataclass
class _RiskSignal:
    """Minimal signal proxy for run_all_checks duck-typing."""

    legs: list
    edge: float
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    strategy: str = ""


@dataclass
class PairFireState:
    """Per-pair cooldown and re-arm state for ArbitrageEngine.

    After a pair fires, armed=False until the spread reverts below
    (min_spread - arb_rearm_hysteresis), preventing churn on oscillating
    prices. The pair also cannot fire again until arb_cooldown_s has elapsed.
    """

    last_fired_at: float
    armed: bool
    last_spread_seen_below: float | None = None


def _make_execution_clients(db, execution_mode: str):
    """
    Return (poly_client, kalshi_client) for the given execution mode.

    - "live"                → real PolymarketExecutionClient + KalshiExecutionClient
    - "paper"/"shadow"/any  → PaperExecutionClient (simulated fills, no real orders)

    Shadow mode intentionally uses paper clients: it generates signals and runs
    all risk checks but never submits real orders.
    """
    if execution_mode == "live":
        from execution.clients.kalshi import KalshiExecutionClient
        from execution.clients.polymarket import PolymarketExecutionClient

        poly_client = PolymarketExecutionClient(db)
        kalshi_client = KalshiExecutionClient(db)
        logger.info("Execution clients: LIVE (Polymarket + Kalshi)")
    else:
        from execution.clients.paper import PaperExecutionClient

        poly_client = PaperExecutionClient(db, platform_label="paper_polymarket")
        kalshi_client = PaperExecutionClient(db, platform_label="paper_kalshi")
        label = (
            "SHADOW (paper clients, no real orders)"
            if execution_mode == "shadow"
            else "PAPER (simulated)"
        )
        logger.info("Execution clients: %s", label)
    return poly_client, kalshi_client


def _make_single_execution_client(db, execution_mode: str, platform: str):
    """
    Return a single execution client for single-platform strategies.

    In live mode, returns the appropriate live client. In paper or shadow mode,
    returns a PaperExecutionClient (shadow uses paper clients by design).
    """
    if execution_mode == "live":
        if platform == "polymarket":
            from execution.clients.polymarket import PolymarketExecutionClient

            return PolymarketExecutionClient(db)
        else:
            from execution.clients.kalshi import KalshiExecutionClient

            return KalshiExecutionClient(db)
    else:
        from execution.clients.paper import PaperExecutionClient

        return PaperExecutionClient(db, platform_label=f"paper_{platform}")


# ── Strategy helpers ─────────────────────────────────────────────────────────

_MONTH_PAT = (
    r"(january|february|march|april|may|june|july|august|september|"
    r"october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)"
)
_STRIP_SUFFIX = re.compile(
    rf"\s+(?:{_MONTH_PAT}|20\d{{2}}|q[1-4]|h[1-2]|"
    r"\$?[\d,]+\.?\d*[km%]?(?:\s*[-–to]+\s*\$?[\d,]+\.?\d*[km%]?)?)\s*$",
    re.IGNORECASE,
)


def _p2_title_root(title: str) -> str:
    """Strip trailing date/value suffixes to find the shared event root.

    Used to group series markets (e.g. "Will GDP grow in Q1?" / "...Q2?")
    so we can detect over-sum inconsistency across the series.
    """
    t = title.lower().strip().rstrip("?.,!")
    # Iteratively strip up to 3 trailing tokens (e.g. "March 2025 $50k")
    for _ in range(3):
        new_t = _STRIP_SUFFIX.sub("", t)
        if new_t == t:
            break
        t = new_t.rstrip("?.,! ")
    return t


# ── Strategy assignment ──────────────────────────────────────────────────────

STRATEGIES = [
    "P1_cross_market_arb",
    "P2_structured_event",
    "P3_calibration_bias",
    "P4_liquidity_timing",
    "P5_information_latency",
]


def assign_strategy(spread: float, pair_type: str) -> str:
    """Assign a strategy based on violation characteristics."""
    if pair_type == "cross_platform":
        return "P1_cross_market_arb"
    elif spread > 0.10:
        return "P5_information_latency"
    elif spread >= 0.05:
        return "P3_calibration_bias"
    elif pair_type == "complement":
        return "P4_liquidity_timing"
    else:
        return "P2_structured_event"


# ── Market fetching ──────────────────────────────────────────────────────────


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

    try:
        key_path = Path(rsa_key_path).expanduser()
        private_key = serialization.load_pem_private_key(
            key_path.read_bytes(), password=None
        )
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
                params = {"limit": 1000, "status": "open", "mve_filter": "exclude"}
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


# ── Store markets in DB ──────────────────────────────────────────────────────


async def store_markets(
    db: aiosqlite.Connection, poly_markets: list, kalshi_markets: list
):
    """Write fetched markets and prices to DB using batch inserts."""
    import json as _json

    now = datetime.now(timezone.utc).isoformat()
    t_start = time.time()

    # ── Prepare Polymarket rows in-memory ────────────────────────────────
    poly_market_rows = []
    poly_price_rows = []

    for m in poly_markets:
        market_id = m.get("condition_id", m.get("id", ""))
        if not market_id:
            continue
        title = m.get("question", m.get("title", ""))
        if not title:
            continue

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

        mid = f"poly_{market_id[:20]}"
        poly_market_rows.append((mid, market_id, title[:200], now, now))
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
               (id, platform, platform_id, title, status, created_at, updated_at)
               VALUES (?, 'polymarket', ?, ?, 'open', ?, ?)""",
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


# ── Inverted-index matching engine ───────────────────────────────────────────
#
# At 30k × 30k markets, brute-force O(n²) is 900M comparisons — way too slow.
# Instead we use a blocking/candidate-generation strategy:
#
# 1. Normalize titles, extract tokens (lowercased, stop-filtered, synonym-expanded)
# 2. Build inverted index: token → set of market IDs (one index per platform)
# 3. For each Polymarket market, find Kalshi candidates that share ≥2 tokens
# 4. Score only those candidates using multi-signal similarity
# 5. Take 1:1 best matches above threshold
#
# This reduces to O(n × avg_candidates) which is typically O(n × 10-50).

SYNONYMS = {
    "fed": "federal reserve",
    "fomc": "federal reserve",
    "cpi": "consumer price index",
    "gdp": "gross domestic product",
    "nonfarm": "nonfarm payrolls",
    "payrolls": "nonfarm payrolls",
    "potus": "president",
    "scotus": "supreme court",
    "btc": "bitcoin",
    "eth": "ethereum",
    "sp500": "s&p 500",
    "s&p": "s&p 500",
    "gop": "republican",
    "dem": "democrat",
    "dems": "democrat",
    "govt": "government",
    "nba": "nba",
    "nfl": "nfl",
    "nhl": "nhl",
    "mlb": "mlb",
    "ncaa": "ncaa",
    "ufc": "ufc",
}

STOP_WORDS = {
    "the", "will", "yes", "no", "this", "that", "what", "when", "how",
    "for", "and", "are", "does", "which", "with", "than", "more", "less",
    "above", "below", "before", "after", "between", "about", "into", "over",
    "under", "from", "have", "has", "been", "would", "could", "should",
    "their", "there", "other", "each", "any", "all", "not", "was", "were",
    "but", "its", "who", "can", "may", "be", "by", "of", "on", "in", "at",
    "to", "is", "it", "an", "or", "if", "do", "so", "as", "up",
}  # fmt: skip


def normalize_title(title: str) -> str:
    """Normalize a market title for comparison."""
    text = title.lower().strip()
    for abbrev, full in SYNONYMS.items():
        text = re.sub(rf"\b{re.escape(abbrev)}\b", full, text)
    text = re.sub(r"[''`]", "'", text)
    text = re.sub(r"[^\w\s']", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def tokenize(text: str) -> set[str]:
    """Extract meaningful tokens from normalized text."""
    words = set(re.findall(r"\b\w{2,}\b", text))
    return words - STOP_WORDS


def extract_numbers(text: str) -> set[str]:
    """Extract numeric tokens (dates, thresholds, percentages)."""
    return set(re.findall(r"\b\d+\.?\d*\b", text))


def compute_match_score(
    norm_a: str,
    norm_b: str,
    tokens_a: set,
    tokens_b: set,
    raw_a: str = "",
    raw_b: str = "",
) -> float:
    """
    Score similarity between two pre-normalized, pre-tokenized market titles.

    Multi-signal approach:
    1. Jaccard on key terms (fast, main discriminator) — weight 0.50
    2. SequenceMatcher ratio (catches substring alignment) — weight 0.30
    3. Number consistency (dates, thresholds must match) — weight 0.20

    raw_a / raw_b: original (un-normalized) titles. When provided, used for
    Phase 1 semantic guards (O/U vs N+ mismatch detection) that require
    decimal-preserving number extraction on raw text.

    Returns 0-1.
    """
    if not tokens_a or not tokens_b:
        return 0.0

    # Phase 1 semantic guard: reject O/U vs N+ false positives.
    # normalize_title strips decimal points ("5.5" → "5 5"), so we must check
    # the raw titles before normalization destroys the distinction.
    if raw_a and raw_b:
        a_has_ou = bool(_OU_PAT.search(raw_a))
        b_has_ou = bool(_OU_PAT.search(raw_b))
        a_has_nplus = bool(_N_PLUS_PAT.search(raw_a))
        b_has_nplus = bool(_N_PLUS_PAT.search(raw_b))

        # Hard reject: O/U on one side and N+ on the other.
        if (a_has_ou and b_has_nplus) or (a_has_nplus and b_has_ou):
            nums_a = extract_numbers(raw_a)
            nums_b = extract_numbers(raw_b)
            # Only reject if the numeric sets are actually different.
            if nums_a != nums_b:
                return 0.0

        # Hard reject: both have threshold terminology but different numbers.
        a_has_thresh = bool(_THRESHOLD_ANY_PAT.search(raw_a))
        b_has_thresh = bool(_THRESHOLD_ANY_PAT.search(raw_b))
        if a_has_thresh and b_has_thresh:
            nums_a = extract_numbers(raw_a)
            nums_b = extract_numbers(raw_b)
            if nums_a and nums_b and nums_a != nums_b:
                return 0.0

    # Jaccard on tokens
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    jaccard = intersection / union if union > 0 else 0

    # Quick reject: if less than 2 shared tokens and jaccard < 0.15, skip expensive SequenceMatcher
    if intersection < 2 and jaccard < 0.15:
        return jaccard * 0.50

    # SequenceMatcher (expensive but accurate for substring alignment)
    seq_score = SequenceMatcher(None, norm_a, norm_b).ratio()

    # Number/date consistency
    nums_a = extract_numbers(norm_a)
    nums_b = extract_numbers(norm_b)
    num_score = 0.0
    if nums_a and nums_b:
        if nums_a == nums_b:
            num_score = 1.0
        elif nums_a & nums_b:
            num_score = len(nums_a & nums_b) / len(nums_a | nums_b)
        # Penalty: if both have numbers but NONE overlap, it's likely a different event
        if not (nums_a & nums_b) and len(nums_a) > 0 and len(nums_b) > 0:
            num_score = -0.3  # Negative penalty

    score = 0.50 * jaccard + 0.30 * seq_score + 0.20 * num_score

    # Length penalty: if titles differ hugely in scope, probably not same market
    len_ratio = min(len(norm_a), len(norm_b)) / max(len(norm_a), len(norm_b))
    if len_ratio < 0.3:
        score *= 0.5

    return max(0.0, min(1.0, score))


def _find_matches_sync(
    poly: list[tuple], kalshi: list[tuple], threshold: float
) -> list[dict]:
    """
    CPU-bound matching logic — runs in a thread to avoid blocking the event loop.

    Builds a token index on Kalshi markets and scores Polymarket markets against
    Kalshi candidates that share at least 2 meaningful tokens.
    """
    t_start = time.time()

    # Debug: dump sample titles
    logger.info("--- Sample Polymarket titles (first 5) ---")
    for _, title, price in poly[:5]:
        logger.info("  [PM $%.2f] %s", price, title[:100])
    logger.info("--- Sample Kalshi titles (first 5) ---")
    for _, title, price in kalshi[:5]:
        logger.info("  [KA $%.2f] %s", price, title[:100])

    # Step 1: Pre-normalize and tokenize all markets
    kalshi_data = {}  # id -> (title, price, normalized, tokens)
    kalshi_index = defaultdict(set)  # token -> set of kalshi ids

    for k_id, k_title, k_price in kalshi:
        norm = normalize_title(k_title)
        tokens = tokenize(norm)
        kalshi_data[k_id] = (k_title, k_price, norm, tokens)
        for token in tokens:
            kalshi_index[token].add(k_id)

    logger.info(
        "Built Kalshi inverted index: %d unique tokens across %d markets",
        len(kalshi_index),
        len(kalshi_data),
    )

    # Step 2: For each Polymarket market, find candidates via shared tokens
    matches = []
    used_kalshi = set()
    comparisons_made = 0
    top_near_misses = []

    for p_id, p_title, p_price in poly:
        p_norm = normalize_title(p_title)
        p_tokens = tokenize(p_norm)

        if not p_tokens:
            continue

        # Find candidate Kalshi markets: those sharing ANY token
        candidate_counts = defaultdict(int)  # kalshi_id -> shared_token_count
        for token in p_tokens:
            for k_id in kalshi_index.get(token, set()):
                if k_id not in used_kalshi:
                    candidate_counts[k_id] += 1

        # Only score candidates sharing ≥2 tokens (reduces noise massively)
        candidates = [k_id for k_id, count in candidate_counts.items() if count >= 2]

        if not candidates:
            continue

        best_score = 0.0
        best_k_id = None

        for k_id in candidates:
            k_title_raw, k_price, k_norm, k_tokens = kalshi_data[k_id]
            score = compute_match_score(
                p_norm, k_norm, p_tokens, k_tokens, p_title, k_title_raw
            )
            comparisons_made += 1

            if score > best_score:
                best_score = score
                best_k_id = k_id

        if best_k_id is not None:
            k_title_raw, k_price, _, _ = kalshi_data[best_k_id]
            if best_score >= threshold:
                used_kalshi.add(best_k_id)
                matches.append(
                    {
                        "poly_id": p_id,
                        "poly_title": p_title,
                        "poly_price": p_price,
                        "kalshi_id": best_k_id,
                        "kalshi_title": k_title_raw,
                        "kalshi_price": k_price,
                        "similarity": best_score,
                    }
                )
            elif best_score >= threshold - 0.15:
                top_near_misses.append((best_score, p_title[:60], k_title_raw[:60]))

    elapsed = time.time() - t_start

    # Log near-misses for tuning
    top_near_misses.sort(reverse=True)
    if top_near_misses:
        logger.info("--- Top 10 near-misses (below %.2f threshold) ---", threshold)
        for score, pt, kt in top_near_misses[:10]:
            logger.info("  %.3f | PM: %s", score, pt)
            logger.info("        | KA: %s", kt)

    matches.sort(key=lambda x: x["similarity"], reverse=True)
    logger.info(
        "Found %d matches (threshold=%.2f) | %d comparisons in %.1fs",
        len(matches),
        threshold,
        comparisons_made,
        elapsed,
    )

    # Log matched pairs
    if matches:
        logger.info("--- Matched pairs ---")
        for m in matches[:20]:
            logger.info(
                "  %.3f | spread=%.4f | PM: %s",
                m["similarity"],
                abs(m["poly_price"] - m["kalshi_price"]),
                m["poly_title"][:50],
            )
            logger.info(
                "        |              | KA: %s",
                m["kalshi_title"][:50],
            )

    return matches


async def find_matches(db: aiosqlite.Connection, threshold: float = 0.80) -> list[dict]:
    """
    Find matching markets across platforms using inverted-index blocking.

    Instead of O(n²) brute force, builds a token index on Kalshi markets
    and only scores Polymarket markets against Kalshi candidates that share
    at least 2 meaningful tokens. Runs in seconds even at 30k × 30k.

    The CPU-bound matching runs in a thread pool so it doesn't block the
    asyncio event loop (which would freeze the dashboard).
    """
    cursor = await db.execute("""SELECT id, platform, title,
           (SELECT yes_price FROM market_prices WHERE market_id = m.id
            ORDER BY polled_at DESC LIMIT 1) as price
           FROM markets m WHERE status = 'open'""")
    rows = await cursor.fetchall()

    poly = [(r[0], r[2], r[3]) for r in rows if r[1] == "polymarket" and r[3]]
    kalshi = [(r[0], r[2], r[3]) for r in rows if r[1] == "kalshi" and r[3]]

    logger.info(
        "Matching %d Polymarket × %d Kalshi markets (inverted-index)...",
        len(poly),
        len(kalshi),
    )

    if not poly or not kalshi:
        logger.info("One platform has 0 markets — skipping matching")
        return []

    # Run CPU-bound matching in a thread so the event loop stays responsive
    # (keeps the dashboard serving requests during the 30k × 30k match)
    matches = await asyncio.to_thread(_find_matches_sync, poly, kalshi, threshold)
    return matches


async def persist_matches(db: aiosqlite.Connection, matches: list[dict]) -> int:
    """Save matched pairs to market_pairs table. Returns count saved.

    Pairs with spread > 0.05 at match time are written with active=0 and
    notes='pending_review' so they never flow into the arb engine until a
    human (or automated review) verifies the match.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for m in matches:
        pair_id = f"{m['poly_id']}_{m['kalshi_id']}"
        spread = round(abs(m.get("poly_price", 0.0) - m.get("kalshi_price", 0.0)), 10)
        if spread > 0.05:
            active = 0
            notes = "pending_review"
        else:
            active = 1
            notes = None
        rows.append(
            (
                pair_id,
                m["poly_id"],
                m["kalshi_id"],
                "cross_platform",
                m.get("similarity", 0.0),
                "inverted_index",
                active,
                notes,
                now,
                now,
            )
        )
    try:
        await db.executemany(
            """INSERT OR REPLACE INTO market_pairs
               (id, market_id_a, market_id_b, pair_type, similarity_score,
                match_method, active, notes, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await db.commit()
        logger.info("Persisted %d matched pairs to market_pairs table", len(rows))
    except Exception as e:
        logger.error("Failed to persist matches: %s", e)
    return len(rows)


async def load_cached_matches(db: aiosqlite.Connection) -> list[dict]:
    """Load previously matched pairs from DB, joined with latest prices."""
    cursor = await db.execute("""
        SELECT
            mp.market_id_a AS poly_id,
            ma.title AS poly_title,
            mp.market_id_b AS kalshi_id,
            mb.title AS kalshi_title,
            mp.similarity_score,
            (SELECT yes_price FROM market_prices WHERE market_id = mp.market_id_a
             ORDER BY polled_at DESC LIMIT 1) AS poly_price,
            (SELECT yes_price FROM market_prices WHERE market_id = mp.market_id_b
             ORDER BY polled_at DESC LIMIT 1) AS kalshi_price
        FROM market_pairs mp
        JOIN markets ma ON ma.id = mp.market_id_a
        JOIN markets mb ON mb.id = mp.market_id_b
        WHERE mp.active = 1 AND mp.pair_type = 'cross_platform'
    """)
    rows = await cursor.fetchall()
    matches = []
    for r in rows:
        if r[5] and r[6]:  # both prices exist
            matches.append(
                {
                    "poly_id": r[0],
                    "poly_title": r[1],
                    "kalshi_id": r[2],
                    "kalshi_title": r[3],
                    "similarity": r[4] or 0.0,
                    "poly_price": r[5],
                    "kalshi_price": r[6],
                }
            )
    logger.info(
        "Loaded %d cached matches from DB (%d with prices)", len(rows), len(matches)
    )
    return matches


async def mark_existing_pairs_pending_review(db: aiosqlite.Connection) -> int:
    """Mark all unreviewed market_pairs as pending_review.

    Any pair with notes=NULL has not been human- or system-verified. Deactivate
    them (active=0, notes='pending_review') so they don't flow into the arb
    engine until reviewed. Pairs that already have a notes value are left alone.

    Returns the number of pairs deactivated.
    """
    cursor = await db.execute("""UPDATE market_pairs
           SET active = 0, notes = 'pending_review'
           WHERE notes IS NULL""")
    await db.commit()
    count = cursor.rowcount
    logger.info("Marked %d existing pairs as pending_review", count)
    return count


# ── Websocket price streaming ────────────────────────────────────────────────


async def stream_prices_polymarket(
    asset_ids: list[str],
    stop_event: asyncio.Event,
    on_price: typing.Callable[[str, float], typing.Awaitable[None]],
    id_map: dict[str, str] | None = None,
):
    """Stream real-time prices from Polymarket CLOB websocket.

    Polymarket limit: 500 assets per connection, so we chunk.
    Calls `on_price(market_id, price)` for every update — this is
    how the ArbitrageEngine gets triggered on each tick.

    id_map: optional mapping from platform_id (asset_id) -> internal market_id (poly_XXX)
    """
    import websockets

    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    CHUNK_SIZE = 450

    async def _connect_chunk(chunk_ids: list[str]):
        while not stop_event.is_set():
            try:
                async with websockets.connect(WS_URL, ping_interval=10) as ws:
                    sub_msg = json.dumps({"assets_ids": chunk_ids, "type": "market"})
                    await ws.send(sub_msg)
                    logger.info(
                        "Polymarket WS: subscribed to %d assets", len(chunk_ids)
                    )

                    async for raw in ws:
                        if stop_event.is_set():
                            break
                        try:
                            data = json.loads(raw)
                            for evt in data if isinstance(data, list) else [data]:
                                asset_id = evt.get("asset_id", "")
                                price = None

                                if evt.get("event_type") == "price_change":
                                    changes = evt.get("price_changes", [evt])
                                    for c in changes:
                                        bid = c.get("best_bid")
                                        ask = c.get("best_ask")
                                        if bid and ask:
                                            try:
                                                price = (float(bid) + float(ask)) / 2.0
                                            except (ValueError, TypeError):
                                                pass
                                elif "price" in evt and asset_id:
                                    try:
                                        price = float(evt["price"])
                                    except (ValueError, TypeError):
                                        pass

                                if price is not None and asset_id:
                                    market_id = (id_map or {}).get(asset_id, asset_id)
                                    await on_price(market_id, price)
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                if not stop_event.is_set():
                    logger.warning("Polymarket WS reconnecting: %s", e)
                    await asyncio.sleep(2)

    chunks = [
        asset_ids[i : i + CHUNK_SIZE] for i in range(0, len(asset_ids), CHUNK_SIZE)
    ]
    logger.info(
        "Polymarket WS: %d assets across %d connections",
        len(asset_ids),
        len(chunks),
    )
    tasks = [asyncio.create_task(_connect_chunk(c)) for c in chunks]
    await asyncio.gather(*tasks, return_exceptions=True)


async def stream_prices_kalshi(
    tickers: list[str],
    stop_event: asyncio.Event,
    on_price: typing.Callable[[str, float], typing.Awaitable[None]],
    api_key: str,
    rsa_key_path: str,
):
    """Stream real-time prices from Kalshi websocket.

    Calls `on_price(market_id, price)` for every ticker update.
    market_id is formatted as "kal_{ticker}" to match our internal IDs.
    """
    import base64
    import websockets
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

    key_path = Path(rsa_key_path).expanduser()
    private_key = serialization.load_pem_private_key(
        key_path.read_bytes(), password=None
    )

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

    while not stop_event.is_set():
        try:
            headers = sign("GET", "/trade-api/ws/v2")
            async with websockets.connect(WS_URL, additional_headers=headers) as ws:
                for i, ticker in enumerate(tickers):
                    sub = json.dumps(
                        {
                            "id": i + 1,
                            "cmd": "subscribe",
                            "params": {
                                "channels": ["ticker"],
                                "market_ticker": ticker,
                            },
                        }
                    )
                    await ws.send(sub)

                logger.info("Kalshi WS: subscribed to %d tickers", len(tickers))

                async for raw in ws:
                    if stop_event.is_set():
                        break
                    try:
                        data = json.loads(raw)
                        if data.get("type") == "ticker":
                            msg = data.get("msg", {})
                            ticker = msg.get("market_ticker", "")
                            bid = msg.get("yes_bid_dollars")
                            ask = msg.get("yes_ask_dollars")
                            if ticker and bid and ask:
                                try:
                                    price = (float(bid) + float(ask)) / 2.0
                                    await on_price(f"kal_{ticker}", price)
                                except (ValueError, TypeError):
                                    pass
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            if not stop_event.is_set():
                logger.warning("Kalshi WS reconnecting: %s", e)
                await asyncio.sleep(2)


# ── Arbitrage engine (low-latency, event-driven) ─────────────────────────────


class ArbitrageEngine:
    """Event-driven cross-platform arbitrage.

    Holds an in-memory index of matched pairs and their latest prices.
    When a websocket price update arrives, `on_price_update()` is called
    synchronously to check if any spread now exceeds the threshold.
    If so, the trade is executed immediately — no polling loop.

    This keeps arb latency bounded by websocket delivery + trade execution,
    not by a sleep-based cycle interval.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        matches: list[dict],
        min_spread: float = 0.03,
        risk_config: "RiskControlConfig | None" = None,
        circuit_breaker=None,
        execution_mode: str | None = None,
    ):
        self.db = db
        self.min_spread = min_spread
        self.trades: list[dict] = []
        self._trade_lock = asyncio.Lock()
        self._pending_commit = 0

        # Phase 2: risk config and circuit breaker.
        # Phase 6: when execution_mode is provided and risk_config is not, use
        # get_effective_risk_config so live mode automatically enforces tighter limits.
        if risk_config is not None:
            self._risk_config: RiskControlConfig = risk_config
        elif execution_mode is not None:
            from core.live_gate import get_effective_risk_config

            self._risk_config = get_effective_risk_config(execution_mode)
        else:
            self._risk_config = RiskControlConfig()
        self._circuit_breaker = circuit_breaker

        # Live price cache: market_id -> price
        self.prices: dict[str, float] = {}

        # fired_state: per-pair cooldown and re-arm state (Phase 3).
        # Replaced the bare recently_fired set to support cooldown + hysteresis re-arm.
        self.fired_state: dict[str, PairFireState] = {}

        # Telemetry fields surfaced in stats() and the STATUS log line.
        self.last_arb_fired_at: float | None = None
        self._ticks_since_last_fire: int = 0
        self._last_tick_at: dict[str, float] = {}  # market_id -> time.time()
        self._market_platform: dict[str, str] = {}  # market_id -> "polymarket"/"kalshi"

        # Build pair indexes for O(1) lookup on price update
        # poly_id -> list of (kalshi_id, match_dict)
        # kalshi_id -> list of (poly_id, match_dict)
        self._poly_to_pairs: dict[str, list[dict]] = defaultdict(list)
        self._kalshi_to_pairs: dict[str, list[dict]] = defaultdict(list)
        self._pairs: dict[str, dict] = {}  # pair_id -> match

        for m in matches:
            pair_id = f"{m['poly_id']}_{m['kalshi_id']}"
            self._pairs[pair_id] = m
            self._poly_to_pairs[m["poly_id"]].append(m)
            self._kalshi_to_pairs[m["kalshi_id"]].append(m)
            self._market_platform[m["poly_id"]] = "polymarket"
            self._market_platform[m["kalshi_id"]] = "kalshi"
            # Seed prices from match data
            if m.get("poly_price"):
                self.prices[m["poly_id"]] = m["poly_price"]
            if m.get("kalshi_price"):
                self.prices[m["kalshi_id"]] = m["kalshi_price"]

        # Execution clients (live or paper depending on EXECUTION_MODE)
        execution_mode = os.getenv("EXECUTION_MODE", "paper")
        self._poly_client, self._kalshi_client = _make_execution_clients(
            db, execution_mode
        )

        # Track pairs that need an initial sweep (prices seeded from match data)
        self._needs_initial_sweep = True

        logger.info(
            "ArbitrageEngine initialized: %d pairs, min_spread=%.4f",
            len(self._pairs),
            min_spread,
        )

    @property
    def recently_fired(self) -> set[str]:
        """Backward-compatible view of all pairs that have ever fired."""
        return set(self.fired_state.keys())

    def _is_eligible(self, pair_id: str) -> bool:
        """Return True if pair_id may fire (never fired, armed, and cooldown elapsed)."""
        state = self.fired_state.get(pair_id)
        if state is None:
            return True  # Never fired → always eligible
        if not state.armed:
            return False  # Waiting for spread reversion
        return (time.time() - state.last_fired_at) >= self._risk_config.arb_cooldown_s

    def _check_rearm(self, pair_id: str, spread: float) -> None:
        """Re-arm pair if spread has reverted below the hysteresis threshold."""
        state = self.fired_state.get(pair_id)
        if state is None or state.armed:
            return
        threshold = self.min_spread - self._risk_config.arb_rearm_hysteresis
        if spread < threshold:
            state.armed = True

    async def initial_sweep(self) -> None:
        """Check all seeded pairs for opportunities at startup.

        Prices are seeded from match data before any websocket tick arrives.
        Without this sweep, pairs already above the spread threshold at launch
        are invisible until a price delta triggers on_price_update.
        """
        if not self._needs_initial_sweep:
            return
        self._needs_initial_sweep = False

        swept = 0
        for match in self._pairs.values():
            pair_id = f"{match['poly_id']}_{match['kalshi_id']}"
            p_price = self.prices.get(match["poly_id"])
            k_price = self.prices.get(match["kalshi_id"])
            if p_price is None or k_price is None:
                continue
            spread = abs(p_price - k_price)
            self._check_rearm(pair_id, spread)
            if spread < self.min_spread:
                continue
            if not self._is_eligible(pair_id):
                continue
            async with self._trade_lock:
                self._check_rearm(pair_id, spread)
                if not self._is_eligible(pair_id):
                    continue
                try:
                    trade = await self._execute_arb_trade(
                        match, p_price, k_price, spread, pair_id
                    )
                except Exception:
                    logger.exception(
                        "Unhandled exception in initial_sweep for pair %s", pair_id
                    )
                    continue
                if trade:
                    self.fired_state[pair_id] = PairFireState(
                        last_fired_at=time.time(), armed=False
                    )
                    self.trades.append(trade)
                    self.last_arb_fired_at = time.time()
                    self._ticks_since_last_fire = 0
                    swept += 1

        if swept:
            logger.info("Initial sweep found %d arb opportunities", swept)

    async def on_price_update(self, market_id: str, new_price: float):
        """Called by websocket handlers on every price tick.

        Checks all pairs involving this market_id. If any spread
        exceeds threshold and we don't have an open position, trade.
        """
        old_price = self.prices.get(market_id)
        self.prices[market_id] = new_price
        self._last_tick_at[market_id] = time.time()

        # Skip if price didn't change meaningfully
        if old_price is not None and abs(new_price - old_price) < 0.001:
            return

        self._ticks_since_last_fire += 1

        # Find all pairs involving this market
        affected = []
        if market_id in self._poly_to_pairs:
            affected.extend(self._poly_to_pairs[market_id])
        if market_id in self._kalshi_to_pairs:
            affected.extend(self._kalshi_to_pairs[market_id])

        for match in affected:
            pair_id = f"{match['poly_id']}_{match['kalshi_id']}"

            p_price = self.prices.get(match["poly_id"])
            k_price = self.prices.get(match["kalshi_id"])
            if p_price is None or k_price is None:
                continue

            spread = abs(p_price - k_price)
            # Always run re-arm check (spread may have reverted below threshold)
            self._check_rearm(pair_id, spread)

            if spread < self.min_spread:
                continue

            if not self._is_eligible(pair_id):
                continue

            # Execute immediately under lock (prevent concurrent trades on same pair)
            async with self._trade_lock:
                # Re-check under lock (state may have changed)
                self._check_rearm(pair_id, spread)
                if not self._is_eligible(pair_id):
                    continue
                # fired_state updated only on success; risk/CB rejections leave pair
                # retriable (Phase 2 contract). Exceptions also leave pair unlocked.
                try:
                    trade = await self._execute_arb_trade(
                        match, p_price, k_price, spread, pair_id
                    )
                except Exception:
                    logger.exception(
                        "Unhandled exception in on_price_update for pair %s", pair_id
                    )
                    continue
                if trade:
                    self.fired_state[pair_id] = PairFireState(
                        last_fired_at=time.time(), armed=False
                    )
                    self.trades.append(trade)
                    self.last_arb_fired_at = time.time()
                    self._ticks_since_last_fire = 0

    async def periodic_scan(self) -> None:
        """Scan all tracked pairs for arbitrage opportunities.

        Called on a timer, independent of price-tick events. Catches pairs
        whose spread opened while both prices drifted simultaneously (no single
        tick would have triggered on_price_update for the pair).
        """
        for match in self._pairs.values():
            pair_id = f"{match['poly_id']}_{match['kalshi_id']}"
            p_price = self.prices.get(match["poly_id"])
            k_price = self.prices.get(match["kalshi_id"])
            if p_price is None or k_price is None:
                continue
            spread = abs(p_price - k_price)
            self._check_rearm(pair_id, spread)
            if spread < self.min_spread:
                continue
            if not self._is_eligible(pair_id):
                continue
            async with self._trade_lock:
                self._check_rearm(pair_id, spread)
                if not self._is_eligible(pair_id):
                    continue
                try:
                    trade = await self._execute_arb_trade(
                        match, p_price, k_price, spread, pair_id
                    )
                except Exception:
                    logger.exception(
                        "Unhandled exception in periodic_scan for pair %s", pair_id
                    )
                    continue
                if trade:
                    self.fired_state[pair_id] = PairFireState(
                        last_fired_at=time.time(), armed=False
                    )
                    self.trades.append(trade)
                    self.last_arb_fired_at = time.time()
                    self._ticks_since_last_fire = 0

    async def _execute_arb_trade(
        self,
        match: dict,
        p_price: float,
        k_price: float,
        spread: float,
        pair_id: str,
    ) -> dict | None:
        """Execute a single arbitrage trade on a matched pair."""
        from execution.models import OrderLeg

        now = datetime.now(timezone.utc).isoformat()
        strategy = "P1_cross_market_arb"
        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        violation_id = f"viol_{uuid.uuid4().hex[:12]}"

        if p_price < k_price:
            buy_platform, sell_platform = "polymarket", "kalshi"
            buy_id, sell_id = match["poly_id"], match["kalshi_id"]
            buy_price, sell_price = p_price, k_price
            buy_client, sell_client = self._poly_client, self._kalshi_client
        else:
            buy_platform, sell_platform = "kalshi", "polymarket"
            buy_id, sell_id = match["kalshi_id"], match["poly_id"]
            buy_price, sell_price = k_price, p_price
            buy_client, sell_client = self._kalshi_client, self._poly_client

        # Phase 2.4: circuit breaker halt check.
        if self._circuit_breaker is not None:
            if await self._circuit_breaker.should_halt():
                logger.warning(
                    "CIRCUIT_BREAKER halted — skipping arb trade on pair=%s", pair_id
                )
                return None

        edge = spread

        # Phase 2.3: Kelly-based position sizing (replaces hardcoded min(10, 100*edge)).
        kelly_f = compute_kelly_fraction(edge, 1.0, self._risk_config.kelly_fraction)
        bankroll = self._risk_config.starting_capital
        max_size = bankroll * self._risk_config.max_position_pct
        size = round(compute_position_size(kelly_f, bankroll, max_size=max_size), 1)
        if size <= 0:
            logger.debug(
                "Kelly sizing produced zero size for edge=%.4f — skipping", edge
            )
            return None

        # Phase 2.2: run all risk checks before executing orders.
        risk_signal = _RiskSignal(
            legs=[
                _RiskLeg(
                    market_id=buy_id, limit_price=buy_price, size=size, side="BUY"
                ),
                _RiskLeg(
                    market_id=sell_id, limit_price=sell_price, size=size, side="SELL"
                ),
            ],
            edge=edge,
            strategy=strategy,
        )
        all_passed, check_results = await run_all_checks(
            risk_signal, self._risk_config, self.db
        )
        if not all_passed:
            failed = [r.check_type for r in check_results if not r.passed]
            logger.info(
                "RISK_REJECTED pair=%s failed_checks=%s — not adding to recently_fired",
                pair_id,
                failed,
            )
            return None

        logger.info(
            "ARB TRADE: spread=%.4f | %s@%.3f vs %s@%.3f",
            spread,
            match["poly_title"][:40],
            p_price,
            match["kalshi_title"][:40],
            k_price,
        )

        # Write market_pair, violation, signal
        try:
            await self.db.execute(
                """INSERT OR IGNORE INTO market_pairs
                   (id, market_id_a, market_id_b, pair_type, similarity_score,
                    match_method, active, created_at, updated_at)
                   VALUES (?, ?, ?, 'cross_platform', ?, 'inverted_index', 1, ?, ?)""",
                (pair_id, buy_id, sell_id, match.get("similarity", 0.0), now, now),
            )
        except Exception as e:
            logger.debug("Market pair insert error: %s", e)

        try:
            await self.db.execute(
                """INSERT OR IGNORE INTO violations
                   (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
                    raw_spread, net_spread, fee_estimate_a, fee_estimate_b,
                    status, detected_at, updated_at)
                   VALUES (?, ?, 'cross_platform', ?, ?, ?, ?, ?, ?, 'detected', ?, ?)""",
                (
                    violation_id,
                    pair_id,
                    buy_price,
                    sell_price,
                    spread,
                    spread - 0.02,
                    buy_price * 0.02,
                    sell_price * 0.02,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Violation insert error: %s", e)

        try:
            kelly = min(edge / 0.50, 0.25)
            await self.db.execute(
                """INSERT OR IGNORE INTO signals
                   (id, violation_id, strategy, signal_type, market_id_a, market_id_b,
                    model_edge, kelly_fraction, position_size_a, position_size_b,
                    total_capital_at_risk, status, fired_at, updated_at)
                   VALUES (?, ?, ?, 'arb_pair', ?, ?, ?, ?, ?, ?, ?, 'fired', ?, ?)""",
                (
                    signal_id,
                    violation_id,
                    strategy,
                    buy_id,
                    sell_id,
                    edge,
                    kelly,
                    size,
                    size,
                    size * 2,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Signal insert error: %s", e)

        # Execute both legs
        buy_leg = OrderLeg(
            market_id=buy_id,
            platform=buy_platform,
            side="BUY",
            size=size,
            limit_price=buy_price,
            order_type="LIMIT",
        )
        sell_leg = OrderLeg(
            market_id=sell_id,
            platform=sell_platform,
            side="SELL",
            size=size,
            limit_price=sell_price,
            order_type="LIMIT",
        )
        buy_result = await buy_client.submit_order(
            buy_leg, signal_id=signal_id, strategy=strategy
        )
        sell_result = await sell_client.submit_order(
            sell_leg, signal_id=signal_id, strategy=strategy
        )

        if buy_result.filled_price and sell_result.filled_price:
            actual_spread = sell_result.filled_price - buy_result.filled_price
            total_fees = (buy_result.fee_paid or 0) + (sell_result.fee_paid or 0)
            actual_pnl = round(actual_spread * size - total_fees, 4)

            _pnl_cap = size * _PNL_SANITY_CAP_RATIO
            if actual_pnl > _pnl_cap:
                logger.warning(
                    "PNL_SANITY_CAP blocked pair=%s actual_pnl=%.4f > cap=%.4f "
                    "(size=%.1f spread=%.4f). Likely false-positive pair — skipping DB write.",
                    pair_id,
                    actual_pnl,
                    _pnl_cap,
                    size,
                    spread,
                )
                return None

            pos_id = f"pos_{uuid.uuid4().hex[:12]}"
            try:
                await self.db.execute(
                    """INSERT INTO positions
                       (id, signal_id, market_id, strategy, side, entry_price,
                        entry_size, exit_price, exit_size, realized_pnl, fees_paid,
                        status, opened_at, closed_at, updated_at)
                       VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)""",
                    (
                        pos_id,
                        signal_id,
                        buy_id,
                        strategy,
                        buy_result.filled_price,
                        size,
                        sell_result.filled_price,
                        size,
                        actual_pnl,
                        total_fees,
                        now,
                        now,
                        now,
                    ),
                )
            except Exception:
                pass

            try:
                await self.db.execute(
                    """INSERT INTO trade_outcomes
                       (id, signal_id, strategy, violation_id, market_id_a, market_id_b,
                        predicted_edge, predicted_pnl, actual_pnl, fees_total,
                        edge_captured_pct, signal_to_fill_ms, holding_period_ms,
                        resolved_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"trade_{uuid.uuid4().hex[:12]}",
                        signal_id,
                        strategy,
                        violation_id,
                        buy_id,
                        sell_id,
                        edge,
                        round(edge * size, 4),
                        actual_pnl,
                        total_fees,
                        (
                            round((actual_pnl / (edge * size)) * 100, 1)
                            if edge * size > 0
                            else 0
                        ),
                        buy_result.submission_latency_ms
                        + (buy_result.fill_latency_ms or 0),
                        5000,
                        now,
                        now,
                    ),
                )
            except Exception:
                pass

            # Batch commit
            self._pending_commit += 1
            if self._pending_commit >= 10:
                await self.db.commit()
                self._pending_commit = 0

            logger.info(
                "  ARB FILLED: pnl=$%.4f fees=$%.4f | buy@%.4f sell@%.4f",
                actual_pnl,
                total_fees,
                buy_result.filled_price,
                sell_result.filled_price,
            )

            return {
                "strategy": strategy,
                "pair_id": pair_id,
                "spread": spread,
                "actual_pnl": actual_pnl,
                "fees": total_fees,
            }
        return None

    async def flush(self):
        """Commit any pending DB writes."""
        if self._pending_commit > 0:
            await self.db.commit()
            self._pending_commit = 0

    def stats(self) -> dict:
        total_pnl = sum(t.get("actual_pnl", 0) for t in self.trades)
        eligible = sum(
            1
            for m in self._pairs.values()
            if (p := self.prices.get(m["poly_id"])) is not None
            and (k := self.prices.get(m["kalshi_id"])) is not None
            and abs(p - k) >= self.min_spread
        )
        now = time.time()
        tick_age: dict[str, int] = {}
        for market_id, ts in self._last_tick_at.items():
            platform = self._market_platform.get(market_id, "unknown")
            age_ms = int((now - ts) * 1000)
            # Keep the freshest (smallest age) tick per platform
            if platform not in tick_age or tick_age[platform] > age_ms:
                tick_age[platform] = age_ms
        return {
            "pairs_monitored": len(self._pairs),
            "pairs_eligible_now": eligible,
            "recently_fired": len(self.recently_fired),
            "last_arb_fired_at": self.last_arb_fired_at,
            "ticks_since_last_fire": self._ticks_since_last_fire,
            "total_pnl": total_pnl,
            "prices_tracked": len(self.prices),
            "ws_last_tick_age_ms_by_platform": tick_age,
        }


class ScheduledStrategyRunner:
    """Runs non-latency-sensitive strategies on a fixed interval.

    These strategies don't depend on capturing a fleeting spread —
    they analyze calibration bias, liquidity patterns, mean reversion, etc.
    Running every 60-300s is fine.
    """

    def __init__(
        self,
        db: aiosqlite.Connection,
        interval: int = 120,
        max_trades_per_cycle: int = 20,
        risk_config: "RiskControlConfig | None" = None,
        circuit_breaker=None,
        execution_mode: str | None = None,
        alert_manager=None,
    ):
        self.db = db
        self.interval = interval
        self.max_trades = max_trades_per_cycle
        self.total_trades = 0
        # Phase 2: risk config and circuit breaker.
        # Phase 6: when execution_mode is provided and risk_config is not, use
        # get_effective_risk_config so live mode automatically enforces tighter limits.
        if risk_config is not None:
            self._risk_config: RiskControlConfig = risk_config
        elif execution_mode is not None:
            from core.live_gate import get_effective_risk_config

            self._risk_config = get_effective_risk_config(execution_mode)
        else:
            self._risk_config = RiskControlConfig()
        self._circuit_breaker = circuit_breaker
        # Phase 7: alert_manager forwards invariant violations to Discord.
        self._alert_manager = alert_manager

    async def run_one_cycle(self) -> list:
        """Execute a single strategy cycle. Returns list of opened positions.

        Circuit breaker check happens first — if halted, returns [] immediately.
        After opening new positions, closes any that have exceeded holding_period_s
        (Phase 4 realistic fill model).
        Extracted from run() so it can be called and tested independently.
        """
        if self._circuit_breaker is not None:
            if await self._circuit_breaker.should_halt():
                logger.warning(
                    "CIRCUIT_BREAKER halted — skipping scheduled strategy cycle"
                )
                return []
        # Mark-to-market pass: close expired open positions at current prices
        await mark_and_close_positions(
            self.db, holding_period_s=self._risk_config.strategy_holding_period_s
        )
        # Phase 7: run invariant checks before opening new positions.
        # alert_manager forwards violations to Discord when configured.
        from core.invariants import check_all_invariants

        await check_all_invariants(
            self.db, mode="warn", alert_manager=self._alert_manager
        )
        return await detect_single_platform_opportunities(
            self.db, max_trades=self.max_trades, risk_config=self._risk_config
        )

    async def run(self, stop_event: asyncio.Event):
        """Run strategy cycles until stop_event is set."""
        logger.info(
            "ScheduledStrategyRunner started: interval=%ds, max_trades=%d",
            self.interval,
            self.max_trades,
        )
        while not stop_event.is_set():
            try:
                t0 = time.time()
                trades = await self.run_one_cycle()
                self.total_trades += len(trades)
                elapsed = time.time() - t0
                logger.info(
                    "Scheduled strategies: %d trades in %.1fs (total: %d)",
                    len(trades),
                    elapsed,
                    self.total_trades,
                )
            except Exception as e:
                logger.error("Scheduled strategy error: %s", e)

            # Wait for interval or stop
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # interval elapsed, run again


# ── Legacy batch trade functions (used by --once / --refresh) ────────────────


async def detect_violations_and_trade(
    db: aiosqlite.Connection,
    matches: list[dict],
    min_spread: float = 0.03,
) -> list[dict]:
    """
    For each matched pair, check for price discrepancies.
    If spread exceeds threshold, generate a signal and execute paper trade.
    """
    from execution.models import OrderLeg

    execution_mode = os.getenv("EXECUTION_MODE", "paper")
    paper_poly, paper_kalshi = _make_execution_clients(db, execution_mode)

    trades = []

    for match in matches:
        now = datetime.now(timezone.utc).isoformat()
        p_price = match["poly_price"]
        k_price = match["kalshi_price"]
        spread = abs(p_price - k_price)

        if spread < min_spread:
            continue

        strategy = assign_strategy(spread, "cross_platform")
        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        violation_id = f"viol_{uuid.uuid4().hex[:12]}"

        # Determine direction: buy cheap, sell expensive
        if p_price < k_price:
            buy_platform, sell_platform = "polymarket", "kalshi"
            buy_id, sell_id = match["poly_id"], match["kalshi_id"]
            buy_price, sell_price = p_price, k_price
            buy_client, sell_client = paper_poly, paper_kalshi
        else:
            buy_platform, sell_platform = "kalshi", "polymarket"
            buy_id, sell_id = match["kalshi_id"], match["poly_id"]
            buy_price, sell_price = k_price, p_price
            buy_client, sell_client = paper_kalshi, paper_poly

        edge = spread
        _rc = RiskControlConfig()
        _kelly_f = compute_kelly_fraction(edge, 1.0, _rc.kelly_fraction)
        size = round(
            compute_position_size(
                _kelly_f,
                _rc.starting_capital,
                max_size=_rc.starting_capital * _rc.max_position_pct,
            ),
            1,
        )
        if size <= 0:
            continue

        logger.info(
            "VIOLATION: spread=%.4f | %s@%.3f vs %s@%.3f | %s",
            spread,
            match["poly_title"][:40],
            p_price,
            match["kalshi_title"][:40],
            k_price,
            strategy,
        )

        # Create market pair record
        pair_id = f"{buy_id}_{sell_id}"
        try:
            await db.execute(
                """INSERT OR IGNORE INTO market_pairs
                   (id, market_id_a, market_id_b, pair_type, similarity_score,
                    match_method, active, created_at, updated_at)
                   VALUES (?, ?, ?, 'cross_platform', ?, 'inverted_index', 1, ?, ?)""",
                (pair_id, buy_id, sell_id, match.get("similarity", 0.0), now, now),
            )
        except Exception as e:
            logger.debug("Market pair insert error: %s", e)

        # Write violation (schema: id, pair_id, violation_type,
        #   price_a_at_detect, price_b_at_detect, raw_spread, net_spread,
        #   fee_estimate_a, fee_estimate_b, status, detected_at, updated_at)
        try:
            await db.execute(
                """INSERT OR IGNORE INTO violations
                   (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
                    raw_spread, net_spread, fee_estimate_a, fee_estimate_b,
                    status, detected_at, updated_at)
                   VALUES (?, ?, 'cross_platform', ?, ?, ?, ?, ?, ?, 'detected', ?, ?)""",
                (
                    violation_id,
                    pair_id,
                    buy_price,
                    sell_price,
                    spread,
                    spread - 0.02,
                    buy_price * 0.02,
                    sell_price * 0.02,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Violation insert error: %s", e)

        # Write signal (schema: id, violation_id, strategy, signal_type,
        #   market_id_a, market_id_b, model_edge, kelly_fraction,
        #   position_size_a, position_size_b, total_capital_at_risk,
        #   status, fired_at, updated_at)
        try:
            kelly = min(edge / 0.50, 0.25)  # simplified Kelly
            await db.execute(
                """INSERT OR IGNORE INTO signals
                   (id, violation_id, strategy, signal_type, market_id_a, market_id_b,
                    model_edge, kelly_fraction, position_size_a, position_size_b,
                    total_capital_at_risk, status, fired_at, updated_at)
                   VALUES (?, ?, ?, 'arb_pair', ?, ?, ?, ?, ?, ?, ?, 'fired', ?, ?)""",
                (
                    signal_id,
                    violation_id,
                    strategy,
                    buy_id,
                    sell_id,
                    edge,
                    kelly,
                    size,
                    size,
                    size * 2,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Signal insert error: %s", e)

        # Execute paper trades
        buy_leg = OrderLeg(
            market_id=buy_id,
            platform=buy_platform,
            side="BUY",
            size=size,
            limit_price=buy_price,
            order_type="LIMIT",
        )
        sell_leg = OrderLeg(
            market_id=sell_id,
            platform=sell_platform,
            side="SELL",
            size=size,
            limit_price=sell_price,
            order_type="LIMIT",
        )

        buy_result = await buy_client.submit_order(
            buy_leg, signal_id=signal_id, strategy=strategy
        )
        sell_result = await sell_client.submit_order(
            sell_leg, signal_id=signal_id, strategy=strategy
        )

        # Record position and trade outcome
        if buy_result.filled_price and sell_result.filled_price:
            actual_spread = sell_result.filled_price - buy_result.filled_price
            total_fees = (buy_result.fee_paid or 0) + (sell_result.fee_paid or 0)
            actual_pnl = round(actual_spread * size - total_fees, 4)

            _pnl_cap = size * _PNL_SANITY_CAP_RATIO
            if actual_pnl > _pnl_cap:
                logger.warning(
                    "PNL_SANITY_CAP blocked dual-platform arb actual_pnl=%.4f > cap=%.4f "
                    "(size=%.1f). Likely false-positive pair — skipping DB write.",
                    actual_pnl,
                    _pnl_cap,
                    size,
                )
                return None

            pos_id = f"pos_{uuid.uuid4().hex[:12]}"
            try:
                await db.execute(
                    """INSERT INTO positions
                       (id, signal_id, market_id, strategy, side, entry_price,
                        entry_size, exit_price, exit_size, realized_pnl, fees_paid,
                        status, opened_at, closed_at, updated_at)
                       VALUES (?, ?, ?, ?, 'BUY', ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)""",
                    (
                        pos_id,
                        signal_id,
                        buy_id,
                        strategy,
                        buy_result.filled_price,
                        size,
                        sell_result.filled_price,
                        size,
                        actual_pnl,
                        total_fees,
                        now,
                        now,
                        now,
                    ),
                )
            except Exception:
                pass

            try:
                await db.execute(
                    """INSERT INTO trade_outcomes
                       (id, signal_id, strategy, violation_id, market_id_a, market_id_b,
                        predicted_edge, predicted_pnl, actual_pnl, fees_total,
                        edge_captured_pct, signal_to_fill_ms, holding_period_ms,
                        resolved_at, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        f"trade_{uuid.uuid4().hex[:12]}",
                        signal_id,
                        strategy,
                        violation_id,
                        buy_id,
                        sell_id,
                        edge,
                        round(edge * size, 4),
                        actual_pnl,
                        total_fees,
                        (
                            round((actual_pnl / (edge * size)) * 100, 1)
                            if edge * size > 0
                            else 0
                        ),
                        buy_result.submission_latency_ms
                        + (buy_result.fill_latency_ms or 0),
                        5000,
                        now,
                        now,
                    ),
                )
            except Exception:
                pass

            trades.append(
                {
                    "strategy": strategy,
                    "buy": f"{buy_platform}:{buy_id[:20]}",
                    "sell": f"{sell_platform}:{sell_id[:20]}",
                    "spread": spread,
                    "actual_pnl": actual_pnl,
                    "fees": total_fees,
                }
            )

            logger.info(
                "  TRADE: pnl=$%.4f fees=$%.4f | buy@%.4f sell@%.4f",
                actual_pnl,
                total_fees,
                buy_result.filled_price,
                sell_result.filled_price,
            )

            # Batch commit every 50 trades
            if len(trades) % 50 == 0:
                await db.commit()

    # Final commit for remaining trades
    await db.commit()
    return trades


# ── Mark-to-market and time-based exit ────────────────────────────────────


async def mark_and_close_positions(
    db: aiosqlite.Connection,
    holding_period_s: int = 300,
) -> int:
    """Close open positions that have exceeded their holding period.

    Walks all positions with status='open' opened more than holding_period_s
    ago, queries the latest market price, computes realized_pnl, and closes
    them. Positions with no current price data are left open (stale handling).

    Returns the number of positions closed.
    """
    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(seconds=holding_period_s)).isoformat()
    now = now_dt.isoformat()

    cursor = await db.execute(
        "SELECT id, market_id, side, entry_price, entry_size, fees_paid "
        "FROM positions WHERE status='open' AND opened_at <= ?",
        (cutoff,),
    )
    rows = await cursor.fetchall()

    closed = 0
    for row in rows:
        pos_id, market_id, side, entry_price, entry_size, fees_paid = row

        price_cursor = await db.execute(
            "SELECT yes_price FROM market_prices WHERE market_id=? "
            "ORDER BY polled_at DESC LIMIT 1",
            (market_id,),
        )
        price_row = await price_cursor.fetchone()
        if price_row is None:
            logger.debug(
                "mark_and_close: no price for market %s — leaving open", market_id
            )
            continue

        current_price = price_row[0]
        if side == "BUY":
            realized_pnl = round(
                (current_price - entry_price) * entry_size - (fees_paid or 0), 4
            )
        else:
            realized_pnl = round(
                (entry_price - current_price) * entry_size - (fees_paid or 0), 4
            )

        await db.execute(
            """UPDATE positions
               SET status='closed', exit_price=?, exit_size=?,
                   realized_pnl=?, current_price=?, closed_at=?, updated_at=?
             WHERE id=?""",
            (current_price, entry_size, realized_pnl, current_price, now, now, pos_id),
        )
        closed += 1
        logger.debug(
            "mark_and_close: closed pos=%s market=%s pnl=%.4f",
            pos_id,
            market_id,
            realized_pnl,
        )

    if closed:
        await db.commit()
        logger.info("mark_and_close_positions: closed %d expired positions", closed)

    return closed


# ── Phase 5: Strategy hygiene helpers ────────────────────────────────────────


def _cross_strategy_dedup(opportunities: list[dict]) -> list[dict]:
    """Keep only the highest-signal_strength entry per market_id (5.1).

    A market that qualifies for both P3 and P4 in the same cycle contributes
    one position — for the strategy most confident about it.
    """
    best: dict[str, dict] = {}
    for opp in opportunities:
        mid = opp["market"]["id"]
        if mid not in best or opp["signal_strength"] > best[mid]["signal_strength"]:
            best[mid] = opp
    return list(best.values())


def _normalize_signal_strengths(opportunities: list[dict]) -> list[dict]:
    """Z-score normalize signal_strength within each strategy bucket (5.3).

    Adds a signal_strength_normalized key to each opportunity. The original
    signal_strength is preserved. Single-item buckets receive 0.0.
    """
    import statistics

    by_strategy: dict[str, list[dict]] = {}
    for opp in opportunities:
        by_strategy.setdefault(opp["strategy"], []).append(opp)

    result = []
    for opps in by_strategy.values():
        strengths = [o["signal_strength"] for o in opps]
        if len(strengths) == 1:
            result.append({**opps[0], "signal_strength_normalized": 0.0})
            continue
        mean = statistics.mean(strengths)
        stdev = statistics.stdev(strengths)
        for opp in opps:
            z = (opp["signal_strength"] - mean) / stdev if stdev > 0 else 0.0
            result.append({**opp, "signal_strength_normalized": z})
    return result


async def _get_strategy_rolling_pnl(
    db: aiosqlite.Connection,
    strategy: str,
    window_s: int,
) -> tuple[int, float]:
    """Return (trade_count, total_pnl) for strategy over the last window_s seconds.

    Only counts realistic-model closed positions (pnl_model='realistic').
    Used by the per-strategy kill-switch (5.6).
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_s)).isoformat()
    cursor = await db.execute(
        "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0.0) "
        "FROM positions "
        "WHERE strategy=? AND pnl_model='realistic' AND status='closed' AND closed_at >= ?",
        (strategy, cutoff),
    )
    row = await cursor.fetchone()
    return row[0], row[1]


# ── Single-platform strategies ──────────────────────────────────────────────


async def detect_single_platform_opportunities(
    db: aiosqlite.Connection,
    max_trades: int = 20,
    risk_config: "RiskControlConfig | None" = None,
) -> list[dict]:
    """
    Find trading opportunities on individual markets (no cross-platform match needed).

    Strategies:
    - P3_calibration_bias: Market is mispriced vs estimated fair value
      (e.g., price far from 0.50 on a coin-flip event, or spread is wide)
    - P4_liquidity_timing: Wide bid/ask spread indicates market maker opportunity
    - P5_mean_reversion: Price moved sharply, bet on reversion
    """
    _risk_cfg: RiskControlConfig = risk_config or RiskControlConfig()
    from execution.models import OrderLeg

    execution_mode = os.getenv("EXECUTION_MODE", "paper")
    # Cache clients by platform to avoid re-initializing per trade
    _clients: dict = {}

    # Get all markets with their latest prices
    cursor = await db.execute("""SELECT m.id, m.platform, m.title,
                  mp.yes_price, mp.no_price, mp.spread,
                  mp.volume_24h, mp.liquidity
           FROM markets m
           JOIN market_prices mp ON mp.market_id = m.id
           WHERE m.status = 'open'
             AND mp.yes_price > 0.05
             AND mp.yes_price < 0.95
           ORDER BY mp.polled_at DESC""")
    rows = await cursor.fetchall()

    # Deduplicate (keep latest per market)
    seen = set()
    markets = []
    for row in rows:
        if row[0] not in seen:
            seen.add(row[0])
            markets.append(
                {
                    "id": row[0],
                    "platform": row[1],
                    "title": row[2],
                    "yes_price": row[3],
                    "no_price": row[4],
                    "spread": row[5] or 0.02,
                    "volume_24h": row[6] or 0,
                    "liquidity": row[7] or 0,
                }
            )

    trades = []
    opportunities = []

    for m in markets:
        price = m["yes_price"]

        # Strategy: Calibration bias — markets with extreme prices
        # (far from 0.50) have high implied conviction. We bet toward
        # the center, assuming mean reversion over time.
        # Edge = 2% of the distance from center (conservative).
        distance_from_center = abs(price - 0.50)
        if distance_from_center > 0.20:
            side = "BUY" if price < 0.50 else "SELL"
            edge = round(distance_from_center * 0.04, 4)  # 4% of distance
            opportunities.append(
                {
                    "market": m,
                    "strategy": "P3_calibration_bias",
                    "side": side,
                    "edge": max(edge, 0.005),
                    "signal_strength": distance_from_center,
                }
            )

        # Strategy: Liquidity timing — markets in the "uncertain zone"
        # (0.35 - 0.65) are most liquid and have the tightest spreads.
        # For markets slightly outside this zone (0.20-0.35 or 0.65-0.80),
        # there's often a liquidity premium we can capture.
        if 0.15 < price < 0.35 or 0.65 < price < 0.85:
            side = "BUY" if price < 0.50 else "SELL"
            edge = round(abs(price - 0.50) * 0.03, 4)  # 3% of distance
            opportunities.append(
                {
                    "market": m,
                    "strategy": "P4_liquidity_timing",
                    "side": side,
                    "edge": max(edge, 0.005),
                    "signal_strength": abs(price - 0.50),
                }
            )

        # Strategy: Information latency — wide bid-ask spread combined with an
        # extreme price indicates that market makers haven't caught up to recent
        # information. The directional signal is already in the price; the edge
        # is the spread compression that occurs as information propagates.
        if m["spread"] >= 0.08 and (price < 0.25 or price > 0.75):
            side = "BUY" if price < 0.50 else "SELL"
            edge = round(m["spread"] * 0.30, 4)  # capture ~30% of the spread
            opportunities.append(
                {
                    "market": m,
                    "strategy": "P5_information_latency",
                    "side": side,
                    "edge": max(edge, 0.005),
                    "signal_strength": m["spread"],
                }
            )

    # Strategy: Structured event inconsistency — group same-platform markets by
    # title root (stripping date/value suffixes). When the YES prices of a
    # mutually-exclusive series sum > 1.05, sell the most overpriced member.
    _p2_groups: dict[tuple, list[dict]] = defaultdict(list)
    for m in markets:
        root = _p2_title_root(m["title"])
        if len(root) >= 25:
            _p2_groups[(m["platform"], root)].append(m)

    for (_plat, _root), group in _p2_groups.items():
        if len(group) < 2 or len(group) > 10:
            continue
        total_yes = sum(m["yes_price"] for m in group)
        if total_yes <= 1.05:
            continue
        expected = 1.0 / len(group)
        best = max(group, key=lambda m: m["yes_price"] - expected)
        edge = round((total_yes - 1.0) / len(group), 4)
        opportunities.append(
            {
                "market": best,
                "strategy": "P2_structured_event",
                "side": "SELL",
                "edge": max(edge, 0.005),
                "signal_strength": total_yes - 1.0,
            }
        )

    # ── Phase 5: Strategy hygiene ─────────────────────────────────────────────

    # 5.4 — Filter strategies disabled via config flags
    _strategy_enabled = {
        "P2_structured_event": _risk_cfg.strategy_p2_enabled,
        "P3_calibration_bias": _risk_cfg.strategy_p3_enabled,
        "P4_liquidity_timing": _risk_cfg.strategy_p4_enabled,
        "P5_information_latency": _risk_cfg.strategy_p5_enabled,
    }
    opportunities = [
        o for o in opportunities if _strategy_enabled.get(o["strategy"], True)
    ]

    # 5.1 — Cross-strategy dedup: same market → keep highest signal_strength only
    opportunities = _cross_strategy_dedup(opportunities)

    # 5.2 — Consecutive-cycle dedup: skip recently-traded markets unless price moved
    _cooldown = _risk_cfg.strategy_replay_cooldown_s
    _min_move = _risk_cfg.strategy_replay_min_move
    _cutoff = (datetime.now(timezone.utc) - timedelta(seconds=_cooldown)).isoformat()
    _recent_cursor = await db.execute(
        "SELECT market_id, entry_price FROM positions "
        "WHERE (status='open' OR (status='closed' AND closed_at >= ?)) "
        "ORDER BY opened_at DESC",
        (_cutoff,),
    )
    _recently_traded: dict[str, float] = {}
    for _row in await _recent_cursor.fetchall():
        _mid, _ep = _row[0], _row[1]
        if _mid not in _recently_traded:
            _recently_traded[_mid] = _ep or 0.0

    _filtered: list[dict] = []
    for opp in opportunities:
        mid = opp["market"]["id"]
        if mid in _recently_traded:
            ep = _recently_traded[mid]
            cp = opp["market"]["yes_price"]
            move = abs(cp - ep) / max(ep, 0.001) if ep else 1.0
            if move < _min_move:
                logger.debug(
                    "5.2 replay-dedup: skip market=%s move=%.4f < min=%.4f",
                    mid,
                    move,
                    _min_move,
                )
                continue
        _filtered.append(opp)
    opportunities = _filtered

    # 5.6 — Per-strategy kill-switch: disable strategy if rolling PnL is negative
    # and enough trades have been recorded to be statistically meaningful.
    _killed_strategies: set[str] = set()
    for _strat in list({o["strategy"] for o in opportunities}):
        _count, _pnl = await _get_strategy_rolling_pnl(
            db, _strat, _risk_cfg.strategy_killswitch_window_s
        )
        if _count >= _risk_cfg.strategy_killswitch_min_trades and _pnl < 0:
            logger.warning(
                "KILLSWITCH: strategy=%s disabled — rolling_pnl=%.4f over %d trades",
                _strat,
                _pnl,
                _count,
            )
            _killed_strategies.add(_strat)
    opportunities = [
        o for o in opportunities if o["strategy"] not in _killed_strategies
    ]

    # 5.3 — Normalize signal_strength within each strategy bucket (z-score)
    opportunities = _normalize_signal_strengths(opportunities)

    # ── Quota allocation ──────────────────────────────────────────────────────
    # Reserve slots per strategy to prevent any single strategy crowding others.
    # P2: 15%, P3: 50%, P4: 25%, P5: remainder (min 1 each).
    p2_cap = max(1, int(max_trades * 0.15))
    p3_cap = max(1, int(max_trades * 0.50))
    p4_cap = max(2, int(max_trades * 0.25))
    p5_cap = max(1, max_trades - p2_cap - p3_cap - p4_cap)

    p2 = sorted(
        [o for o in opportunities if o["strategy"] == "P2_structured_event"],
        key=lambda x: x["signal_strength"],
        reverse=True,
    )[:p2_cap]
    p3 = sorted(
        [o for o in opportunities if o["strategy"] == "P3_calibration_bias"],
        key=lambda x: x["signal_strength"],
        reverse=True,
    )[:p3_cap]
    p4 = sorted(
        [o for o in opportunities if o["strategy"] == "P4_liquidity_timing"],
        key=lambda x: x["signal_strength"],
        reverse=True,
    )[:p4_cap]
    p5 = sorted(
        [o for o in opportunities if o["strategy"] == "P5_information_latency"],
        key=lambda x: x["signal_strength"],
        reverse=True,
    )[:p5_cap]
    opportunities = p2 + p3 + p4 + p5

    if opportunities:
        logger.info(
            "Found %d single-platform opportunities (from %d markets)",
            len(opportunities),
            len(markets),
        )

    for opp in opportunities:
        now = datetime.now(timezone.utc).isoformat()
        m = opp["market"]
        strategy = opp["strategy"]
        side = opp["side"]
        edge = opp["edge"]
        price = m["yes_price"]

        # Phase 2.3: Kelly-based sizing (replaces hardcoded min(10, 100*edge)).
        _kelly_f = compute_kelly_fraction(edge, 1.0, _risk_cfg.kelly_fraction)
        _bankroll = _risk_cfg.starting_capital
        _max_size = _bankroll * _risk_cfg.max_position_pct
        size = round(compute_position_size(_kelly_f, _bankroll, max_size=_max_size), 1)
        if size <= 0:
            logger.debug(
                "Kelly sizing zero for edge=%.4f strategy=%s — skip", edge, strategy
            )
            continue

        signal_id = f"sig_{uuid.uuid4().hex[:12]}"
        violation_id = f"viol_{uuid.uuid4().hex[:12]}"

        logger.info(
            "SINGLE-PLATFORM: %s | %s %s@%.3f spread=%.3f | %s | %s",
            strategy,
            side,
            m["platform"],
            price,
            m["spread"],
            m["title"][:50],
            m["id"][:25],
        )

        # Create self-referencing market pair for single-platform
        pair_id = f"sp_{m['id']}"
        try:
            await db.execute(
                """INSERT OR IGNORE INTO market_pairs
                   (id, market_id_a, market_id_b, pair_type, active, created_at, updated_at)
                   VALUES (?, ?, ?, 'single_platform', 1, ?, ?)""",
                (pair_id, m["id"], m["id"], now, now),
            )
        except Exception as e:
            logger.debug("Market pair insert error: %s", e)

        # Write violation
        try:
            await db.execute(
                """INSERT OR IGNORE INTO violations
                   (id, pair_id, violation_type, price_a_at_detect, price_b_at_detect,
                    raw_spread, net_spread, status, detected_at, updated_at)
                   VALUES (?, ?, 'single_platform', ?, ?, ?, ?, 'detected', ?, ?)""",
                (
                    violation_id,
                    pair_id,
                    price,
                    m["no_price"],
                    m["spread"],
                    edge,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Violation insert error: %s", e)

        # Write signal
        try:
            kelly = min(edge / 0.50, 0.25)
            await db.execute(
                """INSERT OR IGNORE INTO signals
                   (id, violation_id, strategy, signal_type, market_id_a,
                    model_edge, kelly_fraction, position_size_a,
                    total_capital_at_risk, status, fired_at, updated_at)
                   VALUES (?, ?, ?, 'single', ?, ?, ?, ?, ?, 'fired', ?, ?)""",
                (
                    signal_id,
                    violation_id,
                    strategy,
                    m["id"],
                    edge,
                    kelly,
                    size,
                    size,
                    now,
                    now,
                ),
            )
        except Exception as e:
            logger.debug("Signal insert error: %s", e)

        # Get (or create) execution client for this market's platform
        platform = m["platform"]
        if platform not in _clients:
            _clients[platform] = _make_single_execution_client(
                db, execution_mode, platform
            )

        leg = OrderLeg(
            market_id=m["id"],
            platform=platform,
            side=side,
            size=size,
            limit_price=price,
            order_type="LIMIT",
        )
        result = await _clients[platform].submit_order(
            leg, signal_id=signal_id, strategy=strategy
        )

        if result.filled_price:
            # Phase 4: open position, NO synthetic exit price.
            # mark_and_close_positions() will close it after holding_period_s
            # at the then-current market price, giving a realistic realized PnL.
            pos_id = f"pos_{uuid.uuid4().hex[:12]}"
            try:
                await db.execute(
                    """INSERT INTO positions
                       (id, signal_id, market_id, strategy, side, entry_price,
                        entry_size, fees_paid, pnl_model, status, opened_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'realistic', 'open', ?, ?)""",
                    (
                        pos_id,
                        signal_id,
                        m["id"],
                        strategy,
                        side,
                        result.filled_price,
                        size,
                        result.fee_paid or 0,
                        now,
                        now,
                    ),
                )
            except Exception as e:
                logger.debug("Position insert error: %s", e)

            trades.append(
                {
                    "strategy": strategy,
                    "market": f"{m['platform']}:{m['id'][:20]}",
                    "side": side,
                    "price": result.filled_price,
                    "edge": edge,
                    "pos_id": pos_id,
                }
            )

            logger.info(
                "  OPENED: %s@%.4f edge=%.4f | pos=%s (holding until close)",
                side,
                result.filled_price,
                edge,
                pos_id,
            )

            # Batch commit every 50 trades
            if len(trades) % 50 == 0:
                await db.commit()

    # Final commit
    await db.commit()
    return trades


# ── PnL Snapshot ─────────────────────────────────────────────────────────────

PAPER_CAPITAL = 10_000  # Default paper trading starting capital


async def take_trading_snapshot(db: aiosqlite.Connection) -> int | None:
    """
    Write a pnl_snapshots row and per-strategy strategy_pnl_snapshots rows.

    Computes metrics from trade_outcomes (the table paper trading writes to).
    This feeds the dashboard's overview cards, equity curve, strategy PnL chart,
    and risk metrics.
    """
    now = datetime.now(timezone.utc).isoformat()

    try:
        # ── Aggregate totals from trade_outcomes ──
        cursor = await db.execute("""SELECT
                   COALESCE(SUM(actual_pnl), 0) as realized_pnl,
                   COALESCE(SUM(fees_total), 0) as total_fees,
                   COUNT(*) as trade_count
               FROM trade_outcomes""")
        totals = await cursor.fetchone()
        realized_pnl_total = totals[0] if totals else 0
        fees_total = totals[1] if totals else 0
        total_capital = PAPER_CAPITAL + realized_pnl_total - fees_total
        cash = total_capital  # Paper trading has no open positions

        # ── Insert pnl_snapshots row ──
        cursor = await db.execute(
            """INSERT INTO pnl_snapshots (
                   snapshot_type, total_capital, cash,
                   open_positions_count, open_notional,
                   unrealized_pnl, realized_pnl_today, realized_pnl_total,
                   fees_today, fees_total,
                   pnl_constraint_arb, pnl_event_model, pnl_calibration,
                   pnl_liquidity, pnl_latency,
                   capital_polymarket, capital_kalshi, snapshotted_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "periodic",
                total_capital,
                cash,
                0,  # open_positions_count
                0.0,  # open_notional
                0.0,  # unrealized_pnl
                0.0,  # realized_pnl_today (we could compute this, but keeping simple)
                realized_pnl_total,
                0.0,  # fees_today
                fees_total,
                0.0,
                0.0,
                0.0,
                0.0,
                0.0,  # strategy-specific PnL columns
                total_capital * 0.6,  # capital_polymarket
                total_capital * 0.4,  # capital_kalshi
                now,
            ),
        )
        snapshot_id = cursor.lastrowid

        # ── Per-strategy breakdown → strategy_pnl_snapshots ──
        cursor = await db.execute("""SELECT
                   strategy,
                   COALESCE(SUM(actual_pnl), 0) as realized_pnl,
                   COALESCE(SUM(fees_total), 0) as fees,
                   COUNT(*) as trade_count,
                   SUM(CASE WHEN actual_pnl > 0 THEN 1 ELSE 0 END) as win_count
               FROM trade_outcomes
               GROUP BY strategy""")
        strategy_rows = await cursor.fetchall()

        for row in strategy_rows:
            await db.execute(
                """INSERT INTO strategy_pnl_snapshots
                       (snapshot_id, strategy, realized_pnl, unrealized_pnl, fees, trade_count, win_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_id, row[0], row[1], 0.0, row[2], row[3], row[4]),
            )

        await db.commit()
        logger.info(
            "Snapshot #%d: capital=$%.2f realized=$%.4f fees=$%.4f",
            snapshot_id,
            total_capital,
            realized_pnl_total,
            fees_total,
        )
        return snapshot_id

    except Exception as e:
        logger.error("Failed to take snapshot: %s", e)
        return None


# ── Analytics report ─────────────────────────────────────────────────────────


async def print_analytics(db: aiosqlite.Connection):
    """Print strategy-level analytics from the DB."""
    scorecard = StrategyScorecard(db)

    print("\n" + "=" * 70)
    print("  PAPER TRADING SESSION ANALYTICS")
    print("=" * 70)

    # Portfolio summary
    try:
        summary = await scorecard.get_portfolio_summary(days=1)
        if summary:
            print("\n  Portfolio Summary (last 24h)")
            for k, v in summary.items():
                if isinstance(v, float):
                    print(
                        f"    {k}: ${v:.4f}"
                        if "pnl" in k.lower()
                        else f"    {k}: {v:.4f}"
                    )
                else:
                    print(f"    {k}: {v}")
    except Exception as e:
        logger.debug("Portfolio summary error: %s", e)

    # Per-strategy breakdown
    print(
        f"\n  {'Strategy':<28} {'Trades':>7} {'Win%':>7} "
        f"{'PnL':>10} {'Sharpe':>8} {'Edge%':>8}"
    )
    print("  " + "-" * 68)

    for strategy in STRATEGIES:
        try:
            stats = await scorecard.get_strategy_summary(strategy, days=1)
            if stats and stats.get("total_trades", 0) > 0:
                print(
                    f"  {strategy:<28} {stats['total_trades']:>7} "
                    f"{stats.get('win_rate', 0):>6.1f}% "
                    f"${stats.get('total_pnl', 0):>9.4f} "
                    f"{stats.get('sharpe_ratio', 0):>7.2f} "
                    f"{stats.get('avg_edge_captured_pct', 0):>7.1f}%"
                )
        except Exception as e:
            logger.debug("No data for %s: %s", strategy, e)

    # Raw totals
    cursor = await db.execute("SELECT COUNT(*) FROM trade_outcomes")
    row = await cursor.fetchone()
    total = row[0] if row else 0

    cursor = await db.execute(
        "SELECT SUM(actual_pnl), SUM(fees_total) FROM trade_outcomes"
    )
    row = await cursor.fetchone()
    total_pnl = row[0] if row and row[0] else 0
    total_fees = row[1] if row and row[1] else 0

    # Matches and violations
    cursor = await db.execute(
        "SELECT COUNT(*) FROM markets WHERE platform = 'polymarket'"
    )
    poly_count = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM markets WHERE platform = 'kalshi'")
    kalshi_count = (await cursor.fetchone())[0]
    cursor = await db.execute("SELECT COUNT(*) FROM violations")
    viol_count = (await cursor.fetchone())[0]

    print(f"\n  Markets: {poly_count} Polymarket + {kalshi_count} Kalshi")
    print(f"  Violations detected: {viol_count}")
    print(f"  Trades executed: {total}")
    print(f"  Total PnL: ${total_pnl:.4f}")
    print(f"  Total fees: ${total_fees:.4f}")
    print(f"  Net: ${total_pnl:.4f}")
    print("=" * 70)


# ── Main ─────────────────────────────────────────────────────────────────────


async def refresh_markets_and_matches(db: aiosqlite.Connection, cfg) -> list[dict]:
    """Full fetch + store + match + persist. Run once, then use cached matches."""
    logger.info("=" * 50)
    logger.info("Fetching ALL markets from exchanges...")

    poly_task = asyncio.create_task(fetch_polymarket_markets())
    kalshi_task = asyncio.create_task(
        fetch_kalshi_markets(
            api_key=cfg.platform_credentials.kalshi_api_key,
            rsa_key_path=cfg.platform_credentials.kalshi_rsa_key_path,
            api_base=cfg.platform_credentials.kalshi_api_base,
        )
    )
    poly_markets, kalshi_markets = await asyncio.gather(poly_task, kalshi_task)

    if not poly_markets and not kalshi_markets:
        logger.warning("No markets fetched")
        return []

    await store_markets(db, poly_markets, kalshi_markets)

    matches = await find_matches(db)
    if matches:
        await persist_matches(db, matches)
    else:
        logger.info("No cross-platform matches found")

    return matches


async def run_trading_cycle(
    db: aiosqlite.Connection,
    matches: list[dict],
    min_spread: float,
    price_cache: dict | None = None,
) -> int:
    """Run one trading cycle using provided matches and optional live prices."""
    # If we have a live price cache from websockets, update match prices
    if price_cache:
        updated = 0
        for m in matches:
            poly_price = price_cache.get(m["poly_id"])
            kalshi_price = price_cache.get(m["kalshi_id"])
            if poly_price is not None:
                m["poly_price"] = poly_price
                updated += 1
            if kalshi_price is not None:
                m["kalshi_price"] = kalshi_price
                updated += 1
        if updated:
            logger.info("Updated %d prices from live websocket cache", updated)

    all_trades = []

    # 1. Cross-platform arbitrage
    if matches:
        cross_trades = await detect_violations_and_trade(
            db, matches, min_spread=min_spread
        )
        all_trades.extend(cross_trades)
    else:
        logger.info("No cross-platform matches to trade")

    # 2. Single-platform strategies
    single_trades = await detect_single_platform_opportunities(db, max_trades=20)
    all_trades.extend(single_trades)

    logger.info(
        "Cycle complete: %d trades (%d cross-platform, %d single-platform)",
        len(all_trades),
        len(all_trades) - len(single_trades),
        len(single_trades),
    )
    return len(all_trades)


async def take_phase0_baseline_snapshot(db: aiosqlite.Connection) -> None:
    """Write a baseline row to phase0_baseline (Phase 0.4).

    Captures pair count and per-strategy PnL at T0 so every later phase
    can compare against this reference. Safe to call multiple times — each
    call appends a new point-in-time row.
    """
    now = datetime.now(timezone.utc).isoformat()

    cursor = await db.execute("SELECT COUNT(*) FROM market_pairs")
    total_pairs = (await cursor.fetchone())[0]

    cursor = await db.execute("SELECT COUNT(*) FROM market_pairs WHERE active=1")
    active_pairs = (await cursor.fetchone())[0]

    pnl_by_strategy: dict[str, float] = {}
    cursor = await db.execute(
        "SELECT strategy, SUM(actual_pnl) FROM trade_outcomes GROUP BY strategy"
    )
    for row in await cursor.fetchall():
        pnl_by_strategy[row[0]] = row[1] or 0.0

    cursor = await db.execute("SELECT COUNT(*) FROM trade_outcomes")
    total_trade_count = (await cursor.fetchone())[0]
    total_realized_pnl = sum(pnl_by_strategy.values())

    await db.execute(
        """INSERT INTO phase0_baseline
           (snapshot_timestamp, pair_count, active_pair_count,
            p1_realized_pnl, p2_realized_pnl, p3_realized_pnl,
            p4_realized_pnl, p5_realized_pnl, total_realized_pnl,
            total_trade_count, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'phase0')""",
        (
            now,
            total_pairs,
            active_pairs,
            pnl_by_strategy.get("P1_cross_market_arb", 0.0),
            pnl_by_strategy.get("P2_structured_event", 0.0),
            pnl_by_strategy.get("P3_calibration_bias", 0.0),
            pnl_by_strategy.get("P4_liquidity_timing", 0.0),
            pnl_by_strategy.get("P5_information_latency", 0.0),
            total_realized_pnl,
            total_trade_count,
        ),
    )
    await db.commit()
    logger.info(
        "Phase 0 baseline: %d pairs (%d active) total_pnl=%.2f trades=%d",
        total_pairs,
        active_pairs,
        total_realized_pnl,
        total_trade_count,
    )


async def main():
    configure_from_env()

    parser = argparse.ArgumentParser(
        description="Paper trading session with real market data"
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force re-fetch all markets and re-match (slow, ~30s). "
        "Without this flag, uses cached matches from DB.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream prices via websocket. Arb trades fire instantly on "
        "price updates; scheduled strategies run on --interval.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run single batch cycle using cached matches and exit.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=120,
        help="Seconds between scheduled strategy cycles in stream mode (default: 120)",
    )
    parser.add_argument(
        "--min-spread",
        type=float,
        default=0.03,
        help="Minimum spread to trade (default: 0.03)",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Launch the analytics dashboard web server alongside trading.",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=8000,
        help="Port for the analytics dashboard (default: 8000)",
    )
    args = parser.parse_args()

    # Allow env var to override --min-spread without requiring a code change.
    # Set MIN_SPREAD_CROSS_PLATFORM=99.0 in .env to pause all P1 arb until
    # the matcher is fixed (Phase 0.3 / Phase 1).
    _env_min_spread = os.getenv("MIN_SPREAD_CROSS_PLATFORM")
    if _env_min_spread is not None:
        args.min_spread = float(_env_min_spread)
        logger.info(
            "  P1 min_spread overridden by MIN_SPREAD_CROSS_PLATFORM env: %.4f",
            args.min_spread,
        )

    cfg = get_config()

    # Phase 6: pre-live safety gate (no-op for paper/mock/shadow)
    from core.live_gate import check_live_gate

    check_live_gate(cfg.execution.execution_mode)

    mode = "stream" if args.stream else ("refresh+once" if args.refresh else "once")
    logger.info("Paper Trading Session")
    logger.info("  Mode: %s (execution: %s)", mode, cfg.execution.execution_mode)
    logger.info("  Database: %s", cfg.database.db_path)
    logger.info("  Min spread: %.4f", args.min_spread)
    if args.dashboard:
        logger.info("  Dashboard: http://127.0.0.1:%d", args.dashboard_port)

    db_wrapper = Database(
        cfg.database.db_path, migrations_dir=cfg.database.migrations_dir
    )
    await db_wrapper.init()
    db = db_wrapper._conn

    try:
        # ── Dashboard (start FIRST so it's available during refresh) ──
        dashboard_task = None
        if args.dashboard and args.stream:
            from scripts.dashboard_api import (
                create_dashboard_app,
                start_dashboard_server,
            )

            script_dir = Path(__file__).resolve().parent
            dist_dir = script_dir.parent / "dashboard" / "dist"
            static_dir = str(dist_dir) if dist_dir.is_dir() else None
            if not static_dir:
                logger.warning(
                    "dashboard/dist/ not found — run 'npm run build' in dashboard/. "
                    "API will still be available but no frontend."
                )
            dashboard_app = create_dashboard_app(
                db_path=cfg.database.db_path,
                static_dir=static_dir,
            )
            dashboard_task = asyncio.create_task(
                start_dashboard_server(
                    dashboard_app,
                    host="127.0.0.1",
                    port=args.dashboard_port,
                )
            )
            # Yield to let uvicorn bind before continuing
            await asyncio.sleep(0.5)
            logger.info(
                "Dashboard live at http://127.0.0.1:%d",
                args.dashboard_port,
            )

        # ── Step 1: Get matches (refresh or load from cache) ──
        if args.refresh:
            logger.info("Refreshing markets and matches...")
            matches = await refresh_markets_and_matches(db, cfg)
        else:
            matches = await load_cached_matches(db)
            if not matches:
                logger.info("No cached matches found — running initial refresh...")
                matches = await refresh_markets_and_matches(db, cfg)

        if not matches:
            logger.warning("No matches available. Run with --refresh to fetch markets.")
            await print_analytics(db)
            if dashboard_task:
                # Dashboard already running — just keep it alive
                logger.info(
                    "No matches, but dashboard is live at http://127.0.0.1:%d  (Ctrl-C to quit)",
                    args.dashboard_port,
                )
                try:
                    await dashboard_task
                except asyncio.CancelledError:
                    pass
            elif args.dashboard:
                from scripts.dashboard_api import (
                    create_dashboard_app,
                    start_dashboard_server,
                )

                script_dir = Path(__file__).resolve().parent
                dist_dir = script_dir.parent / "dashboard" / "dist"
                static_dir = str(dist_dir) if dist_dir.is_dir() else None
                dashboard_app = create_dashboard_app(
                    db_path=cfg.database.db_path,
                    static_dir=static_dir,
                )
                logger.info(
                    "No matches, but dashboard is live at http://127.0.0.1:%d  (Ctrl-C to quit)",
                    args.dashboard_port,
                )
                await start_dashboard_server(
                    dashboard_app, host="127.0.0.1", port=args.dashboard_port
                )
            return

        logger.info("Working with %d matched pairs", len(matches))

        # ── Step 2: Trade ──
        if args.stream:
            # ════════════════════════════════════════════════════════════
            #  STREAMING MODE
            #
            #  Two concurrent processes:
            #
            #  1. ArbitrageEngine (event-driven, low latency)
            #     - Websocket price ticks call arb_engine.on_price_update()
            #     - If any matched pair's spread exceeds threshold → trade NOW
            #     - Latency = websocket delivery + trade execution (~ms)
            #     - Position tracking prevents duplicate trades on same pair
            #
            #  2. ScheduledStrategyRunner (timer-based, relaxed)
            #     - Runs calibration bias, liquidity timing, etc.
            #     - Fires every --interval seconds (default 120)
            #     - These don't need to capture a fleeting spread
            # ════════════════════════════════════════════════════════════

            stop_event = asyncio.Event()

            # Take initial snapshot so dashboard has data immediately
            try:
                await take_trading_snapshot(db)
            except Exception as e:
                logger.warning("Initial snapshot failed (will retry later): %s", e)

            # Phase 2: instantiate risk config and circuit breaker alongside engine.
            # Phase 6: get_effective_risk_config selects tighter limits for live mode.
            from execution.circuit_breaker import DailyLossCircuitBreaker  # noqa: E402
            from core.live_gate import get_effective_risk_config  # noqa: E402
            from core.alerting import get_alert_manager  # noqa: E402

            execution_mode = cfg.execution.execution_mode
            risk_config = get_effective_risk_config(execution_mode)
            circuit_breaker = DailyLossCircuitBreaker(
                db=db,
                starting_capital=risk_config.starting_capital,
                max_daily_loss_pct=risk_config.max_daily_loss_pct,
                consecutive_failure_limit=risk_config.consecutive_failure_limit,
            )

            arb_engine = ArbitrageEngine(
                db,
                matches,
                min_spread=args.min_spread,
                risk_config=risk_config,
                circuit_breaker=circuit_breaker,
            )
            await arb_engine.initial_sweep()

            scheduled = ScheduledStrategyRunner(
                db,
                interval=args.interval,
                max_trades_per_cycle=20,
                risk_config=risk_config,
                circuit_breaker=circuit_breaker,
                alert_manager=get_alert_manager(),
            )

            # Build asset ID maps for websocket subscriptions
            # Polymarket WS needs platform_ids (condition_id), not our internal IDs
            poly_platform_ids = []
            poly_id_map: dict[str, str] = {}  # platform_id -> internal poly_XXX id
            kalshi_tickers = []

            for m in matches:
                kalshi_tickers.append(m["kalshi_id"].replace("kal_", ""))

            # Look up Polymarket platform_ids in bulk
            poly_internal_ids = list({m["poly_id"] for m in matches})
            for pid in poly_internal_ids:
                cursor = await db.execute(
                    "SELECT platform_id FROM markets WHERE id = ?", (pid,)
                )
                row = await cursor.fetchone()
                if row and row[0]:
                    poly_platform_ids.append(row[0])
                    poly_id_map[row[0]] = pid

            logger.info(
                "Starting streams: %d Polymarket assets + %d Kalshi tickers",
                len(poly_platform_ids),
                len(kalshi_tickers),
            )

            # Launch all concurrent tasks
            ws_tasks = []

            # Polymarket websocket → arb engine
            if poly_platform_ids:
                ws_tasks.append(
                    asyncio.create_task(
                        stream_prices_polymarket(
                            asset_ids=poly_platform_ids,
                            stop_event=stop_event,
                            on_price=arb_engine.on_price_update,
                            id_map=poly_id_map,
                        )
                    )
                )

            # Kalshi websocket → arb engine
            if kalshi_tickers:
                ws_tasks.append(
                    asyncio.create_task(
                        stream_prices_kalshi(
                            tickers=kalshi_tickers,
                            stop_event=stop_event,
                            on_price=arb_engine.on_price_update,
                            api_key=cfg.platform_credentials.kalshi_api_key,
                            rsa_key_path=cfg.platform_credentials.kalshi_rsa_key_path,
                        )
                    )
                )

            # Scheduled strategies (runs on timer)
            scheduled_task = asyncio.create_task(scheduled.run(stop_event))

            # Status logging + snapshot task (runs every 30s)
            async def _log_status():
                while not stop_event.is_set():
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=30)
                        break
                    except asyncio.TimeoutError:
                        stats = arb_engine.stats()
                        _last_fire = (
                            f"{time.time() - stats['last_arb_fired_at']:.0f}s ago"
                            if stats["last_arb_fired_at"]
                            else "never"
                        )
                        logger.info(
                            "STATUS: pairs=%d eligible=%d muted=%d pnl=$%.2f "
                            "prices=%d | last_fire=%s ticks_since=%d | scheduled=%d",
                            stats["pairs_monitored"],
                            stats["pairs_eligible_now"],
                            stats["recently_fired"],
                            stats["total_pnl"],
                            stats["prices_tracked"],
                            _last_fire,
                            stats["ticks_since_last_fire"],
                            scheduled.total_trades,
                        )
                        # Write dashboard snapshot every status cycle
                        try:
                            await take_trading_snapshot(db)
                        except Exception as snap_err:
                            logger.debug("Snapshot failed: %s", snap_err)

            status_task = asyncio.create_task(_log_status())

            # Gather all long-running tasks. The dashboard task is included
            # so the process stays alive even if websockets disconnect.
            all_tasks = [
                *ws_tasks,
                scheduled_task,
                status_task,
                *([dashboard_task] if dashboard_task else []),
            ]

            try:
                await asyncio.gather(*all_tasks, return_exceptions=True)
            except KeyboardInterrupt:
                logger.info("Shutting down...")
            finally:
                stop_event.set()
                await arb_engine.flush()
                for t in all_tasks:
                    t.cancel()
                await asyncio.gather(*all_tasks, return_exceptions=True)
                total_arb = len(arb_engine.trades)
                total_sched = scheduled.total_trades
                logger.info(
                    "Final: %d arb trades + %d scheduled trades = %d total",
                    total_arb,
                    total_sched,
                    total_arb + total_sched,
                )

        else:
            # ════════════════════════════════════════════════════════════
            #  BATCH MODE (--once or --refresh)
            #
            #  Runs all strategies once using stored prices, then exits.
            #  Good for testing or one-off analysis.
            # ════════════════════════════════════════════════════════════
            await run_trading_cycle(db, matches, args.min_spread)
            try:
                await take_trading_snapshot(db)
            except Exception as e:
                logger.warning("Post-batch snapshot failed: %s", e)

        await print_analytics(db)

        # If --dashboard was passed in batch mode, keep serving until Ctrl-C
        if args.dashboard and not args.stream:
            from scripts.dashboard_api import (
                create_dashboard_app,
                start_dashboard_server,
            )

            script_dir = Path(__file__).resolve().parent
            dist_dir = script_dir.parent / "dashboard" / "dist"
            static_dir = str(dist_dir) if dist_dir.is_dir() else None
            dashboard_app = create_dashboard_app(
                db_path=cfg.database.db_path,
                static_dir=static_dir,
            )
            logger.info(
                "Batch complete. Dashboard at http://127.0.0.1:%d  (Ctrl-C to quit)",
                args.dashboard_port,
            )
            await start_dashboard_server(
                dashboard_app, host="127.0.0.1", port=args.dashboard_port
            )

    finally:
        await db_wrapper.close()
        logger.info("Session complete. DB: %s", cfg.database.db_path)


if __name__ == "__main__":
    asyncio.run(main())
