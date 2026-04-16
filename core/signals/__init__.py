"""Signal generator module for trading signals."""

from core.signals.backpressure import BackpressureMonitor
from core.signals.dedup import SignalDeduplicator
from core.signals.dlq import DeadLetterQueue
from core.signals.generator import SignalGenerator
from core.signals.queue import HardenedSignalQueue
from core.signals.risk import (
    RiskCheckResult,
    check_daily_loss_limit,
    check_duplicate_signal,
    check_min_edge,
    check_portfolio_exposure,
    check_position_limit,
    run_all_checks,
)
from core.signals.sizing import compute_kelly_fraction, compute_position_size

__all__ = [
    "SignalGenerator",
    "RiskCheckResult",
    "check_position_limit",
    "check_daily_loss_limit",
    "check_portfolio_exposure",
    "check_duplicate_signal",
    "check_min_edge",
    "run_all_checks",
    "compute_kelly_fraction",
    "compute_position_size",
    "SignalDeduplicator",
    "DeadLetterQueue",
    "BackpressureMonitor",
    "HardenedSignalQueue",
]
