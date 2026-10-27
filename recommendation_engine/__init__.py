"""Standard-library recommendation engine; no external services or mutable state."""

from .config import Config
from .engine import recommend
from .validation import ValidationError

__all__ = ["Config", "ValidationError", "recommend"]
