"""Guard against recurring file corruption (literal \\n sequences).

Files have twice been rewritten as one long line of escaped newlines
(scripts/dashboard_api.py, execution/circuit_breaker.py). Check every source
file, not a hand-picked list.
"""

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent.parent
_SOURCES = sorted(
    p
    for d in ("core", "execution", "scripts", "tests")
    for p in (_ROOT / d).rglob("*.py")
    if "__pycache__" not in p.parts
)


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: str(p.relative_to(_ROOT)))
def test_source_parses(path):
    ast.parse(path.read_text(), filename=str(path))


@pytest.mark.parametrize("path", _SOURCES, ids=lambda p: str(p.relative_to(_ROOT)))
def test_no_single_line_corruption(path):
    src = path.read_text()
    longest = max((len(line) for line in src.splitlines()), default=0)
    assert longest < 1000, (
        f"{path.name} has a {longest}-character line — "
        "possible literal \\n corruption"
    )
