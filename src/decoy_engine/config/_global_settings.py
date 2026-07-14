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
    `unconfigured_column_policy` (DE-03) drives the fail-closed output
    projection: `error` rejects any output column the plan does not
    declare a strategy for; `warn` lets it pass through with a structured
    warning. Unset defaults to the release phase (`warn` pre-GA migration
    window, `error` at GA); an explicit value here overrides that. See
    `execution/_output_projection.resolve_unconfigured_column_policy`.
    `mask_secret_ref` (DE-02) is a REFERENCE to the keyed-mask secret --
    never the secret itself. Two ref kinds are supported: `env:NAME`
    (resolved from an environment variable) and `file:/PATH` (resolved
    from a file's contents), each hex- or base64-decoded to >=32 raw
    bytes. The engine resolves it at the run edge into a
    `SecretKeyProvider` and NEVER serializes the raw secret into the plan,
    a log, or the manifest. A programmatically supplied
    `run(key_provider=...)` takes precedence over this ref. Unset means no
    secret: pre-GA the keyed surface falls back to `job_seed`
    (byte-identical to today); at GA a keyed plan with no secret
    hard-errors. See `decoy_engine.keyprovider`.
    """

    model_config = ConfigDict(extra="forbid")

    seed: int
    post_validation: bool = False
    on_pool_exhaustion: Literal["fail", "scale_up", "fall_back"] = "scale_up"
    fidelity_warn_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    unconfigured_column_policy: Literal["warn", "error"] | None = None
    mask_secret_ref: str | None = None
