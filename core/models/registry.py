"""Model version registry and deployment logic."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

logger = logging.getLogger(__name__)


class ModelStatus(str, Enum):
    """Model deployment status."""

    DRAFT = "draft"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"


@dataclass
class ModelVersion:
    """Model version metadata."""

    model_name: str
    version: str
    status: ModelStatus = ModelStatus.DRAFT
    deployed_at: datetime | None = None
    retired_at: datetime | None = None
    metrics: dict = field(default_factory=dict)
    notes: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ModelRegistry:
    """In-memory model version registry."""

    def __init__(self):
        """Initialize registry."""
        self.versions: dict[str, list[ModelVersion]] = {}
        self.active_versions: dict[str, str] = {}

    def register_version(
        self,
        model_name: str,
        version: str,
        metrics: dict | None = None,
        notes: str = "",
    ) -> ModelVersion:
        """
        Register a new model version.

        Args:
            model_name: Model identifier
            version: Version string (e.g., "1.0.0")
            metrics: Performance metrics dict
            notes: Optional notes

        Returns:
            ModelVersion object
        """
        if model_name not in self.versions:
            self.versions[model_name] = []

        # Check if version already exists
        for v in self.versions[model_name]:
            if v.version == version and v.status != ModelStatus.RETIRED:
                logger.warning(f"Version {version} of {model_name} already exists")
                return v

        model_version = ModelVersion(
            model_name=model_name,
            version=version,
            status=ModelStatus.DRAFT,
            metrics=metrics or {},
            notes=notes,
        )

        self.versions[model_name].append(model_version)
        logger.info(f"Registered {model_name} version {version}")

        return model_version

    def get_active_version(self, model_name: str) -> ModelVersion | None:
        """
        Get active version of a model.

        Args:
            model_name: Model identifier

        Returns:
            Active ModelVersion or None
        """
        if model_name not in self.active_versions:
            return None

        version_str = self.active_versions[model_name]
        if model_name not in self.versions:
            return None

        for v in self.versions[model_name]:
            if v.version == version_str and v.status == ModelStatus.ACTIVE:
                return v

        return None

    def deploy_version(self, model_name: str, version: str) -> bool:
        """
        Deploy a model version to active status.

        Args:
            model_name: Model identifier
            version: Version string

        Returns:
            True if deployment successful
        """
        if model_name not in self.versions:
            logger.error(f"Model {model_name} not found")
            return False

        target = None
        for v in self.versions[model_name]:
            if v.version == version:
                target = v
                break

        if not target:
            logger.error(f"Version {version} of {model_name} not found")
            return False

        # Retire old active version
        old_active = self.get_active_version(model_name)
        if old_active:
            old_active.status = ModelStatus.DEPRECATED
            logger.info(f"Deprecated {model_name} version {old_active.version}")

        # Activate new version
        target.status = ModelStatus.ACTIVE
        target.deployed_at = datetime.now(timezone.utc)
        self.active_versions[model_name] = version

        logger.info(f"Deployed {model_name} version {version}")
        return True

    def retire_version(self, model_name: str, version: str) -> bool:
        """
        Retire a model version.

        Args:
            model_name: Model identifier
            version: Version string

        Returns:
            True if retirement successful
        """
        if model_name not in self.versions:
            logger.error(f"Model {model_name} not found")
            return False

        target = None
        for v in self.versions[model_name]:
            if v.version == version:
                target = v
                break

        if not target:
            logger.error(f"Version {version} of {model_name} not found")
            return False

        if target.status == ModelStatus.ACTIVE:
            logger.error(f"Cannot retire active version {version}")
            return False

        target.status = ModelStatus.RETIRED
        target.retired_at = datetime.now(timezone.utc)

        logger.info(f"Retired {model_name} version {version}")
        return True

    def list_versions(self, model_name: str) -> list[ModelVersion]:
        """
        List all versions of a model.

        Args:
            model_name: Model identifier

        Returns:
            List of ModelVersion objects
        """
        if model_name not in self.versions:
            return []

        return sorted(
            self.versions[model_name],
            key=lambda v: v.created_at,
            reverse=True,
        )

    def get_version(self, model_name: str, version: str) -> ModelVersion | None:
        """
        Get specific model version.

        Args:
            model_name: Model identifier
            version: Version string

        Returns:
            ModelVersion or None if not found
        """
        if model_name not in self.versions:
            return None

        for v in self.versions[model_name]:
            if v.version == version:
                return v

        return None

    def get_registry_status(self) -> dict:
        """
        Get full registry status.

        Returns:
            Status dict
        """
        status = {}
        for model_name, versions in self.versions.items():
            active = self.get_active_version(model_name)
            status[model_name] = {
                "total_versions": len(versions),
                "active_version": active.version if active else None,
                "versions": [
                    {
                        "version": v.version,
                        "status": v.status.value,
                        "deployed_at": (
                            v.deployed_at.isoformat() if v.deployed_at else None
                        ),
                        "metrics": v.metrics,
                    }
                    for v in versions
                ],
            }

        return status
