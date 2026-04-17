"""Ingestor module for prediction market data."""

from core.ingestor.kalshi import KalshiClient
from core.ingestor.polymarket import PolymarketClient

__all__ = [
    "PolymarketClient",
    "KalshiClient",
]
