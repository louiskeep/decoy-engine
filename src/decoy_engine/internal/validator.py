"""Backward-compatibility re-export shim for legacy validator classes.

V2.0-B split (2026-05-25 session 11): MaskerConfigValidator and
GeneratorConfigValidator moved to their own modules. Callers that
import from ``decoy_engine.internal.validator`` continue to work;
all three names are re-exported unchanged.

Both classes are V2.1 deletion candidates once the legacy masker and
generator paths (masker/masker.py + generators/generator.py) are removed.
At that point this shim and its two source modules can be deleted together.
"""

from decoy_engine.errors import ValidationError
from decoy_engine.internal.generator_validator import GeneratorConfigValidator
from decoy_engine.internal.masker_validator import MaskerConfigValidator

__all__ = [
    "ValidationError",
    "MaskerConfigValidator",
    "GeneratorConfigValidator",
]
