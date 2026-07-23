"""GlobalSettings: top-level knobs shared across the pipeline.

Per S1 spec line 152 (`global_settings: {seed: ...}`) + advisory axis 6
(reuse V1's `global_settings` naming convention; not a shim, a naming
choice).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class DpGenerateSettings(BaseModel):
    """Declares that this pipeline's `generate` output must be an honest,
    approximate `(epsilon, delta)`-DP marginal release (DPS Scope B,
    2026-07-22 rebuild of the DPS-3 declaration). Presence alone (key
    membership on `global_settings.dp`, not truthiness of this model's
    contents) is what `plan._checks_dp.check_dp_generate_contract` gates
    on: it hard-rejects `allow_real_categories: true`, `high_cardinality:
    true`, and `condition_on` on a `type: statistical` generate column,
    each of which would silently void or fall outside the guarantee this
    block declares.

    `epsilon`/`delta` here are an ENFORCED CEILING, not an annotation.
    `plan._checks_dp.verify_dp_snapshots` composes the `(epsilon_total,
    delta_total)` actually spent by every distinct DP release (keyed by
    `release_id`, not content digest -- guide section 6 row F5) this
    pipeline consumes and rejects the config (`dp_budget_exceeded`) if
    that composed spend exceeds these declared values.

    There is no `numeric_domains` field here: the real numeric-domain and
    categorical-column declarations are `fit_dp_snapshot`'s own
    `numeric_domains`/`categorical_columns` arguments at fit time
    (`quality/dp.py`), a separate step from this generate-side ceiling
    declaration. This block only declares the generate-side `(epsilon,
    delta)` contract and gates the anti-DP generate-column knobs.
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

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_dp_null(cls, data: Any) -> Any:
        """An explicit `dp: null` is refused here, at validation.

        `dp` present-but-null is always an error (the operator declared
        the key but supplied no `(epsilon, delta)` ceiling), and it must
        never be mistaken for "no DP contract". The earlier fix carried
        that distinction through SERIALIZATION, omitting the key when it
        was never assigned and leaving it when it was. Codex round 5
        showed that channel is lossy: pydantic applies `exclude_none` and
        `exclude_defaults` BEFORE a wrap serializer sees the data, so an
        explicit `dp: null` is already gone by then and cannot be
        restored. `_dp_declared` is a key-membership test, so under
        either option the operator silently got every DP gate skipped.

        Patching those two flags would leave the next serialization
        option to reopen the same hole, so the channel goes away instead:
        the invalid config never validates, and no dump of it can exist
        to be misread. `verify_dp_snapshots` still raises
        `dp_budget_declaration_malformed` for callers that pass raw
        dicts without going through this model, so both entry paths fail
        closed.
        """
        # Codex round 6: this tested `isinstance(data, dict)`, but
        # pydantic accepts ANY mapping, so a `UserDict` carrying an
        # explicit `dp: None` validated, and `exclude_none` /
        # `exclude_defaults` then erased the key and `_dp_declared`
        # returned False -- the same fail-open this validator exists to
        # close, reached through a different container type.
        # Codex round 7: widening to `Mapping` was still fail-open,
        # because this returned the CALLER'S object and pydantic then
        # read it a second time. Validating one snapshot and returning a
        # DIFFERENT object is the whole bug, so take the snapshot once
        # and hand pydantic exactly what was checked. Non-mappings pass
        # through untouched for pydantic to reject with its own error.
        #
        # What the second read could actually do, measured against the
        # pre-fix commit (Codex round 8, confirmed independently by
        # dennis; an earlier version of this comment got it wrong):
        # reporting the key ABSENT failed CLOSED, because pydantic's
        # second pass walks model fields through `__getitem__` and a
        # `KeyError` surfaces as a `ValidationError`. Substituting a
        # VALUE did not. A mapping yielding `epsilon=0.1` and then
        # `epsilon=1000.0` produced a model carrying 1000.0 while this
        # validator had approved 0.1 -- a DP-labelled pipeline with a
        # meaningless budget. Substituting `None` left `dp` present in
        # `model_fields_set`, so the omission serializer below kept it,
        # but `model_dump(exclude_none=True)` is a SEPARATE erasure path
        # that drops a `None` regardless, and `_dp_declared` then
        # returned False. The snapshot closes both.
        if not isinstance(data, Mapping):
            return data
        snapshot = dict(data)
        if "dp" in snapshot and snapshot["dp"] is None:
            raise ValueError(
                "global_settings.dp is present but null. Remove the key entirely for a "
                "pipeline with no DP contract, or supply {epsilon: ..., delta: ...} to "
                "declare one. An explicit null is refused so it can never be read as "
                "'no DP declared' (dp_budget_declaration_malformed)."
            )
        return snapshot

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> Any:
        """Keep the serialization-mode JSON schema intact.

        D-M-A (dennis round 5): a `@model_serializer(mode="wrap")`
        returning `Any` makes pydantic discard this model's SERIALIZATION
        schema entirely, so `model_json_schema(mode="serialization")`
        reported `GlobalSettings` as `{}` and dropped
        `DpGenerateSettings` from `$defs` altogether. Validation mode was
        unaffected, and no consumer in this repo or the platform reads
        the serialization-mode schema today, so it was latent rather than
        live -- but it is a real regression introduced by the `dp`
        omission serializer, in the core schema, and nothing covered it.

        The field-enumerating schema is recovered by asking the handler
        again with the `serialization` entry stripped from the schema
        copy, which is exactly the schema pydantic would have built had
        the serializer not been attached. Only the JSON schema is
        affected; the serializer itself still runs on every dump.
        """
        schema = handler(core_schema)
        if not schema:
            schema = handler({k: v for k, v in core_schema.items() if k != "serialization"})
        # C-M-1 (Codex round 6): the generated schema advertised `dp` as
        # `DpGenerateSettings | null` with default `null`, but validation
        # now REFUSES an explicit null, so a schema-driven client could
        # emit a schema-valid config that pydantic rejects. The field
        # annotation has to stay `| None` (that is what an unset `dp`
        # holds in Python), so the advertised schema is corrected to say
        # what is actually accepted: the model, or absent.
        dp_schema = schema.get("properties", {}).get("dp")
        if isinstance(dp_schema, dict) and "anyOf" in dp_schema:
            non_null = [b for b in dp_schema["anyOf"] if b.get("type") != "null"]
            if len(non_null) == 1:
                # Reuse pydantic's OWN branch object rather than writing a
                # `$ref` by hand: the generator tracks ref counts by
                # identity, and a hand-built ref string is not registered.
                schema["properties"]["dp"] = non_null[0]
        return schema

    @model_serializer(mode="wrap")
    def _omit_never_assigned_dp(self, handler: Any) -> Any:
        """C-B3/D-M3: keep `dp` UNSET distinguishable from an explicit
        `dp: null` in every serialization of this model.

        Pydantic materializes `dp: None` for both cases by default -- an
        unset Optional dumps its default, and an explicit `dp: null`
        validates to the same `None` -- and `plan._checks_dp._dp_declared`
        is a key-membership test. Collapsing the two lets an operator who
        writes `dp: null` bypass every DP gate (provenance, budget,
        categorical consent, receipt), which is the fail-open Codex
        executed.

        This lives here, as a serializer on the model that owns the
        field, rather than as a `model_dump` override on `PipelineConfig`.
        That override only covered `model_dump`: `model_dump_json` goes
        through pydantic-core and never consulted it, so it still emitted
        `dp: None` for a never-assigned field and made `_dp_declared`
        true for ordinary non-DP pipelines. Fail-closed rather than a
        leak, but it broke that path outright. A serializer runs on both.

        Only the one key `dp`, and only when never assigned, is removed;
        every other field keeps its normal default-carrying dump, so
        `_hash_config`'s byte-stability argument is untouched.
        """
        data = handler(self)
        if isinstance(data, dict) and "dp" not in self.model_fields_set:
            data.pop("dp", None)
        return data
