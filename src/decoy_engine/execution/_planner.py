"""Observe-only execution-mode planner: classify a job, never route it.

`classify_job` classifies a validated job into exactly one execution mode
and records WHY every faster mode was rejected, following the EXPLAIN
surface of SQL planners (PostgreSQL `EXPLAIN`, Spark's query-plan
`explain()`): the decision is computed and reported from static inputs
without executing the job, so operators can audit routing before any
route ever changes. Established-methodology source: database EXPLAIN
plans separate plan selection from plan execution; this module ships the
selection surface first, execution routing stays exactly where it is.

Modes, fastest first (the order defines "faster" for rejection
recording):

1. `polars_native`: every mask work node is a scalar polars-native
   strategy, no FK edges, and the polars substrate was requested. This
   mirrors `PolarsExecutionAdapter._is_fully_polars_native` WITHOUT
   executing: same work list (`build_work_list`), same native-strategy
   set (`POLARS_SCALAR_HANDLERS`), same FK-edge gate.
2. `chunked`: a single mask table whose every strategy passes
   `check_chunked_compatibility` (the value-keyed contract), no generate
   tables, no FK edges. Size thresholds are a routing concern and land
   with routing, not here.
3. `sequential_relationship` / `out_of_core_relationship`: relationship
   routes for FK jobs. The FK stack (`_sequential.py`, `out_of_core/`)
   lives on its own branches, so this branch can only detect that the
   job HAS FK edges (a relationship-route candidate); the
   sequential-vs-out-of-core decision is honestly DEFERRED rather than
   pretended (`RELATIONSHIP_ROUTE_DEFERRED`). No off-branch imports.
4. `pandas_fallback`: the universal substrate; always admissible.

Determinism: same inputs -> same `ExecutionPlan`. The rejections mapping
is built in the fixed mode order above, every multi-part reason joins
sorted parts, and nothing is read from the environment or RNG beyond the
explicit `substrate` argument.

Routing seam (P3+): `PLANNER_ROUTING_ENABLED` is the documented flag a
future sprint flips to let the plan drive routing. It is a hard `False`
constant here and NOTHING reads it for routing yet; `run_pipeline` only
ever stamps the classification under
`quality_metrics["execution_plan"]` when asked (`explain_plan=True`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

# Fastest-first mode order; rejection recording and "faster than" both
# derive from this tuple so they cannot disagree.
EXECUTION_MODES: tuple[str, ...] = (
    "polars_native",
    "chunked",
    "sequential_relationship",
    "out_of_core_relationship",
    "pandas_fallback",
)

# Routing seam for P3+: flipping this (in a future sprint, behind its own
# acceptance gates) is what would let classify_job drive routing. Nothing
# reads it for routing today; it exists so the flag has one documented home.
PLANNER_ROUTING_ENABLED: bool = False

# The honest relationship-route disposition on this branch: the FK stack
# (sequential + out-of-core executors) lives on its own branches, so the
# planner can detect candidacy (FK edges exist) but must not pretend it
# evaluated route compatibility it cannot see.
RELATIONSHIP_ROUTE_DEFERRED: str = (
    "DEFERRED: job has FK relationship edges (relationship-route candidate); "
    "sequential-vs-out-of-core compatibility is evaluated when the FK stack "
    "is present, not on this branch."
)

_NO_RELATIONSHIP_ROUTE: str = "no FK relationship edges; relationship routes do not apply."


@dataclass(frozen=True)
class ExecutionPlan:
    """The planner's decision: one chosen mode, plus why faster modes lost.

    `rejections` maps each mode FASTER than `mode` (per EXECUTION_MODES
    order) to its rejection reason; `reason` explains the chosen mode.
    Frozen and read-only so a stamped plan cannot drift after the fact.
    """

    mode: str
    rejections: Mapping[str, str] = field(default_factory=dict)
    reason: str = ""

    def __post_init__(self) -> None:
        if self.mode not in EXECUTION_MODES:
            raise ValueError(f"mode must be one of {EXECUTION_MODES}; got {self.mode!r}.")
        # Re-wrap so callers holding the original dict cannot mutate the plan.
        object.__setattr__(self, "rejections", MappingProxyType(dict(self.rejections)))


def classify_job(
    config: dict[str, Any],
    *,
    plan: Plan,
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
    substrate: str,
) -> ExecutionPlan:
    """Classify a job into exactly one execution mode, without executing.

    `config` is the validated `PipelineConfig` dump; `plan`, `registry`,
    and `relationship_graph` are the same objects `run_pipeline` builds
    before dispatch; `substrate` is the RESOLVED substrate string
    (`"pandas"` / `"polars"`), passed explicitly so the planner never
    reads the environment itself.

    Pure and deterministic: every admissibility check is a static read
    of the compiled plan / config (the chunked gate reuses
    `check_chunked_compatibility`, the polars gate mirrors the polars
    adapter's native predicate over the same work list).
    """
    from decoy_engine.execution._pipeline import classify_table_kinds
    from decoy_engine.execution._runner import build_work_list

    table_kinds = classify_table_kinds(config)
    mask_tables = sorted(name for name, kind in table_kinds.items() if kind == "mask")
    generate_tables = sorted(name for name, kind in table_kinds.items() if kind == "generate")
    work = build_work_list(plan, registry)
    has_fk = bool(relationship_graph.edges)

    rejections: dict[str, str] = {}

    polars_rejection = _polars_native_rejection(
        substrate=substrate, mask_tables=mask_tables, work=work, has_fk=has_fk
    )
    if polars_rejection is None:
        reason = (
            "all mask work is scalar and polars-native with no FK edges on the polars substrate."
        )
        if generate_tables:
            reason += (
                f" generate-kind table(s) {', '.join(generate_tables)} run the"
                " synthesize path regardless of mode."
            )
        return ExecutionPlan(mode="polars_native", rejections={}, reason=reason)
    rejections["polars_native"] = polars_rejection

    chunked_rejection = _chunked_rejection(
        config, mask_tables=mask_tables, generate_tables=generate_tables
    )
    if chunked_rejection is None:
        return ExecutionPlan(
            mode="chunked",
            rejections=rejections,
            reason=(
                f"single mask table {mask_tables[0]!r} with only chunk-safe "
                "(value-keyed) strategies, no FK edges, and no generate tables."
            ),
        )
    rejections["chunked"] = chunked_rejection

    if has_fk:
        rejections["sequential_relationship"] = RELATIONSHIP_ROUTE_DEFERRED
        rejections["out_of_core_relationship"] = RELATIONSHIP_ROUTE_DEFERRED
        reason = (
            "job has FK relationship edges (relationship-route candidate); "
            "FK resolution runs on the pandas substrate on this branch."
        )
    else:
        rejections["sequential_relationship"] = _NO_RELATIONSHIP_ROUTE
        rejections["out_of_core_relationship"] = _NO_RELATIONSHIP_ROUTE
        reason = "no faster execution mode admitted this job; pandas is the universal fallback."
    return ExecutionPlan(mode="pandas_fallback", rejections=rejections, reason=reason)


def _polars_native_rejection(
    *,
    substrate: str,
    mask_tables: list[str],
    work: list[Any],
    has_fk: bool,
) -> str | None:
    """None when the job would take the pure-polars loop; else why not.

    Mirrors `PolarsExecutionAdapter._is_fully_polars_native` (edges gate +
    scalar-native work check) plus the two planner-level gates that
    predicate cannot see: the requested substrate (an operator pin the
    planner must not override) and the presence of any mask work at all
    (a pure-generate job never enters the mask adapter, so calling it
    polars-native would be vacuous).
    """
    from decoy_engine.execution.polars._strategies import POLARS_SCALAR_HANDLERS

    native = frozenset(POLARS_SCALAR_HANDLERS)
    reasons: list[str] = []
    if not mask_tables:
        reasons.append(
            "no mask-kind work; the polars-native loop masks existing data "
            "(generation uses the synthesize path)"
        )
    if substrate != "polars":
        reasons.append(
            f"resolved substrate is {substrate!r}; the polars-native loop "
            "requires the polars substrate"
        )
    if has_fk:
        reasons.append("fk_resolution: FK edges route through the pandas oracle")
    non_native = sorted(
        {
            node.strategy if node.kind == "scalar" else node.kind
            for node in work
            if not (node.kind == "scalar" and node.strategy in native)
        }
    )
    if non_native:
        reasons.append(f"non-polars-native work: {', '.join(non_native)}")
    return "; ".join(reasons) if reasons else None


def _chunked_rejection(
    config: dict[str, Any],
    *,
    mask_tables: list[str],
    generate_tables: list[str],
) -> str | None:
    """None when the job is admissible for chunked streaming; else why not.

    Reuses `check_chunked_compatibility` (the real compile-time gate) for
    the single-mask-table case so the planner and the chunked entrypoint
    cannot disagree on strategy admissibility; the job-shape gates that
    the per-table check does not cover (generate tables elsewhere in the
    job, more than one mask table) are evaluated here.
    """
    from decoy_engine.execution._chunked import check_chunked_compatibility
    from decoy_engine.plan._errors import PlanCompileError

    reasons: list[str] = []
    if not mask_tables:
        reasons.append("no mask-kind tables to stream")
    if generate_tables:
        reasons.append(
            f"generate-kind table(s) {', '.join(generate_tables)} present; "
            "chunked execution masks existing data and has no generation mode"
        )
    if len(mask_tables) > 1:
        reasons.append(
            f"chunked execution masks one table per run; job declares "
            f"{len(mask_tables)} mask tables ({', '.join(mask_tables)})"
        )
    # The relationships gate is checked here (not only via the per-table
    # call below) so multi-table FK jobs still surface it: the per-table
    # gate only runs for the single-mask-table shape.
    if config.get("relationships"):
        reasons.append(
            "chunked_relationships_unsupported: configs with FK relationships "
            "cannot run chunked (resolving a child key reads the whole parent frame)"
        )
    elif len(mask_tables) == 1:
        try:
            check_chunked_compatibility(config, table=mask_tables[0])
        except PlanCompileError as exc:
            reasons.append(f"{exc.code}: {exc.message}")
    return "; ".join(reasons) if reasons else None


__all__ = [
    "EXECUTION_MODES",
    "PLANNER_ROUTING_ENABLED",
    "RELATIONSHIP_ROUTE_DEFERRED",
    "ExecutionPlan",
    "classify_job",
]
