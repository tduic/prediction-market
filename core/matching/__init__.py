"""Matching layer for prediction market pair discovery."""

from core.matching.curator import MarketPairCurator
from core.matching.embedder import MarketEmbedder
from core.matching.rules import match_by_rules

__all__ = ["MarketPairCurator", "MarketEmbedder", "match_by_rules"]
