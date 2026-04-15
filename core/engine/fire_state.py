"""
Fire state dataclasses for ArbitrageEngine risk management.

These lightweight dataclasses satisfy the duck-typing interface expected by
run_all_checks() without requiring the full Signal dataclass from
core/signals/generator.py (which pulls in Redis and other heavy deps).
"""

import uuid
from dataclasses import dataclass, field


@dataclass
class _RiskLeg:
    """Minimal leg proxy for run_all_checks duck-typing."""

    market_id: str
    limit_price: float
    size: float
    side: str = "BUY"


@dataclass
class _RiskSignal:
    """Minimal signal proxy for run_all_checks duck-typing."""

    legs: list
    edge: float
    signal_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    strategy: str = ""


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
