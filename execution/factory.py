"""
Execution client factory functions.

Provides _make_execution_clients and _make_single_execution_client which
dispatch to the appropriate live or paper execution client based on the
execution_mode parameter.
"""

import logging
import os

from execution.clients.base import BaseExecutionClient

logger = logging.getLogger(__name__)


def _make_polymarket_client(db) -> BaseExecutionClient:
    """Live Polymarket client: CLOB V2 by default.

    ``POLYMARKET_CLIENT=legacy`` falls back to the archived py-clob-client
    implementation.
    """
    choice = os.getenv("POLYMARKET_CLIENT", "v2").strip().lower()
    if choice == "v2":
        from execution.clients.polymarket_v2 import PolymarketExecutionClientV2

        return PolymarketExecutionClientV2(db)
    if choice == "legacy":
        from execution.clients.polymarket import PolymarketExecutionClient

        logger.warning("Using legacy py-clob-client (POLYMARKET_CLIENT=legacy)")
        return PolymarketExecutionClient(db)
    raise ValueError(f"POLYMARKET_CLIENT must be 'v2' or 'legacy', got {choice!r}")


def _make_execution_clients(
    db, execution_mode: str
) -> tuple[BaseExecutionClient, BaseExecutionClient]:
    """
    Return (poly_client, kalshi_client) for the given execution mode.

    - "live"            → Polymarket (CLOB V2, see _make_polymarket_client)
                          + KalshiExecutionClient
    - "paper"/"shadow"  → PaperExecutionClient (simulated fills, no real orders)

    Shadow mode uses paper clients by design: full signal/risk pipeline runs
    but no real orders are submitted.
    """
    poly_client: BaseExecutionClient
    kalshi_client: BaseExecutionClient
    if execution_mode == "live":
        from execution.clients.kalshi import KalshiExecutionClient

        poly_client = _make_polymarket_client(db)
        kalshi_client = KalshiExecutionClient(db)
        logger.info("Execution clients: LIVE (Polymarket + Kalshi)")
    else:
        from execution.clients.paper import PaperExecutionClient

        poly_client = PaperExecutionClient(db, platform_label="polymarket")
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
            return _make_polymarket_client(db)
        else:
            from execution.clients.kalshi import KalshiExecutionClient

            return KalshiExecutionClient(db)
    else:
        from execution.clients.paper import PaperExecutionClient

        return PaperExecutionClient(db, platform_label=f"paper_{platform}")
