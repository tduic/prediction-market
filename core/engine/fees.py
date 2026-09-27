"""Venue taker-fee curves and cross-platform arb economics.

Both venues charge a price-dependent taker fee of the form

    fee = ceil_to_precision(quantity * rate * (p * (1 - p)) ** exponent)

- Kalshi: rate = 0.07 * series fee_multiplier, exponent 1, rounded UP to the
  cent. The multiplier comes from the series (or an event override).
- Polymarket CLOB V2: rate ``fd.r`` and exponent ``fd.e`` from
  ``/clob-markets/{condition_id}``, rounded up at 5 decimals. A market with
  no ``fd`` is fee-free (rate 0), matching the official SDK.

The price term is symmetric in p, so a SELL YES at p and the translated
Polymarket BUY NO at 1 - p cost the same fee.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

KALSHI_BASE_RATE = 0.07


@dataclass(frozen=True)
class FeeParams:
    platform: str
    rate: float
    exponent: float = 1.0
    decimals: int = 2


def kalshi_params(multiplier: float = 1.0, base_rate: float = KALSHI_BASE_RATE):
    return FeeParams("kalshi", rate=base_rate * multiplier, exponent=1.0, decimals=2)


def polymarket_params(rate: float, exponent: float) -> FeeParams:
    return FeeParams("polymarket", rate=rate, exponent=exponent, decimals=5)


def _price_term(params: FeeParams, price: float) -> float:
    if not 0.0 < price < 1.0:
        raise ValueError(f"price must be in (0, 1), got {price}")
    if params.rate < 0:
        raise ValueError(f"fee rate must be non-negative, got {params.rate}")
    if not 0.0 <= params.exponent <= 8.0:
        raise ValueError(f"fee exponent must be in [0, 8], got {params.exponent}")
    return params.rate * (price * (1.0 - price)) ** params.exponent


def taker_fee(params: FeeParams, quantity: float, price: float) -> float:
    """Fee for ``quantity`` contracts at ``price``, rounded up to venue precision."""
    if quantity <= 0:
        raise ValueError(f"quantity must be positive, got {quantity}")
    raw = quantity * _price_term(params, price)
    scale = 10**params.decimals
    # round() first so float noise (1.7500000000000002) can't bump a whole unit.
    return math.ceil(round(raw * scale, 9)) / scale


def unit_net_edge(
    buy_price: float, sell_price: float, buy_fees: FeeParams, sell_fees: FeeParams
) -> float:
    """Per-contract edge after fees, before venue rounding (used for sizing)."""
    return (
        sell_price
        - buy_price
        - _price_term(buy_fees, buy_price)
        - _price_term(sell_fees, sell_price)
    )


@dataclass(frozen=True)
class ArbEconomics:
    quantity: float
    buy_price: float
    sell_price: float
    gross: float
    buy_fee: float
    sell_fee: float
    net: float
    net_edge_per_contract: float


def arb_economics(
    buy_price: float,
    sell_price: float,
    quantity: float,
    buy_fees: FeeParams,
    sell_fees: FeeParams,
) -> ArbEconomics | None:
    """Exact net P&L of buying YES at buy_price and selling YES at sell_price."""
    try:
        buy_fee = taker_fee(buy_fees, quantity, buy_price)
        sell_fee = taker_fee(sell_fees, quantity, sell_price)
    except ValueError:
        return None
    gross = (sell_price - buy_price) * quantity
    net = gross - buy_fee - sell_fee
    return ArbEconomics(
        quantity=quantity,
        buy_price=buy_price,
        sell_price=sell_price,
        gross=gross,
        buy_fee=buy_fee,
        sell_fee=sell_fee,
        net=net,
        net_edge_per_contract=net / quantity,
    )
