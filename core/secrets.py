"""
Unified secrets management with pluggable backends.

Usage::

    from core.secrets import get_secret

    api_key = get_secret("KALSHI_API_KEY")
    private_key = get_secret("POLYMARKET_PRIVATE_KEY", default="")

Backends are selected via the ``SECRETS_BACKEND`` environment variable:

    env (default)   os.getenv — existing behaviour, works without cloud deps.
    gcp             GCP Secret Manager. Requires google-cloud-secret-manager
                    and GOOGLE_APPLICATION_CREDENTIALS (or VM service account).
                    GCP_PROJECT_ID must be set.

Cascading lookup: the GCP backend falls through to ``os.getenv`` if a secret
is not found in Secret Manager. This lets you keep low-risk config (poll
intervals, log levels) in the environment and only promote sensitive values
to the secret store. All lookups are cached for the process lifetime so a
production VM making 100 API calls doesn't pay 100 round-trips to GCP.

Secret name mapping: the GCP backend translates underscore names to hyphen
names (``POLYMARKET_PRIVATE_KEY`` → ``polymarket-private-key``) to match
GCP's preferred naming convention. Override by setting the full GCP resource
path in ``SECRET_NAME_MAP_<KEY>`` if you already have secrets with different
names.

To rotate backends without code changes, set ``SECRETS_BACKEND=gcp`` in the
VM's systemd unit file. The rest of the codebase calls ``get_secret(...)``
and remains backend-agnostic.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

logger = logging.getLogger(__name__)


class SecretsBackend(Protocol):
    """Interface that all secret backends must satisfy."""

    name: str

    def get(self, name: str, default: str | None = None) -> str | None: ...


class EnvBackend:
    """Read secrets from environment variables (backwards compatible)."""

    name = "env"

    def get(self, name: str, default: str | None = None) -> str | None:
        return os.getenv(name, default)


class GCPSecretManagerBackend:
    """
    Read secrets from GCP Secret Manager, with env fallback.

    Requires:
        - google-cloud-secret-manager installed
        - GCP_PROJECT_ID env var
        - Credentials via GOOGLE_APPLICATION_CREDENTIALS or VM service account
    """

    name = "gcp"

    def __init__(self, project_id: str | None = None) -> None:
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID", "")
        self._client = None
        self._cache: dict[str, str] = {}
        self._unavailable: set[str] = set()

        if not self.project_id:
            logger.warning(
                "GCP secrets backend selected but GCP_PROJECT_ID is empty — "
                "all lookups will fall through to environment variables."
            )

    def _get_client(self):
        if self._client is None:
            try:
                # Lazy import so the rest of the system works without the
                # google-cloud-secret-manager dependency installed.
                from google.cloud import secretmanager

                self._client = secretmanager.SecretManagerServiceClient()
            except ImportError:
                logger.error(
                    "google-cloud-secret-manager not installed. "
                    "Install with: pip install google-cloud-secret-manager"
                )
                return None
            except Exception as e:
                logger.error("Failed to init GCP Secret Manager client: %s", e)
                return None
        return self._client

    @staticmethod
    def _gcp_name(env_name: str) -> str:
        """Translate POLYMARKET_PRIVATE_KEY -> polymarket-private-key."""
        override = os.getenv(f"SECRET_NAME_MAP_{env_name}")
        if override:
            return override
        return env_name.lower().replace("_", "-")

    def get(self, name: str, default: str | None = None) -> str | None:
        if name in self._cache:
            return self._cache[name]

        if name in self._unavailable or not self.project_id:
            return os.getenv(name, default)

        client = self._get_client()
        if client is None:
            return os.getenv(name, default)

        gcp_name = self._gcp_name(name)
        resource = f"projects/{self.project_id}/secrets/{gcp_name}/versions/latest"

        try:
            response = client.access_secret_version(request={"name": resource})
            value = response.payload.data.decode("utf-8")
            self._cache[name] = value
            logger.debug("Loaded secret %s from GCP Secret Manager", name)
            return value
        except Exception as e:
            # Don't spam retries — remember that this secret is missing and
            # fall through to env vars for the rest of the process lifetime.
            logger.warning(
                "Secret %s not found in GCP (%s); falling back to env var",
                name,
                type(e).__name__,
            )
            self._unavailable.add(name)
            return os.getenv(name, default)


_backend: SecretsBackend | None = None


def get_backend() -> SecretsBackend:
    """Return the process-wide secrets backend, creating it on first use."""
    global _backend
    if _backend is None:
        choice = os.getenv("SECRETS_BACKEND", "env").lower()
        if choice == "gcp":
            _backend = GCPSecretManagerBackend()
            logger.info("Secrets backend: GCP Secret Manager")
        else:
            _backend = EnvBackend()
            logger.info("Secrets backend: environment variables")
    return _backend


def set_backend(backend: SecretsBackend | None) -> None:
    """Override the backend — primarily for tests."""
    global _backend
    _backend = backend


def get_secret(name: str, default: str | None = None) -> str | None:
    """
    Fetch a secret by name.

    This is the single entry point for all secret lookups in the codebase.
    Replace direct ``os.getenv("SOME_SECRET")`` calls with
    ``get_secret("SOME_SECRET")`` to benefit from the backend abstraction.
    """
    return get_backend().get(name, default)


def require_secret(name: str) -> str:
    """Fetch a secret and raise if missing — for values that must be set."""
    value = get_secret(name)
    if not value:
        raise RuntimeError(
            f"Required secret {name!r} is not available from "
            f"backend {get_backend().name!r} or environment"
        )
    return value
