"""Task 4.6 slice 5a: the pure-generate dispatch's admission gate + adapter
call, split out of `_shadow_coordinator.py` to keep that module under the
600-LOC orchestration cap (CLAUDE.md "Orchestration modules cap at ~600 LOC").

`dispatch_synthesis` is what `ShadowCoordinator._dispatch_synthesis` used to
do as a method, now a free function taking `ctx` directly instead of `self`.
It never reimplements generation, never merges into any mask output (that
stitch is 5b territory, added once around the combined result in a later
unit), and propagates every adapter/generation exception UNCHANGED -- the
differential harness's phase-bound comparator decides whether a raise is a
faithful identical rejection, not this module.

`require_generation_shadowable` is the §1 admission gate: a SHALLOW
structural check over the embedded generation config, never a per-generator
leaf-knob validation -- a malformed leaf value (a bad `start`, a bad weight)
must reach the shared `generate_tables` implementation and become a
faithfully IDENTICAL rejection on both sides, not get out-validated here.
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

from decoy_engine.execution.physical._shadow_diff_codes import (
    GENERATION_SHAPE_UNSUPPORTED,
    ShadowDifference,
)

if TYPE_CHECKING:
    import pyarrow as pa

    from decoy_engine.execution.physical._context import SeamContext
    from decoy_engine.execution.physical._plan import SynthesisStage
    from decoy_engine.execution.physical._shadow_context import ShadowContext
    from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
    from decoy_engine.plan._types import Plan

__all__ = ["dispatch_synthesis", "require_generation_shadowable"]

# The admitted generate-column `type` set for the pure-generate dispatch
# (§1): sequence + categorical only. Deliberately closed here rather than
# sourced from `config._tables.GENERATE_TYPES` -- that constant lists every
# type the SCHEMA accepts, not what this slice's admission gate shadows;
# widening it is a scoped follow-up (5a-faker etc.), never an accident of
# reusing the wrong source.
_ADMITTED_GENERATE_COLUMN_TYPES = frozenset({"sequence", "categorical"})


def dispatch_synthesis(
    ctx: ShadowContext,
    physical_synthesis: SynthesisStage,
    snapshot: ShadowSnapshot,
) -> tuple[dict[str, pa.Table], SeamContext]:
    """Admit + dispatch a pure-generate plan through the Task 4.2
    `SynthesisStageAdapter`, which delegates to `generate_tables` -- never
    reimplemented here, and never merged with any mask output (that stitch
    is 5b territory, added once around the combined result in a later
    unit). Every adapter/generation exception propagates UNCHANGED -- the
    differential harness's phase-bound comparator decides whether a raise
    is a faithful identical rejection, not this function.
    """
    # Imported here, not at module scope, matching the OOC branch's own
    # lazy-import precedent: a caller that never takes this branch never
    # pays the import.
    from decoy_engine.execution.physical.drivers._synthesis import SynthesisStageAdapter

    plan_obj = require_generation_shadowable(ctx, physical_synthesis, snapshot)
    adapter = SynthesisStageAdapter()
    outputs = adapter.run(
        plan_obj,
        derive_key=ctx.derive_key,
        instance_default_locale=ctx.instance_default_locale,
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


def require_generation_shadowable(
    ctx: ShadowContext, physical_synthesis: SynthesisStage, snapshot: ShadowSnapshot
) -> Plan:
    """The §1 admission gate -- see the module docstring for the full
    admitted-domain contract."""
    if snapshot.tables:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="shadow snapshot is not empty"
        )
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
    digest = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
    if digest != physical_synthesis.config_digest:
        raise ShadowDifference(code=GENERATION_SHAPE_UNSUPPORTED, detail="config_digest mismatch")
    config = _decode_generation_config_shallow(config_json)
    generate_table_names = _require_pure_generation_shape(config)
    if generate_table_names != set(physical_synthesis.tables):
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="generate-table name-set mismatch"
        )
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


def _require_pure_generation_shape(config: dict[str, Any]) -> frozenset[str]:
    """The rest of the §1 shallow structural admission, over the decoded
    config: every table generate-kind with an admitted column `type` and no
    `determinism: fresh`, and the job-scope fields the coordinator does not
    own (sources/relationships/namespaces/subset/transforms/validators/
    quarantine/run_storm/mask_secret_ref) all empty/absent/`None`/`False`.
    Never inspects a leaf knob (`start`/`step`/`categories`/`weights`/
    `null_probability`/...) -- those must reach `generate_tables` unfiltered
    so a malformed one becomes a faithfully identical rejection on both
    sides, not an out-validation here. Returns the generate-table name set
    for the caller's identity-bind check against `physical_synthesis.tables`.
    """
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
    if not isinstance(tables, list) or not tables:
        raise ShadowDifference(
            code=GENERATION_SHAPE_UNSUPPORTED, detail="config.tables is empty or malformed"
        )

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
        if table.get("transforms"):
            raise ShadowDifference(
                code=GENERATION_SHAPE_UNSUPPORTED, detail=f"table={name!r}: transforms is non-empty"
            )
        for column in generate_columns:
            if not isinstance(column, dict):
                raise ShadowDifference(
                    code=GENERATION_SHAPE_UNSUPPORTED,
                    detail=f"table={name!r}: a generate column is not a mapping",
                )
            column_type = column.get("type")
            if column_type not in _ADMITTED_GENERATE_COLUMN_TYPES:
                raise ShadowDifference(
                    code=GENERATION_SHAPE_UNSUPPORTED,
                    detail=f"table={name!r}: generate column type {column_type!r} is not admitted",
                )
            if column.get("determinism") == "fresh":
                raise ShadowDifference(
                    code=GENERATION_SHAPE_UNSUPPORTED, detail=f"table={name!r}: determinism=fresh"
                )
        generate_table_names.add(name)
    return frozenset(generate_table_names)


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
