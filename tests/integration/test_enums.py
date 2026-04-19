"""Tests for execution.enums — Side and Book string-backed enums."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution.enums import Book, Side  # noqa: E402


def test_side_values_are_uppercase_strings():
    assert Side.BUY.value == "BUY"
    assert Side.SELL.value == "SELL"


def test_book_values_are_uppercase_strings():
    assert Book.YES.value == "YES"
    assert Book.NO.value == "NO"


def test_side_equals_string_at_boundary():
    # str inheritance means enum members equal their string value.
    assert Side.BUY == "BUY"
    assert "SELL" == Side.SELL


def test_book_equals_string_at_boundary():
    assert Book.YES == "YES"
    assert "NO" == Book.NO


def test_side_roundtrip_from_string():
    assert Side("BUY") is Side.BUY
    assert Side("SELL") is Side.SELL


def test_book_roundtrip_from_string():
    assert Book("YES") is Book.YES
    assert Book("NO") is Book.NO


def test_order_leg_accepts_side_enum():
    from execution.models import OrderLeg

    leg = OrderLeg(
        market_id="m1", platform="polymarket",
        side=Side.BUY, size=10, limit_price=0.5,
    )
    assert leg.side is Side.BUY


def test_order_leg_coerces_string_side_to_enum():
    from execution.models import OrderLeg

    leg = OrderLeg(
        market_id="m1", platform="polymarket",
        side="SELL", size=10, limit_price=0.5,
    )
    assert leg.side is Side.SELL
