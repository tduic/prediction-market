"""Guard against recurring file corruption (literal \\n sequences)."""

import ast
from pathlib import Path

_ROOT = Path(__file__).parent.parent.parent

_CORRUPTION_PRONE = [
    _ROOT / "scripts" / "dashboard_api.py",
    _ROOT / "core" / "engine" / "reconciliation.py",
    _ROOT / "core" / "strategies" / "batch.py",
]


def test_dashboard_api_is_valid_python():
    src = (_ROOT / "scripts" / "dashboard_api.py").read_text()
    ast.parse(src)


def test_reconciliation_is_valid_python():
    src = (_ROOT / "core" / "engine" / "reconciliation.py").read_text()
    ast.parse(src)


def test_batch_strategy_is_valid_python():
    src = (_ROOT / "core" / "strategies" / "batch.py").read_text()
    ast.parse(src)


def test_no_literal_newline_corruption():
    """All corruption-prone files must not contain literal backslash-n sequences
    where a file has fewer real newlines than expected (i.e., the file is one
    long line with escaped newlines)."""
    for path in _CORRUPTION_PRONE:
        src = path.read_text()
        real_lines = src.count("\n")
        assert real_lines >= 50, (
            f"{path.name} has only {real_lines} real newlines — "
            f"possible literal \\\\n corruption"
        )
