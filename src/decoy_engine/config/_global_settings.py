"""GlobalSettings: top-level knobs shared across the pipeline.

Per S1 spec line 152 (`global_settings: {seed: ...}`) + advisory axis 6
(reuse V1's `global_settings` naming convention; not a shim, a naming
choice).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DpGenerateSettings(BaseModel):
    """DPS-3: declares that this pipeline's `generate` output must be
    (epsilon, delta)-DP. Presence alone (not epsilon/delta's values) is
    what `plan._checks_dp.check_dp_generate_contract` gates on: it hard-
    rejects `allow_real_categories: true` / `high_cardinality: true`
    generate columns, which release real vocabulary and would silently
    void the guarantee this block declares.

    Gate remediation Fix 5 (LOW #5): a `numeric_domains` field used to live
    here as a documented-informational mirror of
    `quality/snapshot.compute_distribution_snapshot`'s fit-time param --
    but fitting happens via the separate `decoy fit` step (CLI-side), so
    the engine never read it from this block; an operator setting it here
    got silently no effect. Dropped rather than kept as a footgun. The
    real numeric-domain declaration is `compute_distribution_snapshot(...,
    numeric_domains=..., dp_mode=True)` at fit time; this block only
    declares the generate-side (epsilon, delta) contract and gates the
    anti-DP generate-column knobs.
    """

    model_config = ConfigDict(extra="forbid")

    epsilon: float = Field(gt=0)
    delta: float = Field(default=1e-6, gt=0, lt=1)


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
    `categorical_retention_warn_threshold` (HC-5) drives the FIT-time
    categorical-retention warn-gate (`quality._retention_gate`): a warning
    is logged for any snapshot column that fell to `freetext` via the
    cardinality cliff, or whose top-K collapse (or a requested joint
    table's cell collapse) dropped retained mass below this value.
    Warn-only, same contract as `fidelity_warn_threshold`; `0.0` disables
    it entirely.
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
    `freetext_advisory_min_avg_length` / `freetext_advisory_min_distinctness`
    (HC-7) drive the compile-time clinical free-text advisory
    (`quality._freetext_advisory`): an unmasked (`strategy: passthrough`)
    string column whose name matches a clinical/claims free-text hint, or
    whose average length and distinctness both clear these thresholds,
    gets a warning recommending `strategy: text_mask`. Warn-only, same
    contract as `fidelity_warn_threshold`; never auto-assigns a strategy,
    never mutates the plan/config, never changes output bytes.
    `freetext_advisory_min_avg_length <= 0` disables the advisory entirely
    (both branches).
    `dp` (DPS-3) opts this pipeline's `generate` output into a (epsilon,
    delta)-DP marginal claim; see `DpGenerateSettings`. Unset (the
    default) means no DP contract and no gate -- byte-identical to every
    prior engine version.
    """

    model_config = ConfigDict(extra="forbid")

    seed: int
    post_validation: bool = False
    on_pool_exhaustion: Literal["fail", "scale_up", "fall_back"] = "scale_up"
    fidelity_warn_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    categorical_retention_warn_threshold: float = Field(default=0.8, ge=0.0, le=1.0)
    unconfigured_column_policy: Literal["warn", "error"] | None = None
    mask_secret_ref: str | None = None
    freetext_advisory_min_avg_length: float = Field(default=40.0)
    freetext_advisory_min_distinctness: float = Field(default=0.5, ge=0.0, le=1.0)
    dp: DpGenerateSettings | None = None
