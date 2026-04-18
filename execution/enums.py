"""
Strongly-typed enums for execution-layer side and book selection.

These replace stringly-typed `side` / `book` values across the system.
Inheriting from `str` preserves DB-string compatibility (e.g.,
``positions.side = 'BUY'``, ``orders.book = 'YES'``) with zero data
migration — ``"BUY" == Side.BUY`` still holds at serialization boundaries.
"""

from enum import Enum


class Side(str, Enum):
    """Trading side at the strategy layer."""

    BUY = "BUY"
    SELL = "SELL"


class Book(str, Enum):
    """Which CLOB order book a Polymarket order hit.

    Polymarket markets have two independent books — one per outcome token.
    Kalshi markets only have a YES book, so Kalshi orders are always
    ``Book.YES``.
    """

    YES = "YES"
    NO = "NO"
