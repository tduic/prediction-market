"""Atomically merge deployment configuration into the protected environment file.

The update file contains deployment settings only. Runtime credentials belong in
the configured secrets backend and are never copied by this helper.
"""

import os
import re
import tempfile
from pathlib import Path

UPDATES_PATH = Path("/tmp/predictor_updates.env")
ENV_PATH = Path("/data/predictor/.env")


def merge_environment_file(updates_path: Path, env_path: Path) -> None:
    """Merge updates and replace the destination atomically with mode 0600."""
    updates: dict[str, str] = {}
    for line in updates_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            updates[key.strip()] = value.strip()

    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    for key, value in updates.items():
        pattern = re.compile(r"^" + re.escape(key) + r"=.*$", re.MULTILINE)
        entry = f"{key}={value}"
        if pattern.search(existing):
            existing = pattern.sub(entry, existing)
        else:
            existing = existing.rstrip("\n") + "\n" + entry + "\n"

    fd, temporary_path = tempfile.mkstemp(
        dir=env_path.parent,
        prefix=f".{env_path.name}.",
        text=True,
    )
    try:
        os.chmod(temporary_path, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(existing)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, env_path)
    except OSError:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
        raise

    updates_path.unlink()


def main() -> None:
    merge_environment_file(UPDATES_PATH, ENV_PATH)


if __name__ == "__main__":
    main()
