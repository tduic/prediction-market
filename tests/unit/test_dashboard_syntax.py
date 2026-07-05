"""Guard against recurring dashboard_api.py corruption (literal \\n sequences)."""

import ast
from pathlib import Path


def test_dashboard_api_is_valid_python():
    src = (
        Path(__file__).parent.parent.parent / "scripts" / "dashboard_api.py"
    ).read_text()
    ast.parse(src)
