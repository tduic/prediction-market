"""
Inverted-index matching engine for cross-platform market matching.

At 30k × 30k markets, brute-force O(n²) is 900M comparisons — way too slow.
Instead we use a blocking/candidate-generation strategy:

1. Normalize titles, extract tokens (lowercased, stop-filtered, synonym-expanded)
2. Build inverted index: token → set of market IDs (one index per platform)
3. For each Polymarket market, find Kalshi candidates that share ≥2 tokens
4. Score only those candidates using multi-signal similarity
5. Take 1:1 best matches above threshold

This reduces to O(n × avg_candidates) which is typically O(n × 10-50).
"""

import asyncio
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

import aiosqlite

logger = logging.getLogger(__name__)

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
