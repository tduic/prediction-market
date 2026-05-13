"""Ingestor module for prediction market data.

Clients are intentionally not exported at package level — they carry heavy
transitive dependencies (cryptography, etc.) that break in environments where
those libraries are unavailable (CI, tests without credentials).  Import them
directly from their submodules where needed:

    from core.ingestor.kalshi import KalshiClient
    from core.ingestor.polymarket import PolymarketClient
"""
