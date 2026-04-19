"""
Tests for ``core.ingestor.polymarket.PolymarketClient._parse_market``.

The paper execution client's live price lookup routes through this parser.
Regressions here silently degrade to stale DB prices — which is exactly
what was masking arb-engine stale-price fires for Polymarket before.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.ingestor.polymarket import PolymarketClient  # noqa: E402


def _client() -> PolymarketClient:
    return PolymarketClient(api_key="x", api_secret="y")


def test_parse_uses_tokens_price_when_lastprice_null():
    """CLOB `/markets/{conditionId}` returns lastPrice=None; the YES price
    must come from tokens[0].price (0.535 in this example)."""
    item = {
        "condition_id": "0xabc",
        "question": "Will X happen?",
        "lastPrice": None,
        "tokens": [
            {"token_id": "t1", "outcome": "Yes", "price": 0.535},
            {"token_id": "t2", "outcome": "No", "price": 0.465},
        ],
    }
    md = _client()._parse_market(item)
    assert md is not None
    assert md.market_id == "0xabc"
    assert md.last_price == 0.535


def test_parse_prefers_explicit_lastprice_when_present():
    """Gamma-style payloads provide lastPrice directly; don't clobber it."""
    item = {
        "conditionId": "0xcamel",
        "question": "q",
        "lastPrice": "0.60",
        "tokens": [{"price": 0.50}],
    }
    md = _client()._parse_market(item)
    assert md is not None
    assert md.last_price == 0.60


def test_parse_accepts_camelcase_condition_id():
    item = {
        "conditionId": "0xcamel",
        "question": "q",
        "tokens": [{"price": 0.5}],
    }
    md = _client()._parse_market(item)
    assert md is not None
    assert md.market_id == "0xcamel"


def test_parse_returns_zero_price_when_nothing_usable():
    """No lastPrice, no tokens — parser still returns a MarketData with
    last_price=0 so callers can decide to reject (rather than crash)."""
    item = {"conditionId": "0xbare", "question": "q"}
    md = _client()._parse_market(item)
    assert md is not None
    assert md.last_price == 0.0


def test_parse_extracts_no_token_price_from_tokens_array():
    """CLOB tokens[1] is the NO token; its price must land on
    MarketData.last_price_no so paper can read it for translated fills."""
    item = {
        "condition_id": "0xabc",
        "question": "Will X happen?",
        "lastPrice": None,
        "tokens": [
            {"token_id": "t1", "outcome": "Yes", "price": 0.535},
            {"token_id": "t2", "outcome": "No", "price": 0.465},
        ],
    }
    md = _client()._parse_market(item)
    assert md is not None
    assert md.last_price == 0.535
    assert md.last_price_no == 0.465


def test_parse_no_price_none_when_no_second_token():
    item = {
        "conditionId": "0xone",
        "question": "q",
        "tokens": [{"price": 0.5}],  # only YES token, no NO
    }
    md = _client()._parse_market(item)
    assert md is not None
    assert md.last_price_no is None


def test_parse_no_price_none_when_tokens_missing():
    item = {"conditionId": "0xbare", "question": "q"}
    md = _client()._parse_market(item)
    assert md is not None
    assert md.last_price_no is None
