"""Unit tests for assign_strategy() in paper_trading_session."""

import ast
import sys
import textwrap
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_SRC = (PROJECT_ROOT / "scripts" / "paper_trading_session.py").read_text()
_TREE = ast.parse(_SRC)

_func_node = next(
    n
    for n in ast.walk(_TREE)
    if isinstance(n, ast.FunctionDef) and n.name == "assign_strategy"
)
_func_lines = _SRC.splitlines()[_func_node.lineno - 1 : _func_node.end_lineno]
_func_src = textwrap.dedent("\n".join(_func_lines))

_ns: dict = {}
exec(compile(_func_src, "<assign_strategy>", "exec"), _ns)
assign_strategy = _ns["assign_strategy"]


@pytest.mark.parametrize(
    "spread,pair_type,expected",
    [
        # cross_platform always → P1 regardless of spread
        (0.20, "cross_platform", "P1_cross_market_arb"),
        (0.01, "cross_platform", "P1_cross_market_arb"),
        # large spread → P5
        (0.15, "same", "P5_information_latency"),
        (0.11, "same", "P5_information_latency"),
        # boundary: spread == 0.10 → P3 (was dead code before fix)
        (0.10, "same", "P3_calibration_bias"),
        # medium spread → P3
        (0.07, "same", "P3_calibration_bias"),
        (0.05, "same", "P3_calibration_bias"),
        # small spread + complement → P4
        (0.03, "complement", "P4_liquidity_timing"),
        # small spread + other → P2
        (0.03, "same", "P2_structured_event"),
        (0.01, "other", "P2_structured_event"),
    ],
)
def test_assign_strategy_branches(spread, pair_type, expected):
    assert assign_strategy(spread, pair_type) == expected
