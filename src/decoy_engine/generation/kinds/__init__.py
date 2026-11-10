"""Generate types that need no source data: `constant` and parametric `distribution`.

A sub-package rather than modules beside `synthesize.py` (generation/ already holds 25 direct
modules). The name is `kinds`, not `types`, so it never shadows the stdlib module. Callers import
from here, never from the underscore modules.
"""

from __future__ import annotations

from decoy_engine.generation.kinds._constant import (
    ConstantStream,
    constant_arrow_type,
    constant_payload_bound,
    constant_values,
)
from decoy_engine.generation.kinds._parametric import (
    ParametricStream,
    parametric_arrow_type,
    parametric_payload_bound,
    parametric_values,
)

__all__ = [
    "ConstantStream",
    "ParametricStream",
    "constant_arrow_type",
    "constant_payload_bound",
    "constant_values",
    "parametric_arrow_type",
    "parametric_payload_bound",
    "parametric_values",
]
