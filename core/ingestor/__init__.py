"""Ingestor module for polling prediction market data."""

from core.ingestor.external import (
    BLSCalendarFetcher,
    ClevelandFedScraper,
    CMEFedWatchScraper,
    MetaculusClient,
)
from core.ingestor.kalshi import KalshiClient
from core.ingestor.polymarket import PolymarketClient
from core.ingestor.scheduler import IngestorScheduler

__all__ = [
    "PolymarketClient",
    "KalshiClient",
    "CMEFedWatchScraper",
    "ClevelandFedScraper",
    "MetaculusClient",
    "BLSCalendarFetcher",
    "IngestorScheduler",
]
