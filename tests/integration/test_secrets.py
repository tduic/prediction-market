"""
Tests for core.secrets.

Covers:
  - Default EnvBackend reads from os.environ
  - require_secret raises on missing
  - set_backend override works for custom backends
  - GCPSecretManagerBackend falls through to env when no project ID
  - GCPSecretManagerBackend name translation
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # noqa: E402

from core.secrets import (  # noqa: E402
    EnvBackend,
    GCPSecretManagerBackend,
    get_secret,
    require_secret,
    set_backend,
)


@pytest.fixture(autouse=True)
def reset_backend():
    """Ensure each test gets a clean backend state."""
    set_backend(None)
    yield
    set_backend(None)


def test_env_backend_reads_environ(monkeypatch):
    monkeypatch.setenv("TEST_SECRET_A", "hello")
    backend = EnvBackend()
    assert backend.get("TEST_SECRET_A") == "hello"


def test_env_backend_default_when_missing():
    backend = EnvBackend()
    assert backend.get("NONEXISTENT_SECRET_XYZ", "fallback") == "fallback"
    assert backend.get("NONEXISTENT_SECRET_XYZ") is None


def test_get_secret_uses_env_backend_by_default(monkeypatch):
    monkeypatch.delenv("SECRETS_BACKEND", raising=False)
    monkeypatch.setenv("MY_KEY", "my-value")
    assert get_secret("MY_KEY") == "my-value"


def test_require_secret_raises_on_missing(monkeypatch):
    monkeypatch.delenv("DEFINITELY_NOT_SET_XYZ", raising=False)
    with pytest.raises(RuntimeError, match="Required secret"):
        require_secret("DEFINITELY_NOT_SET_XYZ")


def test_require_secret_returns_value(monkeypatch):
    monkeypatch.setenv("PRESENT_SECRET", "ok")
    assert require_secret("PRESENT_SECRET") == "ok"


def test_set_backend_override():
    class FakeBackend:
        name = "fake"

        def get(self, name, default=None):
            return f"fake-{name}"

    set_backend(FakeBackend())
    assert get_secret("ANYTHING") == "fake-ANYTHING"


def test_gcp_backend_name_translation():
    assert (
        GCPSecretManagerBackend._gcp_name("POLYMARKET_PRIVATE_KEY")
        == "polymarket-private-key"
    )
    assert GCPSecretManagerBackend._gcp_name("KALSHI_API_KEY") == "kalshi-api-key"


def test_gcp_backend_respects_name_override(monkeypatch):
    monkeypatch.setenv("SECRET_NAME_MAP_KALSHI_API_KEY", "prod/kalshi-prod-key")
    assert GCPSecretManagerBackend._gcp_name("KALSHI_API_KEY") == "prod/kalshi-prod-key"


def test_gcp_backend_falls_through_to_env_without_project(monkeypatch):
    """With no GCP_PROJECT_ID, the backend should just read env vars."""
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    monkeypatch.setenv("MY_FALLBACK_KEY", "env-value")

    backend = GCPSecretManagerBackend(project_id="")
    assert backend.get("MY_FALLBACK_KEY") == "env-value"


def test_gcp_backend_caches_unavailable_keys(monkeypatch):
    """After a missing lookup, subsequent calls should not retry the client."""
    monkeypatch.setenv("GCP_PROJECT_ID", "fake-project-id")
    monkeypatch.setenv("CACHED_MISSING_KEY", "from-env")

    backend = GCPSecretManagerBackend(project_id="fake-project-id")

    # Without google-cloud-secret-manager installed, _get_client() returns
    # None and the backend falls through to env.
    val = backend.get("CACHED_MISSING_KEY")
    assert val == "from-env"

    # Second call should still work (from cache or env fallback).
    assert backend.get("CACHED_MISSING_KEY") == "from-env"


def test_backend_selection_via_env(monkeypatch):
    from core.secrets import get_backend

    monkeypatch.setenv("SECRETS_BACKEND", "env")
    set_backend(None)
    assert get_backend().name == "env"

    monkeypatch.setenv("SECRETS_BACKEND", "gcp")
    set_backend(None)
    assert get_backend().name == "gcp"
