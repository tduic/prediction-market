"""Shared rule for turning a bid/ask quote into a usable mid price.

Both exchanges show stub books (0.001 / 0.999, 0.02 / 0.98) and one-sided
books (bid 0). Their "mid" is not a price anyone can trade, and feeding it to
matching or the arb engine creates phantom spreads. Every ingest path (REST
listing, Kalshi REST client, both websocket streams) uses this one check.
"""

# Widest YES bid/ask spread whose mid is still treated as a price.
MAX_MID_SPREAD = 0.20
_EPS = 1e-9


def two_sided_mid(bid: float | None, ask: float | None) -> float | None:
    """Mid of a two-sided, uncrossed book no wider than MAX_MID_SPREAD.

    Returns None for missing/zero/one-sided, crossed, out-of-range or too
    wide books.
    """
    if bid is None or ask is None:
        return None
    if not (0.0 < bid <= ask < 1.0):
        return None
    if ask - bid > MAX_MID_SPREAD + _EPS:
        return None
    return (bid + ask) / 2.0
