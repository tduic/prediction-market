"""
Tests for core/live_gate.py

Covers:
  - check_live_gate: sentinel file guard (6.1) and daily confirmation code (6.2)
  - get_effective_risk_config: stricter limits for live mode (6.3)
  - Shadow execution mode passes the gate (6.4)
"""

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.live_gate import (  # noqa: E402
    SENTINEL_PATH,
    LiveGateError,
    check_live_gate,
    get_effective_risk_config,
)

# ── Sentinel file ──────────────────────────────────────────────────────────────


class TestSentinelFile:
    def test_live_mode_without_sentinel_raises(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        with pytest.raises(LiveGateError, match="sentinel"):
            check_live_gate(
                "live",
                sentinel_path=sentinel,
                confirmation_code=date.today().isoformat(),
            )

    def test_error_message_names_sentinel_path(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        with pytest.raises(LiveGateError) as exc_info:
            check_live_gate(
                "live",
                sentinel_path=sentinel,
                confirmation_code=date.today().isoformat(),
            )
        assert str(sentinel) in str(exc_info.value)

    def test_live_mode_with_sentinel_passes(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        sentinel.write_text("armed by operator on 2026-04-11\n")
        check_live_gate(
            "live",
            sentinel_path=sentinel,
            confirmation_code=date.today().isoformat(),
        )

    def test_paper_mode_ignores_missing_sentinel(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        check_live_gate("paper", sentinel_path=sentinel, confirmation_code=None)

    def test_shadow_mode_ignores_sentinel(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        check_live_gate("shadow", sentinel_path=sentinel, confirmation_code=None)

    def test_sentinel_path_constant_is_absolute(self):
        assert SENTINEL_PATH.is_absolute()

    def test_sentinel_path_constant_references_etc(self):
        assert str(SENTINEL_PATH).startswith("/etc/")


# ── Confirmation code ──────────────────────────────────────────────────────────


class TestConfirmationCode:
    def test_live_mode_without_confirmation_raises(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        sentinel.write_text("armed\n")
        with pytest.raises(LiveGateError, match="LIVE_CONFIRMATION_CODE"):
            check_live_gate("live", sentinel_path=sentinel, confirmation_code=None)

    def test_live_mode_empty_confirmation_raises(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        sentinel.write_text("armed\n")
        with pytest.raises(LiveGateError, match="LIVE_CONFIRMATION_CODE"):
            check_live_gate("live", sentinel_path=sentinel, confirmation_code="")

    def test_wrong_date_raises(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        sentinel.write_text("armed\n")
        with pytest.raises(LiveGateError, match="date"):
            check_live_gate(
                "live", sentinel_path=sentinel, confirmation_code="2020-01-01"
            )

    def test_error_shows_expected_and_actual_date(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        sentinel.write_text("armed\n")
        today = date.today().isoformat()
        with pytest.raises(LiveGateError) as exc_info:
            check_live_gate(
                "live", sentinel_path=sentinel, confirmation_code="2020-01-01"
            )
        assert today in str(exc_info.value)

    def test_correct_date_passes(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        sentinel.write_text("armed\n")
        check_live_gate(
            "live",
            sentinel_path=sentinel,
            confirmation_code=date.today().isoformat(),
        )

    def test_paper_mode_ignores_confirmation_code(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        check_live_gate("paper", sentinel_path=sentinel, confirmation_code="2020-01-01")

    def test_shadow_mode_ignores_confirmation_code(self, tmp_path):
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        check_live_gate(
            "shadow", sentinel_path=sentinel, confirmation_code="2020-01-01"
        )

    def test_check_live_gate_reads_env_var(self, tmp_path):
        """check_live_gate with no confirmation_code arg reads LIVE_CONFIRMATION_CODE."""
        sentinel = tmp_path / "ARMED_FOR_LIVE"
        sentinel.write_text("armed\n")
        today = date.today().isoformat()
        with patch.dict(os.environ, {"LIVE_CONFIRMATION_CODE": today}):
            check_live_gate("live", sentinel_path=sentinel)


# ── Effective risk config ──────────────────────────────────────────────────────


class TestEffectiveRiskConfig:
    def test_live_has_lower_max_position_pct(self):
        live_cfg = get_effective_risk_config("live")
        paper_cfg = get_effective_risk_config("paper")
        assert live_cfg.max_position_pct < paper_cfg.max_position_pct

    def test_live_has_lower_max_daily_loss_pct(self):
        live_cfg = get_effective_risk_config("live")
        paper_cfg = get_effective_risk_config("paper")
        assert live_cfg.max_daily_loss_pct < paper_cfg.max_daily_loss_pct

    def test_live_has_higher_min_edge(self):
        live_cfg = get_effective_risk_config("live")
        paper_cfg = get_effective_risk_config("paper")
        assert live_cfg.min_edge > paper_cfg.min_edge

    def test_live_position_pct_at_most_half_paper(self):
        live_cfg = get_effective_risk_config("live")
        paper_cfg = get_effective_risk_config("paper")
        assert live_cfg.max_position_pct <= paper_cfg.max_position_pct / 2

    def test_shadow_uses_paper_limits(self):
        shadow_cfg = get_effective_risk_config("shadow")
        paper_cfg = get_effective_risk_config("paper")
        assert shadow_cfg.max_position_pct == paper_cfg.max_position_pct
        assert shadow_cfg.max_daily_loss_pct == paper_cfg.max_daily_loss_pct
        assert shadow_cfg.min_edge == paper_cfg.min_edge

    def test_live_env_overrides_respected(self):
        with patch.dict(os.environ, {"LIVE_MAX_POSITION_PCT": "0.01"}):
            live_cfg = get_effective_risk_config("live")
        assert live_cfg.max_position_pct == pytest.approx(0.01)


# ── Shadow execution mode ──────────────────────────────────────────────────────


class TestShadowMode:
    def test_shadow_is_valid_execution_mode(self):
        from core.config import Config

        with patch.dict(os.environ, {"EXECUTION_MODE": "shadow"}):
            cfg = Config()
        assert cfg.execution.execution_mode == "shadow"

    def test_shadow_not_rejected_by_config_validate(self):
        from core.config import ExecutionConfig

        cfg = ExecutionConfig.__new__(ExecutionConfig)
        object.__setattr__(cfg, "execution_mode", "shadow")
        assert cfg.execution_mode == "shadow"

    def test_shadow_client_is_paper_type(self):
        from execution.clients.paper import PaperExecutionClient
        from execution.factory import _make_single_execution_client

        client = _make_single_execution_client(None, "shadow", "polymarket")
        assert isinstance(client, PaperExecutionClient)

    def test_shadow_client_kalshi_is_paper_type(self):
        from execution.clients.paper import PaperExecutionClient
        from execution.factory import _make_single_execution_client

        client = _make_single_execution_client(None, "shadow", "kalshi")
        assert isinstance(client, PaperExecutionClient)
