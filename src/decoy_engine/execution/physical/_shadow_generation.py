"""Task 4.6 slice 5a: the pure-generate dispatch's admission gate + adapter
call, split out of `_shadow_coordinator.py` to keep that module under the
600-LOC orchestration cap (CLAUDE.md "Orchestration modules cap at ~600 LOC").

`dispatch_synthesis` is what `ShadowCoordinator._dispatch_synthesis` used to
do as a method, now a free function taking `ctx` directly instead of `self`.
It never reimplements generation, never merges into any mask output (that
stitch is slice 5b-i territory, owned by `execution/_stitch.py` and called
from `_shadow_mixed.py`), and propagates every adapter/generation exception
UNCHANGED -- the differential harness's phase-bound comparator decides
whether a raise is a faithful identical rejection, not this module.

Task 4.6 slice 5b-i splits the admission gate in three, so the independent-
mixed dispatch (`_shadow_mixed.py`) can share the generation-identity check
without inheriting the pure dispatch's job-scope exclusions (a real mixed
job legitimately carries non-empty sources/relationships/a non-empty
snapshot, all of which the pure gate rejects):

- `require_generation_shape` (§1a, shared): generation IDENTITY/column-shape
  only -- config_digest, the generate-table name set, admitted generate
  column `type`s, no `determinism: fresh`. Never inspects a job-scope field
  (sources/relationships/snapshot/...); those are not side-local to
  generation and are decided by whichever caller-specific gate wraps this.
- `require_pure_generation_shadowable` (§1b): `require_generation_shape`
  plus the pure-only exclusions (empty snapshot/sources/relationships/
  namespaces/subset/generate-table-transforms/validators/quarantine/
  run_storm/mask_secret_ref, and every runtime carrier at its admitted
  value). This is slice 5a's original gate, behavior-preserved exactly
  under its new name.
- `require_independent_mixed_shadowable` lives in `_shadow_mixed.py` (its
  own module, not here, to keep this file's job scoped to the pure-only
  contract) and calls `require_generation_shape` for its own shared core.

Declines (coded `GENERATION_SHAPE_UNSUPPORTED`, before the adapter is ever
constructed) unless ALL hold: the shadow snapshot is empty; `ctx.plan`
exists and carries a `generation` payload whose config digest AND
generate-table name set both match `physical_synthesis` (the identity bind
-- `pipeline_config_hash` deliberately excludes sources/targets, so it
cannot serve this role); every table is generate-kind with an admitted
column `type` (`sequence`/`categorical`) and no `determinism: fresh`; the
job-scope fields (`sources`/`relationships`/`namespaces`/`subset`/
generate-table `transforms`/`validators`/`quarantine`/`run_storm`/
`mask_secret_ref`) are all empty/absent/`None`/`False`; and the
runtime-admission record (`derive_key`/`instance_default_locale`/
`key_provider`/`sink`/`source_loader`/`vault_writer`/`fidelity_report`) is
entirely at its admitted (`None`/`False`) value.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from decoy_engine.execution._pipeline import classify_table_kinds
from decoy_engine.execution.physical._shadow_diff_codes import (
    GENERATION_SHAPE_UNSUPPORTED,
    ShadowDifference,
)
from decoy_engine.generation._faker_pool import POOL_ELIGIBLE_FAKER_TYPES
from decoy_engine.internal.faker_setup import has_custom_faker_override

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pyarrow as pa
    from faker import Faker

    from decoy_engine.execution.physical._context import SeamContext
    from decoy_engine.execution.physical._plan import SynthesisStage
    from decoy_engine.execution.physical._shadow_context import ShadowContext
    from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
    from decoy_engine.plan._types import Plan

__all__ = [
    "SHADOW_ADMISSIBLE_FAKER_TYPES",
    "dispatch_synthesis",
    "require_generation_shape",
    "require_pure_generation_shadowable",
    "run_synthesis_adapter",
]

# The admitted generate-column `type` set for the slice's dispatches (both
# pure and independent-mixed): sequence + categorical + a closed faker
# allowlist (5a-faker). Deliberately closed here rather than sourced from
# `config._tables.GENERATE_TYPES` -- that constant lists every type the
# SCHEMA accepts, not what this slice's admission gates shadow; every other
# generate type is a scoped follow-up, never an accident of reusing the
# wrong source.
_ADMITTED_GENERATE_COLUMN_TYPES = frozenset({"sequence", "categorical", "faker"})

# 5a-faker: the closed, empirically-proven-deterministic faker_type set this
# gate admits, reusing GP2's own reviewed allowlist as the single source
# (Codex plan-gate v1 confirmed no import cycle -- execution.physical
# already depends on generation via drivers/_synthesis.py). Every other
# faker_type (custom overrides, non-deterministic builtins like uuid1/
# passport_full, the ~190-type long tail) stays oracle-only; widening this
# needs its own determinism proof + review, per the gen-5a-faker plan.
SHADOW_ADMISSIBLE_FAKER_TYPES: frozenset[str] = POOL_ELIGIBLE_FAKER_TYPES


def dispatch_synthesis(
    ctx: ShadowContext,
    physical_synthesis: SynthesisStage,
    snapshot: ShadowSnapshot,
) -> tuple[dict[str, pa.Table], SeamContext]:
    """Admit + dispatch a PURE-generate plan (no mask tables at all) through
    the Task 4.2 `SynthesisStageAdapter`. Never reimplements generation,
    never merges with any mask output. Every adapter/generation exception
    propagates UNCHANGED -- the differential harness's phase-bound
    comparator decides whether a raise is a faithful identical rejection,
    not this function.
    """
    plan_obj = require_pure_generation_shadowable(ctx, physical_synthesis, snapshot)
    return run_synthesis_adapter(ctx, plan_obj, physical_synthesis)


def run_synthesis_adapter(
    ctx: ShadowContext,
    plan_obj: Plan,
    physical_synthesis: SynthesisStage,
) -> tuple[dict[str, pa.Table], SeamContext]:
    """The adapter call shared by the pure dispatch above and the
    independent-mixed dispatch (`_shadow_mixed.dispatch_mixed`): given a
    plan ALREADY admitted by the caller's own gate, run the Task 4.2
    `SynthesisStageAdapter` and verify its output table set matches the
    compiled synthesis stage exactly. Takes the pre-admitted `plan_obj`
    rather than re-deriving it, since the two callers admit it through
    different gates (`require_pure_generation_shadowable` vs
    `require_independent_mixed_shadowable`) that this function must stay
    agnostic to.
    """
    # Imported here, not at module scope, matching the OOC branch's own
    # lazy-import precedent: a caller that never takes this branch never
    # pays the import.
    from decoy_engine.execution.physical.drivers._synthesis import SynthesisStageAdapter

    adapter = SynthesisStageAdapter()
    outputs = adapter.run(
        plan_obj,
        derive_key=ctx.derive_key,
        instance_default_locale=ctx.instance_default_locale,
        provider_snapshot=ctx.provider_snapshot,
    )
    seam_context = adapter.last_invocation
    if seam_context is None:  # pragma: no cover - set unconditionally before delegation
        raise AssertionError("SynthesisStageAdapter.run returned without setting last_invocation")
    if set(outputs) != set(physical_synthesis.tables):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED,
            detail=(
                f"adapter output tables {sorted(outputs)} != "
                f"synthesis.tables {sorted(physical_synthesis.tables)}"
            ),
        )
    return outputs, seam_context


def require_generation_shape(
    ctx: ShadowContext, physical_synthesis: SynthesisStage
) -> tuple[Plan, dict[str, Any], frozenset[str]]:
    """§1a, shared: generation IDENTITY/column-shape only -- never a
    job-scope field (sources/relationships/snapshot/...), which is not
    side-local to generation and is decided by whichever gate wraps this
    (`require_pure_generation_shadowable` or, in `_shadow_mixed.py`,
    `require_independent_mixed_shadowable`).

    Checks: `ctx.plan` exists and carries a `generation` payload whose
    config digest matches `physical_synthesis.config_digest` (the identity
    bind -- `pipeline_config_hash` deliberately excludes sources/targets, so
    it cannot serve this role); every GENERATE-kind table in the decoded
    config (per `classify_table_kinds`, the same generate/mask split
    `run_pipeline` itself uses) has an admitted column `type`
    (`sequence`/`categorical`/`faker`) and no `determinism: fresh`; and the
    generate-table name set matches `physical_synthesis.tables` exactly. A
    `faker` column additionally passes `_require_shadow_admissible_faker_
    column` against `ctx.provider_snapshot` (5a-faker) -- the SAME snapshot
    both this admission check and the downstream `generate_tables` calls
    read, so a custom-provider mutation between them cannot desync the
    admission decision from what generation actually produces. MASK-kind
    table entries in the same decoded config are skipped here (never
    validated, never rejected) -- an independent-mixed job's config
    legitimately carries both kinds in one `tables:` list; validating the
    mask half is the mask-side admission machinery's job, not this one's.

    Returns `(plan_obj, config, generate_table_names)` so a caller can
    apply its own job-scope exclusions against the same decoded `config`
    without re-parsing `config_json` a second time.
    """
    plan_obj = ctx.plan
    if plan_obj is None:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ShadowContext.plan is None"
        )
    generation = plan_obj.generation
    if generation is None:
        raise ShadowDifference(code=GENERATION_SHAPE_UNSUPPORTED, detail="plan.generation is None")
    config_json = generation.config_json
    if not isinstance(config_json, str):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="generation.config_json is not a string"
        )
    try:
        config_bytes = config_json.encode("utf-8")
    except UnicodeError:
        # A surrogate/un-encodable config_json is a coded decline, not a raw
        # UnicodeEncodeError escaping the total-guarded gate.
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED,
            detail="generation.config_json is not UTF-8 encodable",
        ) from None
    digest = hashlib.sha256(config_bytes).hexdigest()
    if digest != physical_synthesis.config_digest:
        raise ShadowDifference(code=GENERATION_SHAPE_UNSUPPORTED, detail="config_digest mismatch")
    config = _decode_generation_config_shallow(config_json)
    generate_table_names = _require_generate_table_column_shape(config, ctx.provider_snapshot)
    if generate_table_names != set(physical_synthesis.tables):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="generate-table name-set mismatch"
        )
    return plan_obj, config, generate_table_names


def require_pure_generation_shadowable(
    ctx: ShadowContext, physical_synthesis: SynthesisStage, snapshot: ShadowSnapshot
) -> Plan:
    """§1b: the PURE-generate admission gate -- `require_generation_shape`
    plus the pure-only exclusions (see the module docstring for the full
    admitted-domain contract). This is slice 5a's original gate, preserved
    exactly under its new name."""
    if snapshot.tables:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="shadow snapshot is not empty"
        )
    plan_obj, config, generate_table_names = require_generation_shape(ctx, physical_synthesis)
    _require_pure_job_scope(config, generate_table_names)
    _require_admitted_runtime_carriers(ctx)
    return plan_obj


def _decode_generation_config_shallow(config_json: str) -> dict[str, Any]:
    """TOTAL-guarded parse of the embedded generation config for the
    admission gate's shallow structural check: a JSON decode failure or a
    non-mapping root declines coded, never escapes as a raw
    `JSONDecodeError`/`TypeError`."""
    try:
        config = json.loads(config_json)
    except json.JSONDecodeError as exc:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="generation config_json is not valid JSON"
        ) from exc
    if not isinstance(config, dict):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="generation config root is not a mapping"
        )
    return config


def _require_generate_table_column_shape(
    config: dict[str, Any],
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
) -> frozenset[str]:
    """The identity/column-shape half of §1a: every GENERATE-kind table
    (per `classify_table_kinds`) has an admitted column `type` and no
    `determinism: fresh`. A MASK-kind table entry in the same list is
    skipped, never inspected -- this function's whole job is the generate
    half's shape, nothing else. Never inspects a leaf knob
    (`start`/`step`/`categories`/`weights`/`null_probability`/...) -- those
    must reach `generate_tables` unfiltered so a malformed one becomes a
    faithfully identical rejection on both sides, not an out-validation
    here. A `type: faker` column is the one exception: it gets its own
    TOTAL admission check (`_require_shadow_admissible_faker_column`)
    against `provider_snapshot`, since "is this faker_type shadow-safe" is
    a shape question this gate owns, not a leaf-knob value the two sides
    could naturally diverge or agree on unfiltered. Returns the
    generate-table name set for the caller's identity-bind check against
    `physical_synthesis.tables`.
    """
    tables = config.get("tables")
    if not isinstance(tables, list) or not tables:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.tables is empty or malformed"
        )
    # `classify_table_kinds` tolerates a malformed `tables` entry (skips a
    # non-dict / non-str-name entry rather than raising); this loop below
    # re-walks the SAME entries for the generate-kind subset it must
    # actually validate, so a malformed entry that classify_table_kinds
    # silently dropped is still caught here as a structural decline.
    table_kinds = classify_table_kinds(config)

    generate_table_names: set[str] = set()
    for table in tables:
        if not isinstance(table, dict):
            raise ShadowDifference(
                code=GENERATION_SHAPE_UNSUPPORTED, detail="a table entry is not a mapping"
            )
        name = table.get("name")
        if not isinstance(name, str):
            raise ShadowDifference(
                code=GENERATION_SHAPE_UNSUPPORTED, detail="a table entry has no string name"
            )
        if table_kinds.get(name) != "generate":
            continue  # mask-kind entry: out of scope for this function
        generate_columns = table.get("generate_columns")
        if not generate_columns:
            raise ShadowDifference(
                code=GENERATION_SHAPE_UNSUPPORTED, detail=f"table={name!r} is not generate-kind"
            )
        if not isinstance(generate_columns, list):
            raise ShadowDifference(
                code=GENERATION_SHAPE_UNSUPPORTED,
                detail=f"table={name!r}: generate_columns is not a list",
            )
        for column in generate_columns:
            if not isinstance(column, dict):
                raise ShadowDifference(
                    code=GENERATION_SHAPE_UNSUPPORTED,
                    detail=f"table={name!r}: a generate column is not a mapping",
                )
            column_type = column.get("type")
            # `isinstance(str)` first: an unhashable value (e.g. a list) must
            # decline coded, not raise TypeError on the frozenset membership.
            if (
                not isinstance(column_type, str)
                or column_type not in _ADMITTED_GENERATE_COLUMN_TYPES
            ):
                raise ShadowDifference(
                    code=GENERATION_SHAPE_UNSUPPORTED,
                    detail=f"table={name!r}: generate column type {column_type!r} is not admitted",
                )
            if column_type == "faker":
                _require_shadow_admissible_faker_column(column, name, provider_snapshot)
            if column.get("determinism") == "fresh":
                raise ShadowDifference(
                    code=GENERATION_SHAPE_UNSUPPORTED, detail=f"table={name!r}: determinism=fresh"
                )
        generate_table_names.add(name)
    return frozenset(generate_table_names)


def _require_shadow_admissible_faker_column(
    column: dict[str, Any],
    table_name: str,
    provider_snapshot: Mapping[str, Callable[[Faker], Any]] | None,
) -> None:
    """5a-faker's TOTAL admission gate for one `type: faker` column, run
    BEFORE the adapter is ever constructed. Every failure below is a coded
    decline in this fixed order (Codex plan-gate v1 HIGH): a malformed
    `faker_type` (`None`/a list/a mapping) must decline on the very first
    check, never reach the frozenset membership test or the resolver and
    raise a raw `TypeError`.

    1. `faker_type` is a string.
    2. `faker_type` is in the reviewed, empirically-deterministic allowlist
       (`SHADOW_ADMISSIBLE_FAKER_TYPES`).
    3. No custom provider claims `faker_type` in `provider_snapshot` -- a
       custom override can be non-deterministic (wall clock, `fake.unique`)
       or simply diverge across the shadow/oracle's two independent
       `generate_tables` calls, so it is never shadow-safe regardless of
       what type name it overrides.
    4. `determinism` is not `"fresh"` (`os.urandom` is non-reproducible).

    A column that survives all four is exactly the case the gen-5a-faker
    investigation proved byte-identical across two independent seeded
    `generate_tables` calls, per-row and pooled.
    """
    faker_type = column.get("faker_type")
    if not isinstance(faker_type, str):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED,
            detail=f"table={table_name!r}: faker_type {faker_type!r} is not a string",
        )
    if faker_type not in SHADOW_ADMISSIBLE_FAKER_TYPES:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED,
            detail=f"table={table_name!r}: faker_type {faker_type!r} is not shadow-admissible",
        )
    if has_custom_faker_override(faker_type, provider_snapshot):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED,
            detail=f"table={table_name!r}: faker_type {faker_type!r} has a custom provider override",
        )
    if column.get("determinism") == "fresh":
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED,
            detail=f"table={table_name!r}: faker column determinism=fresh",
        )


def _require_pure_job_scope(config: dict[str, Any], generate_table_names: frozenset[str]) -> None:
    """The rest of §1b, over the decoded config: the job-scope fields the
    coordinator does not own (sources/relationships/namespaces/subset/
    validators/quarantine/run_storm/mask_secret_ref) all empty/absent/
    `None`/`False`, every table in `config.tables` is generate-kind (a pure
    job has no mask-kind entry at all), and no generate-table declares a
    `transforms` op."""
    if config.get("sources"):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.sources is non-empty"
        )
    if config.get("relationships"):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.relationships is non-empty"
        )
    if config.get("namespaces"):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.namespaces is non-empty"
        )
    if config.get("subset") is not None:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.subset is not None"
        )
    if config.get("validators"):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.validators is non-empty"
        )
    if config.get("quarantine") is not None:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.quarantine is not None"
        )
    if config.get("run_storm"):
        raise ShadowDifference(code=GENERATION_SHAPE_UNSUPPORTED, detail="config.run_storm is true")
    global_settings = config.get("global_settings")
    if isinstance(global_settings, dict) and global_settings.get("mask_secret_ref") is not None:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED,
            detail="global_settings.mask_secret_ref is set",
        )

    tables = config.get("tables")
    if not isinstance(tables, list):  # pragma: no cover - already validated by the shape check
        raise ShadowDifference(code=GENERATION_SHAPE_UNSUPPORTED, detail="config.tables malformed")
    for table in tables:
        if not isinstance(table, dict):  # pragma: no cover - already validated by the shape check
            continue
        name = table.get("name")
        if name not in generate_table_names:
            raise ShadowDifference(
                code=GENERATION_SHAPE_UNSUPPORTED,
                detail=f"table={name!r}: not generate-kind in a pure-generate dispatch",
            )
        if table.get("transforms"):
            raise ShadowDifference(
                code=GENERATION_SHAPE_UNSUPPORTED, detail=f"table={name!r}: transforms is non-empty"
            )


def _require_admitted_runtime_carriers(ctx: ShadowContext) -> None:
    """The r5-fixed runtime-admission record (§2.3): the coordinator cannot
    observe these settings from the plan or snapshot alone, so the harness
    mirrors what it passed to the oracle onto `ctx`, and this reads them
    back. Every admitted 5a job pins all seven to the oracle-identical safe
    value; a non-admitted value declines before the adapter is ever
    constructed."""
    if ctx.derive_key is not None:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.derive_key is not None"
        )
    if ctx.instance_default_locale is not None:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.instance_default_locale is not None"
        )
    if ctx.key_provider is not None:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.key_provider is not None"
        )
    if ctx.sink_requested:
        raise ShadowDifference(code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.sink_requested")
    if ctx.source_loader_requested:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.source_loader_requested"
        )
    if ctx.vault_writer_requested:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.vault_writer_requested"
        )
    if ctx.fidelity_report:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="ctx.fidelity_report is true"
        )
