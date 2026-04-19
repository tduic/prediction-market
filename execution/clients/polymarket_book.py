"""
BookResolver — encodes Polymarket's no-naked-shorts rule.

The CLOB does not allow a SELL on a token we don't hold. To express
short exposure without inventory, the only legal operation is a BUY
on the opposing token's book at the complementary price:
``BUY NO @ 1 − p`` substitutes for ``SELL YES @ p``.

Rule (B1 — no NO-book price comparison):
    side=BUY                              → (yes_token, BUY, p,   YES, False)
    side=SELL, YES inventory ≥ size       → (yes_token, SELL, p,  YES, False)
    side=SELL, YES inventory < size       → (no_token,  BUY, 1−p, NO,  True)

Returns None for unresolvable requests; the caller writes a failed
order with a clear error message.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import aiosqlite

from execution.enums import Book, Side

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedOrder:
    token_id: str
    side: Side
    limit_price: float
    size: float
    book: Book
    translated: bool


class BookResolver:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    async def resolve(
        self,
        market_id: str,
        side: Side,
        size: float,
        limit_price: float | None,
    ) -> ResolvedOrder | None:
        if not self._valid_size(size) or not self._valid_price(limit_price):
            return None

        tokens = await self._tokens(market_id)
        if tokens is None:
            logger.warning("BookResolver: no market row for %s", market_id)
            return None
        yes_tok, no_tok = tokens
        if yes_tok is None:
            logger.warning(
                "BookResolver: %s has no yes_token_id; cannot route BUY or YES-SELL",
                market_id,
            )
            return None

        if side is Side.BUY:
            return ResolvedOrder(
                token_id=yes_tok,
                side=Side.BUY,
                limit_price=float(limit_price),  # already validated
                size=size,
                book=Book.YES,
                translated=False,
            )

        # side is SELL from here on.
        inventory = await self._yes_inventory(market_id)
        if inventory >= size:
            return ResolvedOrder(
                token_id=yes_tok,
                side=Side.SELL,
                limit_price=float(limit_price),
                size=size,
                book=Book.YES,
                translated=False,
            )

        # No inventory → translate to BUY NO.
        if not self._translation_enabled():
            logger.warning(
                "BookResolver: short translation disabled for %s (kill-switch)",
                market_id,
            )
            return None
        if no_tok is None:
            logger.warning(
                "BookResolver: cannot short %s; no_token_id missing", market_id
            )
            return None

        return ResolvedOrder(
            token_id=no_tok,
            side=Side.BUY,
            limit_price=round(1.0 - float(limit_price), 4),
            size=size,
            book=Book.NO,
            translated=True,
        )

    @staticmethod
    def _valid_size(size: float) -> bool:
        try:
            return size > 0
        except TypeError:
            return False

    @staticmethod
    def _valid_price(price: float | None) -> bool:
        if price is None:
            return False
        try:
            return 0 < float(price) < 1
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _translation_enabled() -> bool:
        return os.getenv("POLYMARKET_ALLOW_SHORT_TRANSLATION", "true").lower() != "false"

    async def _tokens(self, market_id: str) -> tuple[str | None, str | None] | None:
        cur = await self.db.execute(
            "SELECT yes_token_id, no_token_id FROM markets WHERE id = ?",
            (market_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        return (row[0], row[1])

    async def _yes_inventory(self, market_id: str) -> float:
        cur = await self.db.execute(
            """
            SELECT COALESCE(SUM(entry_size - COALESCE(exit_size, 0)), 0)
            FROM positions
            WHERE market_id = ? AND side = 'BUY' AND status = 'open'
              AND book = 'YES'
            """,
            (market_id,),
        )
        row = await cur.fetchone()
        return float(row[0] or 0.0)

    # TODO[B2]: price-aware NO-book routing. When we have a live NO-book
    # price feed, compare (YES best-bid) vs (1 - NO best-ask) and pick the
    # better fill even when we have YES inventory. Requires NO-token WS
    # subscription + staleness tracking mirroring the YES-side work in v2.4.0.
