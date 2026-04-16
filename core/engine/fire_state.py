"""
Fire state dataclasses for ArbitrageEngine risk management.

These lightweight dataclasses satisfy the duck-typing interface expected by
run_all_checks() without requiring the full Signal dataclass from
core/signals/generator.py (which pulls in Redis and other heavy deps).
"""

from dataclasses import dataclass


@dataclass
class _RiskLeg:
    """Minimal leg proxy for run_all_checks duck-typing."""

    market_id: str
    limit_price: float
    size: float
    side: str = "BUY"


@dataclass
class _RiskSignal:
    """Minimal signal proxy for run_all_checks duck-typing.

    ``violation_id`` is threaded through so the risk_check_log audit row
    can link back to the triggering violation. ``signal_id`` is not
    carried: the signal row does not exist in the DB at risk-check time
    (under PRAGMA foreign_keys=ON the FK would fail), so the audit row
    always writes NULL for signal_id. See core/signals/risk.py.
    """

    legs: list
    edge: float
    strategy: str = ""
    violation_id: str | None = None


@dataclass
class PairFireState:
    """Per-pair cooldown and re-arm state for ArbitrageEngine.

    After a pair fires, armed=False until the spread reverts below
    (min_spread - arb_rearm_hysteresis), preventing churn on oscillating
    prices. The pair also cannot fire again until arb_cooldown_s has elapsed.
    """

    last_fired_at: float
    armed: bool
    last_spread_seen_below: float | None = None
