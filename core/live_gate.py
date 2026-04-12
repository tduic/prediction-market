"""
Pre-live safety gate: sentinel file, daily confirmation code, and stricter
risk configuration for live-mode execution.

Usage at startup::

    from core.live_gate import check_live_gate, get_effective_risk_config

    # Raises LiveGateError if mode is 'live' and checks fail
    check_live_gate(cfg.execution.execution_mode)

    # Returns appropriate RiskControlConfig based on mode
    risk_cfg = get_effective_risk_config(cfg.execution.execution_mode)
"""

import os
from datetime import date
from pathlib import Path

from core.config import RiskControlConfig

# Default sentinel path. Must be created manually with elevated privileges.
# Never include in deploy tarballs or version control.
SENTINEL_PATH = Path("/etc/predictor/ARMED_FOR_LIVE")


class LiveGateError(RuntimeError):
    """Raised when a live-mode safety check fails."""


def check_live_gate(
    execution_mode: str,
    *,
    sentinel_path: Path = SENTINEL_PATH,
    confirmation_code: str | None = ...,  # type: ignore[assignment]
) -> None:
    """Enforce pre-live safety checks.

    For non-live modes (paper, mock, shadow) this is a no-op.

    For live mode:
      1. The sentinel file must exist at sentinel_path.
      2. The confirmation code must equal today's date (YYYY-MM-DD).
         If confirmation_code is not provided as an argument, the
         LIVE_CONFIRMATION_CODE environment variable is read instead.

    Args:
        execution_mode: One of 'live', 'paper', 'mock', 'shadow'.
        sentinel_path: Path to the sentinel file (default: SENTINEL_PATH).
        confirmation_code: Today's date string or None to read from env.

    Raises:
        LiveGateError: If any live-mode check fails.
    """
    if execution_mode != "live":
        return

    # --- 6.1: Sentinel file ---
    if not sentinel_path.exists():
        raise LiveGateError(
            f"Live mode requires a sentinel file at {sentinel_path}. "
            "Create it manually with: sudo mkdir -p /etc/predictor && "
            "sudo tee /etc/predictor/ARMED_FOR_LIVE <<< 'armed by <operator> on $(date +%F)'"
        )

    # --- 6.2: Daily confirmation code ---
    # Use the argument if provided (not the sentinel default); else read env.
    if confirmation_code is ...:  # type: ignore[comparison-overlap]
        confirmation_code = os.getenv("LIVE_CONFIRMATION_CODE")

    today = date.today().isoformat()

    if not confirmation_code:
        raise LiveGateError(
            "Live mode requires LIVE_CONFIRMATION_CODE=<YYYY-MM-DD> in the environment "
            f"(expected today's date: {today})."
        )

    if confirmation_code != today:
        raise LiveGateError(
            f"LIVE_CONFIRMATION_CODE mismatch: got '{confirmation_code}', "
            f"expected today's date '{today}'. "
            "Update LIVE_CONFIRMATION_CODE in your .env file each day before starting."
        )


def get_effective_risk_config(execution_mode: str) -> RiskControlConfig:
    """Return a RiskControlConfig appropriate for the given execution mode.

    For live mode, defaults are intentionally stricter than paper:
      - max_position_pct:  0.02 (vs paper default 0.05)
      - max_daily_loss_pct: 0.01 (vs paper default 0.02)
      - min_edge:           0.05 (vs paper default 0.02)

    Each value can still be overridden via its LIVE_* env var counterpart
    (e.g. LIVE_MAX_POSITION_PCT). All other risk fields inherit the standard
    env-var driven defaults from RiskControlConfig.

    For all other modes, returns a standard RiskControlConfig() (env-driven).
    """
    if execution_mode != "live":
        return RiskControlConfig()

    return RiskControlConfig(
        max_position_pct=float(os.getenv("LIVE_MAX_POSITION_PCT", "0.02")),
        max_daily_loss_pct=float(os.getenv("LIVE_MAX_DAILY_LOSS_PCT", "0.01")),
        min_edge=float(os.getenv("LIVE_MIN_EDGE_TO_TRADE", "0.05")),
    )
