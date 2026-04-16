"""
Execution client factory functions.

Provides _make_execution_clients and _make_single_execution_client which
dispatch to the appropriate live or paper execution client based on the
execution_mode parameter.
"""

import logging

logger = logging.getLogger(__name__)


def _make_execution_clients(db, execution_mode: str):
    """
    Return (poly_client, kalshi_client) for the given execution mode.

    - "live"            → real PolymarketExecutionClient + KalshiExecutionClient
    - "paper"/"shadow"  → PaperExecutionClient (simulated fills, no real orders)

    Shadow mode uses paper clients by design: full signal/risk pipeline runs
    but no real orders are submitted.
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
