"""Tests for Config._validate() — newly-added boundary checks."""

import pytest

from core.config import Config, RiskControlConfig


def _make_config(**overrides) -> Config:
    """Build a Config with RiskControlConfig field overrides."""
    return Config(risk_controls=RiskControlConfig(**overrides))


class TestStrategyReplayCooldown:
    def test_default_is_valid(self):
        assert RiskControlConfig().strategy_replay_cooldown_s >= 0

    def test_zero_is_valid(self):
        _make_config(strategy_replay_cooldown_s=0)

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="STRATEGY_REPLAY_COOLDOWN_S"):
            _make_config(strategy_replay_cooldown_s=-1)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("STRATEGY_REPLAY_COOLDOWN_S", "600")
        assert RiskControlConfig().strategy_replay_cooldown_s == 600


class TestArbPretradeLookupTimeout:
    def test_default_is_valid(self):
        assert RiskControlConfig().arb_pretrade_lookup_timeout_s > 0

    def test_positive_is_valid(self):
        _make_config(arb_pretrade_lookup_timeout_s=0.1)

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="ARB_PRETRADE_LOOKUP_TIMEOUT_S"):
            _make_config(arb_pretrade_lookup_timeout_s=0.0)

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="ARB_PRETRADE_LOOKUP_TIMEOUT_S"):
            _make_config(arb_pretrade_lookup_timeout_s=-1.0)


class TestArbMinFillSize:
    def test_default_is_valid(self):
        assert RiskControlConfig().arb_min_fill_size > 0

    def test_positive_is_valid(self):
        _make_config(arb_min_fill_size=0.01)

    def test_zero_raises(self):
        with pytest.raises(ValueError, match="ARB_MIN_FILL_SIZE"):
            _make_config(arb_min_fill_size=0.0)

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="ARB_MIN_FILL_SIZE"):
            _make_config(arb_min_fill_size=-1.0)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ARB_MIN_FILL_SIZE", "5")
        assert RiskControlConfig().arb_min_fill_size == 5.0
