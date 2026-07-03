"""Orchestration: `plan_subset` / `run_subset` / `subset_inputs_from_config`.

Composition contract (SS6 CLI / platform runner): callers run `run_subset(...)`
first, then feed the written subset Parquet files as the `sources` of the
existing mask path (`decoy_engine.execution.run_pipeline` / adapters) --
masking itself is UNCHANGED. `subset_inputs_from_config` converts a validated
`PipelineConfig` dump's `subset:` block into the kwargs `run_subset` needs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import polars as pl

from decoy_engine.plan._types import PlanRelationship
from decoy_engine.subset._closure import ClosureResult, compute_closure, verify_closure
from decoy_engine.subset._edges import build_subset_edges, relationships_from_config
from decoy_engine.subset._errors import SubsetConfigError, SubsetPreflightError
from decoy_engine.subset._keys import load_key_frames
from decoy_engine.subset._manifest import build_manifest, to_json_dict
from decoy_engine.subset._materialize import materialize_subset
from decoy_engine.subset._policy import build_estimate, make_budget_check, resolve_edge_directions
from decoy_engine.subset._preflight import run_subset_preflight
from decoy_engine.subset._seed import select_seed_rows
from decoy_engine.subset._types import (
    EdgeDirection,
    FanOutBudget,
    FanOutPolicy,
    Predicate,
    SeedSpec,
    SubsetEdge,
    SubsetPlan,
    SubsetResult,
    SubsetSource,
)


def _seed_spec_public(spec: SeedSpec) -> dict[str, Any]:
    """Serialize a `SeedSpec` for the plan/manifest, WITHOUT raw data values.

    `keys` mode carries raw values on `spec.keys`; only the count is
    serialized. `sample` mode's `fraction`/`count` are config thresholds, not
    data, so they may appear as-is. `filter` mode's predicate `column` and
    `op` name WHAT was filtered (operator-useful, not sensitive on their
    own), but the predicate `value` is an operator-supplied literal that can
    itself be PII (e.g. a seed of `filter email == "victim@example.com"`) --
    it is NEVER serialized here. `value_redacted` records only whether a
    literal was present, so the shape of the filter stays visible without the
    data.
    """
    if spec.mode == "keys":
        return {
            "table": spec.table,
            "mode": "keys",
            "key_columns": list(spec.key_columns),
            "key_count": len(spec.keys),
        }
    d: dict[str, Any] = {
        "table": spec.table,
        "mode": spec.mode,
        "key_columns": list(spec.key_columns),
    }
    if spec.mode == "sample":
        d["fraction"] = spec.fraction
        d["count"] = spec.count
    if spec.mode == "filter":
        d["predicates"] = [
            {"column": p.column, "op": p.op, "value_redacted": p.value is not None}
            for p in spec.predicates
        ]
    return d


def _compute(
    *,
    sources: dict[str, SubsetSource],
    relationships: tuple[PlanRelationship, ...],
    seeds: tuple[SeedSpec, ...],
    policy: FanOutPolicy,
    job_seed: bytes,
    engine_version: str,
) -> tuple[
    SubsetPlan,
    dict[str, pl.DataFrame],
    tuple[SubsetEdge, ...],
    dict[str, EdgeDirection],
    ClosureResult,
]:
    report = run_subset_preflight(sources=sources, relationships=relationships)
    if not report.passed:
        first = report.failures[0]
        raise SubsetPreflightError(code=first.code, report=report, message=first.message)

    edges = build_subset_edges(relationships)
    directions = resolve_edge_directions(edges, policy)
    key_frames = load_key_frames(sources, edges, seeds)
    seed_rows, seed_counts, seed_null_excluded = select_seed_rows(
        seeds=seeds, key_frames=key_frames, job_seed=job_seed
    )
    total_seed_rows = sum(seed_counts.values())
    budget_check = make_budget_check(policy.budget, total_seed_rows)
    closure = compute_closure(
        edges=edges,
        directions=directions,
        key_frames=key_frames,
        seed_rows=seed_rows,
        budget_check=budget_check,
    )
    seed_specs_public = tuple(_seed_spec_public(s) for s in seeds)
    plan = build_estimate(
        engine_version=engine_version,
        seed_specs_public=seed_specs_public,
        key_frames=key_frames,
        seed_counts=seed_counts,
        seed_null_excluded=seed_null_excluded,
        closure=closure,
        budget=policy.budget,
        preflight=report,
    )
    return plan, key_frames, edges, directions, closure


def plan_subset(
    *,
    sources: dict[str, SubsetSource],
    relationships: tuple[PlanRelationship, ...],
    seeds: tuple[SeedSpec, ...],
    policy: FanOutPolicy,
    job_seed: bytes,
    engine_version: str,
) -> SubsetPlan:
    """The dry-run. Never writes anything; never reads non-key columns."""
    plan, _key_frames, _edges, _directions, _closure = _compute(
        sources=sources,
        relationships=relationships,
        seeds=seeds,
        policy=policy,
        job_seed=job_seed,
        engine_version=engine_version,
    )
    return plan


def run_subset(
    *,
    sources: dict[str, SubsetSource],
    relationships: tuple[PlanRelationship, ...],
    seeds: tuple[SeedSpec, ...],
    policy: FanOutPolicy,
    job_seed: bytes,
    engine_version: str,
    output_dir: str | Path,
) -> SubsetResult:
    """Plan, verify, then materialize + write the manifest.

    The budget gate (inside `_compute`, via the closure's `budget_check`)
    sits strictly before `output_dir` is ever touched. `verify_closure` is
    the last line of defense on the upward-completeness invariant before any
    write happens.
    """
    plan, key_frames, edges, directions, closure = _compute(
        sources=sources,
        relationships=relationships,
        seeds=seeds,
        policy=policy,
        job_seed=job_seed,
        engine_version=engine_version,
    )
    verify_closure(edges=edges, directions=directions, key_frames=key_frames, result=closure)

    output_dir = Path(output_dir)
    output_paths = materialize_subset(
        sources=sources, survivors=closure.survivors, output_dir=output_dir
    )
    manifest = build_manifest(plan, engine_version)
    (output_dir / "subset-manifest.json").write_text(
        json.dumps(to_json_dict(manifest), sort_keys=True, indent=2)
    )
    return SubsetResult(plan=plan, manifest=manifest, output_paths=output_paths)


def subset_inputs_from_config(
    config: dict[str, Any],
) -> tuple[
    dict[str, SubsetSource], tuple[PlanRelationship, ...], tuple[SeedSpec, ...], FanOutPolicy
]:
    """Adapt a validated `PipelineConfig` dump (with a `subset:` block) to `run_subset` kwargs."""
    subset_block = config.get("subset")
    if subset_block is None:
        raise SubsetConfigError(
            code="subset_config_missing",
            message="config has no `subset:` block; subset_inputs_from_config requires one",
        )

    sources: dict[str, SubsetSource] = {}
    for name, src in config.get("sources", {}).items():
        if src.get("type") != "file":
            raise SubsetConfigError(
                code="subset_source_type_unsupported",
                message=f"table {name!r}: subsetting only supports local file sources "
                f"(got type={src.get('type')!r}); DB / cloud sources are deferred (GATE-1 #2)",
            )
        sources[name] = SubsetSource(path=src["path"], format=src["format"])

    relationships = relationships_from_config(config)

    seeds: list[SeedSpec] = []
    for entry in subset_block.get("seeds", []):
        predicates = tuple(
            Predicate(column=p["column"], op=p["op"], value=p.get("value"))
            for p in entry.get("predicates", [])
        )
        keys = tuple(tuple(k) for k in entry.get("keys", []))
        seeds.append(
            SeedSpec(
                table=entry["table"],
                mode=entry["mode"],
                key_columns=tuple(entry.get("key_columns", [])),
                fraction=entry.get("fraction"),
                count=entry.get("count"),
                predicates=predicates,
                keys=keys,
            )
        )

    budget_block = subset_block.get("budget", {}) or {}
    budget = FanOutBudget(
        max_total_rows=budget_block.get("max_total_rows"),
        max_table_seed_multiple=budget_block.get("max_table_seed_multiple"),
    )
    edge_directions = tuple(subset_block.get("edge_directions", {}).items())
    policy = FanOutPolicy(
        budget=budget,
        edge_directions=edge_directions,
        allow_dangling=bool(subset_block.get("allow_dangling", False)),
    )
    return sources, relationships, tuple(seeds), policy
