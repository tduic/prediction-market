"""Tests for core/signals/sizing.py (the production Kelly sizing functions)."""

import pytest

from core.signals.sizing import compute_kelly_fraction, compute_position_size


class TestKellyFraction:
    def test_long_edge_is_quarter_kelly(self):
        # raw = 0.10 / 0.90; quarter-Kelly
        assert compute_kelly_fraction(0.10) == pytest.approx(0.10 / 0.90 * 0.25)

    def test_short_edge_is_negative(self):
        assert compute_kelly_fraction(-0.10) == pytest.approx(-0.10 / 0.90 * 0.25)

    def test_zero_edge_bets_nothing(self):
        assert compute_kelly_fraction(0.0) == 0.0

    def test_custom_fraction(self):
        assert compute_kelly_fraction(0.10, kelly_fraction=1.0) == pytest.approx(
            0.10 / 0.90
        )

    @pytest.mark.parametrize("edge", [0.9, 0.99, 1.0, 5.0])
    def test_capped_at_half(self, edge):
        assert compute_kelly_fraction(edge, kelly_fraction=1.0) <= 0.5

    @pytest.mark.parametrize("edge", [-0.9, -1.0, -5.0])
    def test_short_capped_at_minus_half(self, edge):
        assert compute_kelly_fraction(edge, kelly_fraction=1.0) >= -0.5


class TestPositionSize:
    def test_fraction_of_bankroll(self):
        assert compute_position_size(0.05, 10_000, max_size=1_000) == pytest.approx(500)

    def test_capped_at_max_size(self):
        assert compute_position_size(0.5, 10_000, max_size=500) == 500

    def test_short_fraction_sizes_by_magnitude(self):
        assert compute_position_size(-0.05, 10_000, max_size=1_000) == pytest.approx(
            500
        )

    @pytest.mark.parametrize("bankroll", [0, -100])
    def test_non_positive_bankroll_sizes_zero(self, bankroll):
        assert compute_position_size(0.1, bankroll) == 0.0
