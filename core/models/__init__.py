"""Model service module for probability prediction."""

from core.models.base import BaseModel
from core.models.registry import ModelRegistry, ModelVersion

# Try to import models, but don't fail if dependencies are missing.
# The `# type: ignore` on the except arm is needed because mypy sees the
# try block's binding as a type alias (type[FOMCModel] etc.) and rejects
# rebinding to None.
try:
    from core.models.fomc import FOMCModel
except ImportError:
    FOMCModel = None  # type: ignore[assignment,misc]

try:
    from core.models.cpi import CPIModel
except ImportError:
    CPIModel = None  # type: ignore[assignment,misc]

try:
    from core.models.calibration import CalibrationModel
except ImportError:
    CalibrationModel = None  # type: ignore[assignment,misc]

__all__ = [
    "BaseModel",
    "FOMCModel",
    "CPIModel",
    "CalibrationModel",
    "ModelRegistry",
    "ModelVersion",
]
