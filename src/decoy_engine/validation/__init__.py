"""decoy_engine validation package.

Two layers (engine-v2 S10 operating model):

- **Compile-time** (always on): `decoy_engine.plan.validate.validate_plan` runs
  every plan-compile check. The legacy config-shape validator `validate_config`
  (V1, re-exported here) checks a pipeline config is well-formed.
- **Post-execution** (opt-in via `post_validation: true`): the scan suite in
  `decoy_engine.validation.post` produces the `quality_summary` manifest block.

`validate_config` moved from the old `validation.py` module to `validation/_config.py`
when this became a package (S10 slice 2); the public name is unchanged.
"""

from __future__ import annotations

from decoy_engine.validation._config import validate_config

__all__ = ["validate_config"]
