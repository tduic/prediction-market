"""Tests for core/engine/fees.py — venue taker-fee curves and arb economics."""

import pytest

from core.engine.fees import (
    FeeParams,
    arb_economics,
    kalshi_params,
    polymarket_params,
    taker_fee,
    unit_net_edge,
)


class TestKalshiFee:
    def test_hundred_contracts_at_even_odds(self):
        # 0.07 * 100 * 0.5 * 0.5 = 1.75 exactly — float noise must not bump a cent
        assert taker_fee(kalshi_params(), 100, 0.50) == 1.75

    def test_rounds_up_to_the_cent(self):
        # 0.07 * 1 * 0.25 = 0.0175 → 0.02
        assert taker_fee(kalshi_params(), 1, 0.50) == 0.02

    def test_series_multiplier(self):
        # 0.035 * 10 * 0.1 * 0.9 = 0.0315 → 0.04
        assert taker_fee(kalshi_params(multiplier=0.5), 10, 0.10) == 0.04

    def test_zero_multiplier_is_free(self):
        assert taker_fee(kalshi_params(multiplier=0.0), 10, 0.40) == 0.0


class TestPolymarketFee:
    def test_rate_and_exponent_one(self):
        # 10 * 0.05 * (0.4*0.6)^1 = 0.12
        assert taker_fee(polymarket_params(0.05, 1.0), 10, 0.40) == 0.12

    def test_fee_free_market(self):
        assert taker_fee(polymarket_params(0.0, 1.0), 10, 0.40) == 0.0

    def test_exponent_zero_is_kept(self):
        # (p(1-p))^0 == 1 → fee = q * r. The fork coerced e=0 to 1 (4x+ under).
        assert taker_fee(polymarket_params(0.03, 0.0), 10, 0.40) == 0.3

    def test_five_decimal_precision(self):
        # 3 * 0.03 * 0.2275 = 0.020475 → rounds up at 5 decimals
        assert taker_fee(polymarket_params(0.03, 1.0), 3, 0.35) == 0.02048


class TestValidation:
    @pytest.mark.parametrize("price", [0.0, 1.0, -0.1, 1.2])
    def test_price_outside_open_interval(self, price):
        with pytest.raises(ValueError):
            taker_fee(kalshi_params(), 10, price)

    def test_non_positive_quantity(self):
        with pytest.raises(ValueError):
            taker_fee(kalshi_params(), 0, 0.5)

    def test_negative_rate(self):
        with pytest.raises(ValueError):
            taker_fee(FeeParams("kalshi", rate=-0.01), 10, 0.5)

    @pytest.mark.parametrize("exponent", [-1.0, 9.0])
    def test_exponent_bounds(self, exponent):
        with pytest.raises(ValueError):
            taker_fee(FeeParams("polymarket", rate=0.05, exponent=exponent), 1, 0.5)


class TestArbEconomics:
    def test_fees_can_eat_the_gross_edge(self):
        econ = arb_economics(0.49, 0.51, 100, kalshi_params(), kalshi_params())
        assert econ is not None
        assert econ.gross == pytest.approx(2.0)
        assert econ.buy_fee == 1.75 and econ.sell_fee == 1.75
        assert econ.net < 0

    def test_profitable_after_fees(self):
        poly = polymarket_params(0.0, 1.0)
        econ = arb_economics(0.40, 0.50, 10, poly, kalshi_params())
        assert econ is not None
        # gross 1.00; kalshi sell fee ceil(0.07*10*0.25)=0.18
        assert econ.sell_fee == 0.18
        assert econ.net == pytest.approx(0.82)
        assert econ.net_edge_per_contract == pytest.approx(0.082)

    @pytest.mark.parametrize(
        "buy,sell,qty", [(0.0, 0.5, 10), (0.4, 1.0, 10), (0.4, 0.5, 0)]
    )
    def test_invalid_inputs_return_none(self, buy, sell, qty):
        assert arb_economics(buy, sell, qty, kalshi_params(), kalshi_params()) is None

    def test_unit_net_edge_is_unrounded(self):
        edge = unit_net_edge(0.40, 0.50, polymarket_params(0.0, 1.0), kalshi_params())
        # 0.10 - 0.07*0.25 = 0.0825
        assert edge == pytest.approx(0.0825)
