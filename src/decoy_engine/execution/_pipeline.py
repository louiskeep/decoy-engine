"""FC-1 (2026-06-02) unified pipeline entry: mixed mask + generate.

The single load-bearing function this module exposes is `run_pipeline`.
It is the V2 spine the platform job runner + the CLI both call when
the operator submits a `PipelineConfig` that may declare BOTH mask-kind
tables (with `columns:`) AND generate-kind tables (with
`generate_columns:` + `row_count:`) in a single config.

Sequencing contract (PO directive 2026-06-01 + FC-1 spec):

  1. Validate-by-precondition: the caller has already run
     `PipelineConfig.model_validate(raw).model_dump()` and is handing in
     the resulting dict; this entry does not re-validate.
  2. `profile_source(config)` runs over the declared `sources:` block.
     Pure-generate configs (empty `sources:`) get a zero-table Profile.
  3. `compile_plan(config, profile, decoy_engine_version=...)` produces
     the frozen Plan that covers every table in `tables:`. The compiler
     already handles per-table-kind (S6-ENG-1 wired generate into the
     plan compile path).
  4. `build_namespace_registry` + `check_orphan_fk_policy_completeness`
     + `build_relationship_graph` run as usual; the FK graph spans both
     kinds (a generate table can be referenced by a mask child and
     vice versa post-FC-1).
  5. Split `tables:` into generate-kind (have `generate_columns`) and
     mask-kind (have `columns`). Call `generate_tables(config, ...)`
     FIRST so generate outputs exist as Arrow tables.
  6. Merge generate outputs into the `sources` dict the mask adapter
     reads. A mask table whose FK parent is a generate table reads the
     generate output as if it were a source: the generate-side value
     IS the FK pool for the mask side.
  7. Call `PandasExecutionAdapter.run(plan, merged_sources, ...)` to
     mask the mask-kind tables. The plan only carries mask-table seeds;
     generate tables are not re-traversed.
  8. Build one `ExecutionResult` whose `outputs` covers every output
     table (generate + mask) and whose `table_kinds` dict carries the
     per-table kind for the manifest stamping at F3 / platform side.

Per-table evidence-kind stamping (PO D1 sub-decision 2026-06-01,
RESOLVED per-table): the unified ExecutionResult carries `table_kinds:
dict[str, "mask" | "generate"]` so `update_finished_manifest` writes
`kind="mask"` or `kind="generate"` per table in one manifest.

Out of scope for FC-1 (deferred to V2.1):

- Generate child to mask parent FK direction. The mask parent has a
  finite pre-existing pool; resolving generate children against it
  crosses the generate `reference` generator into the mask substrate.
  REJECTED at schema validation post-2026-06-02 (engine FC-1 QA
  review Finding 2): `_reference_graph_valid` raises at submit time
  when a generate column's `reference_table` points at a mask-kind
  parent. Operators see a clear "deferred to V2.1" error up front
  instead of a hung job at runtime.
- Cross-substrate mixed mode. Polars falls back to pandas for FK paths
  (`_polars_adapter.py:121`); the pandas adapter is the canonical
  mixed-mode adapter for V2 ship.
- Per-node preview on mixed configs. Covered by F5 at the platform
  layer (`run_v2_pipeline_preview`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

if TYPE_CHECKING:
    from decoy_engine.providers_v2 import ProviderRegistry


__all__ = ["run_pipeline", "classify_table_kinds"]


def classify_table_kinds(config: dict[str, Any]) -> dict[str, str]:
    """Return `{table_name: "mask" | "generate"}` for every table in the config.

    Per-table kind is inferred from `columns` (mask) vs `generate_columns`
    (generate) presence on each TableConfig. The schema already enforces
    XOR at validation time (`_per_table_kind_consistency` + `TableConfig`
    invariants), so a config that reaches this helper has at most one
    populated per table. Tables with neither are classified as mask
    (defensive default; the schema rejects them upstream).
    """
    out: dict[str, str] = {}
    for table in config.get("tables") or []:
        if not isinstance(table, dict):
            continue
        name = table.get("name")
        if not isinstance(name, str):
            continue
        if table.get("generate_columns"):
            out[name] = "generate"
        else:
            out[name] = "mask"
    return out


def run_pipeline(
    config: dict[str, Any],
    sources: "dict[str, pa.Table] | None" = None,
    *,
    engine_version: str,
    registry: "ProviderRegistry | None" = None,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
) -> ExecutionResult:
    """Execute a mixed mask + generate config end-to-end.

    `config` MUST be the validated dump from `PipelineConfig.model_validate`;
    no re-validation here. `sources` is the caller-loaded
    `dict[table_name -> pa.Table]` for the mask-kind tables; pure-generate
    configs may pass `None` (or an empty dict). `engine_version` flows
    into `compile_plan`'s audit-evidence stamping.

    Returns one `ExecutionResult` whose `outputs` covers every output
    table (generate + mask) and whose `table_kinds` field carries the
    per-table classification for the manifest stamping.
    """
    from decoy_engine.generation.synthesize import generate_tables
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import profile_source
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships import (
        RelationshipGraph,
        build_namespace_registry,
        build_relationship_graph,
        check_orphan_fk_policy_completeness,
    )

    resolved_registry = registry if registry is not None else get_default_registry()
    caller_sources: dict[str, pa.Table] = dict(sources) if sources else {}

    table_kinds = classify_table_kinds(config)
    has_mask_table = any(kind == "mask" for kind in table_kinds.values())
    has_generate_table = any(kind == "generate" for kind in table_kinds.values())

    job_seed_raw = (config.get("global_settings") or {}).get("seed")
    job_seed = job_seed_raw if isinstance(job_seed_raw, int) else None

    profile = profile_source(config, seed=job_seed)

    plan = compile_plan(config, profile, decoy_engine_version=engine_version)

    ns_registry = build_namespace_registry(config, profile)
    if profile.relationships:
        lookup = check_orphan_fk_policy_completeness(config, profile.relationships)
        graph = build_relationship_graph(
            profile.relationships,
            namespace_registry=ns_registry,
            orphan_policy_lookup=lookup,
        )
    else:
        graph = RelationshipGraph(edges=(), ordering=())

    # Step 1: generate-kind tables. The synthesize entry filters by
    # `generate_columns` presence already (synthesize.py:113), so passing
    # the full config is safe even when mask tables are present.
    generate_outputs: dict[str, pa.Table] = {}
    if has_generate_table:
        generate_outputs = generate_tables(
            config,
            derive_key=derive_key,
            instance_default_locale=instance_default_locale,
        )

    # Step 2: mask-kind tables.
    mask_outputs: dict[str, pa.Table] = {}
    mask_timings: tuple = ()
    mask_conversion_ms: float = 0.0
    mask_warnings: tuple = ()
    mask_quality_metrics: dict[str, Any] = {}
    if has_mask_table:
        # Merge generate outputs into the sources dict the mask adapter
        # reads. A mask table whose FK parent is a generate table reads the
        # generate output as if it were a source: the generated value IS
        # the FK pool for the mask side.
        merged_sources: dict[str, pa.Table] = {}
        merged_sources.update(caller_sources)
        merged_sources.update(generate_outputs)

        adapter = PandasExecutionAdapter()
        mask_result = adapter.run(
            plan,
            merged_sources,
            registry=resolved_registry,
            relationship_graph=graph,
            namespace_registry=ns_registry,
        )
        # The mask adapter returns `outputs` only for mask-kind tables (its
        # work-list iterates over `plan.tables`); generate-kind table
        # entries in `merged_sources` are passed-through input, not output.
        mask_outputs = dict(mask_result.outputs)
        mask_timings = mask_result.timings
        mask_conversion_ms = mask_result.boundary_conversion_ms
        mask_warnings = mask_result.warnings
        mask_quality_metrics = mask_result.quality_metrics

    # Step 3: stitch the outputs together. Mask wins ties (every name in
    # the config maps to one kind by construction, so no real conflicts).
    outputs: dict[str, pa.Table] = {}
    outputs.update(generate_outputs)
    outputs.update(mask_outputs)

    return ExecutionResult(
        outputs=outputs,
        timings=mask_timings,
        boundary_conversion_ms=mask_conversion_ms,
        warnings=mask_warnings,
        quality_metrics=mask_quality_metrics,
        table_kinds=table_kinds,
    )
