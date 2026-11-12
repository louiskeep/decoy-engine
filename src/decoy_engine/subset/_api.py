"""Orchestration: `plan_subset` / `run_subset` / `subset_inputs_from_config`.

Composition contract (SS6 CLI / platform runner): callers run `run_subset(...)`
first, then feed the written subset Parquet files as the `sources` of the
existing mask path (`decoy_engine.execution.run_pipeline` / adapters) --
masking itself is UNCHANGED. `subset_inputs_from_config` converts a validated
`PipelineConfig` dump's `subset:` block into the kwargs `run_subset` needs.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import polars as pl
import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.config import classify_table_kinds
from decoy_engine.fileaccess import FileAccess, entry_point
from decoy_engine.fileaccess.adapters import serial
from decoy_engine.plan._types import PlanRelationship
from decoy_engine.profile import LazySource
from decoy_engine.release import __version__
from decoy_engine.subset._closure import ClosureResult, compute_closure, verify_closure
from decoy_engine.subset._edges import build_subset_edges, relationships_from_config
from decoy_engine.subset._errors import SubsetConfigError, SubsetPreflightError
from decoy_engine.subset._inputs import TableInput, as_input
from decoy_engine.subset._keys import load_key_frames
from decoy_engine.subset._manifest import build_manifest, to_json_dict
from decoy_engine.subset._materialize import materialize_subset
from decoy_engine.subset._policy import build_estimate, make_budget_check, resolve_edge_directions
from decoy_engine.subset._preflight import run_subset_preflight
from decoy_engine.subset._scope import SubsetRequest, SubsetSelection, scope_for
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
    sources: Mapping[str, SubsetSource | pa.Table | LazySource | TableInput],
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
    try:
        inputs = {name: as_input(name, value) for name, value in sources.items()}
        report = run_subset_preflight(sources=inputs, relationships=relationships, seeds=seeds)
        if not report.passed:
            first = report.failures[0]
            raise SubsetPreflightError(code=first.code, report=report, message=first.message)

        edges = build_subset_edges(relationships)
        directions = resolve_edge_directions(edges, policy)
        key_frames = load_key_frames(inputs, edges, seeds)
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
        # Runs in BOTH plan_subset and run_subset (nice-to-have, dennis review):
        # the dry-run estimate is exactly the surface an operator inspects to
        # trust a subset before committing to it, so it deserves the same
        # cheap, in-memory, no-I/O invariant guard as the real run, not just a
        # guard on the write path.
        verify_closure(edges=edges, directions=directions, key_frames=key_frames, result=closure)
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
    except BaseException:
        del job_seed
        raise


@entry_point
def plan_subset(
    *,
    sources: dict[str, SubsetSource],
    relationships: tuple[PlanRelationship, ...],
    seeds: tuple[SeedSpec, ...],
    policy: FanOutPolicy,
    job_seed: bytes,
    engine_version: str,
    file_access: FileAccess | None = None,
) -> SubsetPlan:
    """The dry-run. Never writes anything; reads only key columns plus any
    columns named in a filter seed's predicates (needed to evaluate the
    predicate), never ordinary payload columns."""
    try:
        plan, _key_frames, _edges, _directions, _closure = _compute(
            sources=sources,
            relationships=relationships,
            seeds=seeds,
            policy=policy,
            job_seed=job_seed,
            engine_version=engine_version,
        )
        return plan
    except BaseException:
        del job_seed
        raise


@entry_point
def run_subset(
    *,
    sources: dict[str, SubsetSource],
    relationships: tuple[PlanRelationship, ...],
    seeds: tuple[SeedSpec, ...],
    policy: FanOutPolicy,
    job_seed: bytes,
    engine_version: str,
    output_dir: str | Path,
    file_access: FileAccess | None = None,
) -> SubsetResult:
    """Plan, verify, then materialize + write the manifest.

    The budget gate (inside `_compute`, via the closure's `budget_check`)
    sits strictly before `output_dir` is ever touched. `verify_closure`
    (also inside `_compute`, shared with `plan_subset`) is the last line of
    defense on the upward-completeness invariant before any write happens.
    """
    try:
        plan, _key_frames, _edges, _directions, closure = _compute(
            sources=sources,
            relationships=relationships,
            seeds=seeds,
            policy=policy,
            job_seed=job_seed,
            engine_version=engine_version,
        )

        txn, output_paths = materialize_subset(
            sources=sources, survivors=closure.survivors, output_dir=Path(output_dir)
        )
        try:
            manifest = build_manifest(plan, engine_version)
            text = json.dumps(to_json_dict(manifest), sort_keys=True, indent=2)
            serial.write_text(txn, "subset-manifest.json", text)
            txn.publish()
        except BaseException:
            txn.abort()
            raise
        return SubsetResult(plan=plan, manifest=manifest, output_paths=output_paths)
    except BaseException:
        del job_seed
        raise


@entry_point
def select_subset(
    request: SubsetRequest,
    inputs: Mapping[str, pa.Table | LazySource],
    *,
    job_seed: bytes,
    file_access: FileAccess | None = None,
) -> SubsetSelection:
    """Pick the subset rows of resident or lazy tables. Reads key columns only and writes
    nothing. `file_access` governs path inputs; a lazy source reads under the policy it
    captured when it was built, and a resident table reads nothing."""
    try:
        plan, key_frames, edges, directions, closure = _compute(
            sources=inputs,
            relationships=request.relationships,
            seeds=request.seeds,
            policy=request.policy,
            job_seed=job_seed,
            engine_version=__version__,
        )
        scope = scope_for(request, key_frames)
        counts = {
            name: (
                frame.height,
                len(closure.survivors[name]) if name in scope.tables else frame.height,
            )
            for name, frame in key_frames.items()
        }
        return SubsetSelection(
            survivors={name: closure.survivors[name] for name in sorted(scope.tables)},
            counts=counts,
            scope=scope,
            excluded_edges=tuple(
                sorted(
                    {
                        *request.excluded_edges,
                        *(e.edge_id for e in edges if directions[e.edge_id] == "none"),
                    }
                )
            ),
            estimate=plan,
        )
    except BaseException:
        del job_seed
        raise


def subset_request_from_config(config: dict[str, Any]) -> SubsetRequest | None:
    """The typed request a validated `PipelineConfig` dump's `subset:` block describes, or
    `None` when it has no block."""
    subset_block = config.get("subset")
    if subset_block is None:
        return None
    # A generate table has no source to read, so an edge touching one cannot be subset.
    generated = {n for n, kind in classify_table_kinds(config).items() if kind == "generate"}
    relationships: list[PlanRelationship] = []
    excluded: list[str] = []
    for rel in relationships_from_config(config):
        # Drop per edge: a mask child of a mask parent keeps its edge even when a sibling
        # child is generated, or its rows would point at parents the subset removed.
        kept = (
            ()
            if rel.parent.table in generated
            else tuple(c for c in rel.children if c.table not in generated)
        )
        if len(kept) != len(rel.children):
            dropped = replace(rel, children=tuple(c for c in rel.children if c not in kept))
            excluded.extend(e.edge_id for e in build_subset_edges((dropped,)))
        if kept:
            relationships.append(replace(rel, children=kept))

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
    return SubsetRequest(
        relationships=tuple(relationships),
        seeds=tuple(seeds),
        policy=policy,
        excluded_edges=tuple(sorted(excluded)),
    )


def subset_inputs_from_config(
    config: dict[str, Any],
) -> tuple[
    dict[str, SubsetSource], tuple[PlanRelationship, ...], tuple[SeedSpec, ...], FanOutPolicy
]:
    """Adapt a validated `PipelineConfig` dump (with a `subset:` block) to `run_subset` kwargs."""
    if config.get("subset") is None:
        raise SubsetConfigError(
            code=reasons.config.SUBSET_CONFIG_MISSING,
            message="config has no `subset:` block; subset_inputs_from_config requires one",
        )

    sources: dict[str, SubsetSource] = {}
    for name, src in config.get("sources", {}).items():
        if src.get("type") != "file":
            raise SubsetConfigError(
                code=reasons.config.SUBSET_SOURCE_TYPE_UNSUPPORTED,
                message=f"table {name!r}: subsetting only supports local file sources "
                f"(got type={src.get('type')!r}); DB / cloud sources are deferred (GATE-1 #2)",
            )
        sources[name] = SubsetSource(path=src["path"], format=src["format"])

    # The block was checked above, so the request is never None. Relationships stay unfiltered:
    # the generate-table filter belongs to the run path, and standalone subsetting refuses an
    # edge that touches a generate table in preflight.
    request = cast(SubsetRequest, subset_request_from_config(config))
    return sources, relationships_from_config(config), request.seeds, request.policy
