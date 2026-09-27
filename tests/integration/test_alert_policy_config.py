"""Regression coverage for non-secret alert-policy configuration."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.config import RiskControlConfig


def test_alert_policy_defaults_are_documented_values(monkeypatch):
    for name in (
        "AGED_POSITION_ALERT_THRESHOLD_S",
        "EXECUTION_FAILURE_ALERT_COUNT",
        "EXECUTION_FAILURE_ALERT_WINDOW_S",
    ):
        monkeypatch.delenv(name, raising=False)

    config = RiskControlConfig()
    assert config.aged_position_alert_threshold_s == 72 * 3600
    assert config.execution_failure_alert_count == 3
    assert config.execution_failure_alert_window_s == 10 * 60


@pytest.mark.parametrize(
    ("name", "value", "attribute", "default"),
    [
        ("AGED_POSITION_ALERT_THRESHOLD_S", "invalid", "aged_position_alert_threshold_s", 259200),
        ("AGED_POSITION_ALERT_THRESHOLD_S", "0", "aged_position_alert_threshold_s", 259200),
        ("AGED_POSITION_ALERT_THRESHOLD_S", "31536001", "aged_position_alert_threshold_s", 259200),
        ("EXECUTION_FAILURE_ALERT_COUNT", "-1", "execution_failure_alert_count", 3),
        ("EXECUTION_FAILURE_ALERT_COUNT", "1001", "execution_failure_alert_count", 3),
        ("EXECUTION_FAILURE_ALERT_WINDOW_S", "0", "execution_failure_alert_window_s", 600),
        ("EXECUTION_FAILURE_ALERT_WINDOW_S", "86401", "execution_failure_alert_window_s", 600),
    ],
)
def test_invalid_alert_policy_values_fall_back_safely(
    monkeypatch, name, value, attribute, default
):
    monkeypatch.setenv(name, value)
    assert getattr(RiskControlConfig(), attribute) == default


def test_valid_alert_policy_values_override_defaults(monkeypatch):
    monkeypatch.setenv("AGED_POSITION_ALERT_THRESHOLD_S", "3600")
    monkeypatch.setenv("EXECUTION_FAILURE_ALERT_COUNT", "7")
    monkeypatch.setenv("EXECUTION_FAILURE_ALERT_WINDOW_S", "120")

    config = RiskControlConfig()
    assert config.aged_position_alert_threshold_s == 3600
    assert config.execution_failure_alert_count == 7
    assert config.execution_failure_alert_window_s == 120
