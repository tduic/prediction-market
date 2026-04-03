"""Model service module for probability prediction."""

from core.models.base import BaseModel
from core.models.registry import ModelRegistry, ModelVersion

# Try to import models, but don't fail if dependencies are missing
try:
    from core.models.fomc import FOMCModel
except ImportError:
    FOMCModel = None

try:
    from core.models.cpi import CPIModel
except ImportError:
    CPIModel = None

try:
    from core.models.calibration import CalibrationModel
except ImportError:
    CalibrationModel = None

__all__ = [
    "BaseModel",
    "FOMCModel",
    "CPIModel",
    "CalibrationModel",
    "ModelRegistry",
    "ModelVersion",
]
