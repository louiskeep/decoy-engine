"""GlobalSettings: top-level knobs shared across the pipeline.

Per S1 spec line 152 (`global_settings: {seed: ...}`) + advisory axis 6
(reuse V1's `global_settings` naming convention; not a shim, a naming
choice).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class GlobalSettings(BaseModel):
    """Pipeline-wide settings.

    `seed` is the job-level seed material (S1 stub derivation; S3 swaps
    in real HMAC-keyed material). `post_validation` opts the pipeline
    into post-mask invariant checks per the operating model §Validation
    requirements. `on_pool_exhaustion` drives the planner's pool-capacity
    pre-flight (read via `global_settings.get("on_pool_exhaustion")` in
    plan-compile); default `scale_up` matches the engine default.
    `fidelity_warn_threshold` drives the generation-time fidelity
    warn-gate: statistical generate columns are scored against their
    source snapshot after generation and a warning is logged when the
    overall fidelity score falls below this value (warn-only; never
    fails the run or changes output bytes).
    """

    model_config = ConfigDict(extra="forbid")

    seed: int
    post_validation: bool = False
    on_pool_exhaustion: Literal["fail", "scale_up", "fall_back"] = "scale_up"
    fidelity_warn_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
