"""Versioned ranking coefficients, kept separate from business validation."""

from dataclasses import dataclass, field
from math import isclose

from .validation import ValidationError, number, object_value, positive_int, text

DEFAULT_WEIGHTS = {"interest": 0.40, "major": 0.15, "pillar": 0.25,
                   "schedule": 0.15, "walletAffinity": 0.05}


@dataclass(frozen=True)
class Config:
    version: str = "initial-defaults-1"
    club_limit: int = 6
    activity_limit: int = 6
    weights: dict = field(default_factory=lambda: DEFAULT_WEIGHTS.copy())
    weakest_boost: float = 2.0
    strongest_boost: float = 1.5
    other_boost: float = 1.0
    tie_tolerance: float = 1.0
    wallet_cap: int = 1000
    max_source_age_hours: float = 24.0

    @classmethod
    def from_dict(cls, value=None):
        if value is None:
            return cls()
        allowed = {"configVersion", "clubLimit", "activityLimit", "weights",
                   "weakestBoost", "strongestBoost", "otherBoost", "tieTolerance",
                   "walletAffinityCap", "maxSourceAgeHours"}
        value = object_value(value, "config", allowed)
        version = text(value.get("configVersion"), "configVersion")
        weights = object_value(value.get("weights", DEFAULT_WEIGHTS), "weights", set(DEFAULT_WEIGHTS))
        if set(weights) != set(DEFAULT_WEIGHTS):
            raise ValidationError("INVALID_CONFIG", "All five weights are required.")
        weights = {k: number(v, k, 0, 1) for k, v in weights.items()}
        if not isclose(sum(weights.values()), 1.0, abs_tol=1e-9):
            raise ValidationError("INVALID_CONFIG", "Weights must sum to one.")
        result = cls(
            version, positive_int(value.get("clubLimit", 6), "clubLimit", 20),
            positive_int(value.get("activityLimit", 6), "activityLimit", 20), weights,
            number(value.get("weakestBoost", 2), "weakestBoost", 0.01, 100),
            number(value.get("strongestBoost", 1.5), "strongestBoost", 0.01, 100),
            number(value.get("otherBoost", 1), "otherBoost", 0.01, 100),
            number(value.get("tieTolerance", 1), "tieTolerance", 0, 100),
            positive_int(value.get("walletAffinityCap", 1000), "walletAffinityCap", 9007199254740991),
            number(value.get("maxSourceAgeHours", 24), "maxSourceAgeHours", 0.01, 168),
        )
        if result.version == "initial-defaults-1" and result != cls():
            raise ValidationError("CONFIG_VERSION_REQUIRED", "Changed settings require a new configVersion.")
        return result
