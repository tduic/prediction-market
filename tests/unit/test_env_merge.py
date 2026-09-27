"""Tests for the atomic deployment environment-file updater."""

import importlib.util
import os
import stat
from pathlib import Path

MODULE_PATH = Path(__file__).parent.parent.parent / "deploy" / "env_merge.py"
SPEC = importlib.util.spec_from_file_location("env_merge", MODULE_PATH)
assert SPEC and SPEC.loader
env_merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(env_merge)


def test_merge_replaces_keys_preserves_other_values_and_restricts_mode(
    tmp_path, monkeypatch
):
    env_path = tmp_path / "runtime.env"
    updates_path = tmp_path / "updates.env"
    env_path.write_text("UNCHANGED=value\nREPLACED=old\n", encoding="utf-8")
    updates_path.write_text("REPLACED=new\nADDED=value\n", encoding="utf-8")

    chmod_modes: list[int] = []
    original_chmod = env_merge.os.chmod

    def track_chmod(path, mode):
        chmod_modes.append(mode)
        original_chmod(path, mode)

    monkeypatch.setattr(env_merge.os, "chmod", track_chmod)
    env_merge.merge_environment_file(updates_path, env_path)

    assert env_path.read_text(encoding="utf-8") == (
        "UNCHANGED=value\nREPLACED=new\nADDED=value\n"
    )
    assert not updates_path.exists()
    assert 0o600 in chmod_modes
    if os.name == "posix":
        assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_merge_keeps_update_file_when_replacement_fails(tmp_path, monkeypatch):
    env_path = tmp_path / "runtime.env"
    updates_path = tmp_path / "updates.env"
    updates_path.write_text("ADDED=value\n", encoding="utf-8")

    def fail_replace(*_args):
        raise OSError("dummy replacement failure")

    monkeypatch.setattr(env_merge.os, "replace", fail_replace)

    try:
        env_merge.merge_environment_file(updates_path, env_path)
    except OSError:
        pass
    else:
        raise AssertionError("expected atomic replacement to fail")

    assert updates_path.exists()
