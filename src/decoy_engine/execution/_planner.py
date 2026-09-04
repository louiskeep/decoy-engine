"""Execution-mode planner: classify a job; the chunked mode also routes.

`classify_job` classifies a validated job into exactly one execution mode
and records WHY every faster mode was rejected, following the EXPLAIN
surface of SQL planners (PostgreSQL `EXPLAIN`, Spark's query-plan
`explain()`): the decision is computed and reported without executing
the job, so operators can audit routing. Established-methodology
source: database EXPLAIN plans separate plan selection from plan
execution, and cost-based planners switch access paths on cardinality
(PostgreSQL's seq-scan-vs-index cost switch); the size threshold below
is that pattern applied to chunked streaming.

Modes, fastest first (the order defines "faster" for rejection
recording):

1. `polars_native`: every mask work node is a scalar polars-native
   strategy, no FK edges, and the polars substrate was requested. This
   mirrors `PolarsExecutionAdapter._is_fully_polars_native` WITHOUT
   executing: same work list (`build_work_list`), same native-strategy
   set (`POLARS_SCALAR_HANDLERS`), same FK-edge gate.
2. `chunked`: a single mask table whose every strategy passes
   `check_chunked_compatibility` (the value-keyed contract), no generate
   tables, no FK edges, resolved substrate pandas (the chunked route is
   pandas-only), only scalar work (composite bundles carry state the
   per-column gate cannot see), no fpe join-group (its QualityWarning
   cannot ride the chunked stream), no `when` predicates (frame-scoped
   evaluation), date_shift only with an explicit
   `provider_config.date_format` (format detection samples the whole
   column), and -- when the caller provides the loaded source tables --
   a source at or above the size threshold with chunk-stable dtypes and
   bucketize sources that are null-free numeric (bucketize output dtype
   is chunk-content-dependent otherwise). `run_pipeline(auto_chunk=True)`
   ROUTES this mode through `run_mask_pipeline_chunked` since the
   auto-chunk sprint; without loaded sources the runtime gates are
   skipped and the classification is admissibility-only.
3. `sequential_relationship` / `out_of_core_relationship`: relationship
   routes for FK jobs. The FK stack (`_sequential.py`, `out_of_core/`)
   now lives on this branch, but the LIVE relationship-route decision
   (sequential vs. out-of-core vs. full-frame) is owned by
   `run_pipeline`'s `_pipeline_routing.decide_execution_route`, NOT by
   this planner: that decision reads job size + out-of-core compatibility
   at dispatch time, and `run_pipeline` calls it as an early return
   BEFORE this classifier ever routes (SC2 auto-routing). This branch
   therefore only DETECTS that the job has FK edges (a relationship-route
   candidate) and points at the live router for the actual disposition
   (`RELATIONSHIP_ROUTE_DEFERRED`), so the EXPLAIN surface never claims a
   route the planner does not itself take.
4. `pandas_fallback`: the universal substrate; always admissible.

Determinism: same inputs -> same `ExecutionPlan`. The rejections mapping
is built in the fixed mode order above, every multi-part reason joins
sorted parts, and nothing is read from the environment or RNG beyond the
explicit `substrate` argument (`source_tables` contributes only row
counts, schema types, and null counts -- all pure metadata reads).

Routing seam: `PLANNER_ROUTING_ENABLED` remains the documented flag for
FULL planner-driven routing (all modes). It stays a hard `False`
constant: the auto-chunk sprint wired only the `chunked` mode into
routing, behind `run_pipeline`'s `auto_chunk` knob, and the relationship
routes (sequential / out-of-core) are routed by
`_pipeline_routing.decide_execution_route`, not by flipping this flag.
This classifier stays the static EXPLAIN + chunked-admissibility surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from decoy_engine.execution._pipeline_sources import lazy_source_rejection
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    import pyarrow as pa

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

# Routing seam for full planner-driven routing (all modes): flipping this
# (in a future sprint, behind its own acceptance gates) is what would let
# classify_job drive every route. Only the chunked mode routes today, via
# run_pipeline's auto_chunk knob; nothing reads this flag for routing.
PLANNER_ROUTING_ENABLED: bool = False

# Below this row count the full frame is small enough that per-chunk plan
# and adapter overhead buys nothing: the P0 job-level gates put a 100k-row
# 5-column scalar job at ~20 MiB traced peak full-frame, comfortable on any
# supported host, while the same job chunked showed ~10x lower peak at
# equal wall clock. 100k is where the memory win starts mattering.
AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT: int = 100_000

# The honest relationship-route disposition: this planner detects FK
# candidacy (edges exist) but does not itself route relationship jobs -- the
# live sequential-vs-out-of-core-vs-full-frame decision is made at dispatch
# time by run_pipeline's decide_execution_route (SC2 auto-routing), reading
# job size + out-of-core compatibility this static classifier deliberately
# does not evaluate. Pointing at the live router keeps the EXPLAIN surface
# from claiming a route the planner does not take.
RELATIONSHIP_ROUTE_DEFERRED: str = (
    "DEFERRED to the live router: job has FK relationship edges "
    "(relationship-route candidate); the sequential-vs-out-of-core-vs-full-frame "
    "decision is made by run_pipeline's decide_execution_route from job size + "
    "out-of-core compatibility, not by this planner "
    "(PLANNER_ROUTING_ENABLED stays False)."
)

# SC2 auto-routing thresholds (per LARGEST mask table's row count), consumed by
# _pipeline_routing.decide_execution_route. Defaults target the 32 GB
# deployment box (Cam's stated goal + the GCP n2-standard-8 bench box) and the
# documented full-frame FK memory model in docs/relationships-memory-scaling.md
# section 6: peak_RSS ~= 144 MB + 3.3 MB * (rows_per_table / 1000) for a
# 3-table width-16 hash chain, so full-frame FK OOMs near ~9M rows/table
# (~30 GB) on 32 GB. Both are conservative, schema-specific interim constants,
# plumbed as run_pipeline kwargs so the platform SC5 estimator can override
# them with box+schema-calibrated values (the memory-scaling doc is explicit
# that precise MB prediction is SC5's calibrated job, not a fixed constant's).
#
# OUT_OF_CORE: at/above this, an out-of-core-ELIGIBLE FK job routes to the
# bounded-RAM DuckDB route instead of sequential. 5M projects to ~16.6 GB
# full-frame (~half a 32 GB box) -- entering the risk zone -- and is well clear
# of BOTH measured out-of-core cost zones from section 6.2: sub-250k rows/table
# is the memory-overhead zone (out-of-core is +11% to +30% HEAVIER than
# full-frame there -- fixed per-run DuckDB overhead dominates at that scale),
# and 250k-1M rows/table is the wall-clock-tax zone (out-of-core is actually
# -1.9% to -11.4% LIGHTER on peak RSS there, but ~29% SLOWER wall-clock). 5M is
# past both zones, so the route is only chosen once a job is large enough that
# full-frame's memory risk outweighs the wall-clock tax.
OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT: int = 5_000_000

# REJECT: at/above this, a large relationship job that can ONLY run full-frame
# (not out-of-core-eligible, and not helped by the bounded sequential path) is
# rejected BEFORE the mask step rather than risking a silent OOM. 7.5M projects
# to ~24.9 GB full-frame (~78% of a 32 GB box) -- the hard-ceiling danger zone
# just below the documented ~9M/~30 GB cliff, with margin so a wider-than-16
# payload (which cliffs earlier) is caught before the OOM-killer. Set above
# OUT_OF_CORE_THRESHOLD so an out-of-core-eligible job at this size is routed
# to streaming, never rejected (GATE-1 #4: reroute OOC-eligible, reject only
# the hard ceiling).
FULL_FRAME_REJECT_ROWS_DEFAULT: int = 7_500_000

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
    source_tables: Mapping[str, pa.Table | LazySource] | None = None,
    auto_chunk_threshold_rows: int = AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT,
) -> ExecutionPlan:
    """Classify a job into exactly one execution mode, without executing.

    `config` is the validated `PipelineConfig` dump; `plan`, `registry`,
    and `relationship_graph` are the same objects `run_pipeline` builds
    before dispatch; `substrate` is the RESOLVED substrate string
    (`"pandas"` / `"polars"`), passed explicitly so the planner never
    reads the environment itself.

    `source_tables` are the caller-loaded Arrow frames (what
    `run_pipeline` receives as `sources`). When provided, the chunked
    classification additionally applies the RUNTIME gates: the source
    must exist, hold at least `auto_chunk_threshold_rows` rows, and
    carry only chunk-stable dtypes. When None (a static, pre-load
    classification) those gates are skipped and `chunked` means
    admissible-by-contract only.

    Pure and deterministic: every admissibility check is a static read
    of the compiled plan / config (the chunked gate reuses
    `check_chunked_compatibility`, the polars gate mirrors the polars
    adapter's native predicate over the same work list); `source_tables`
    contributes only Arrow metadata (row/null counts, schema types).
    """
    from decoy_engine.execution._pipeline import classify_table_kinds
    from decoy_engine.execution._runner import build_work_list, order_work

    table_kinds = classify_table_kinds(config)
    mask_tables = sorted(name for name, kind in table_kinds.items() if kind == "mask")
    generate_tables = sorted(name for name, kind in table_kinds.items() if kind == "generate")
    work = build_work_list(plan, registry)
    # Phase 4 slice 2: the group_key group_by effective-type gate is
    # work-order aware (Trap E), so the auto-route rejection needs the same
    # ordered list `check_chunked_compatibility`'s manual-entry gate derives
    # from the plan; computed once here rather than re-derived per table.
    ordered_work = order_work(work, relationship_graph)
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
        config,
        mask_tables=mask_tables,
        generate_tables=generate_tables,
        work=work,
        ordered_work=ordered_work,
        substrate=substrate,
        source_tables=source_tables,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
    )
    if chunked_rejection is None:
        reason = (
            f"single mask table {mask_tables[0]!r} with only chunk-safe "
            "(value-keyed) scalar strategies, no FK edges, and no generate "
            "tables, on the pandas substrate."
        )
        if source_tables is not None:
            rows = source_tables[mask_tables[0]].num_rows
            reason += (
                f" source holds {rows} rows, at or above the auto-chunk "
                f"threshold ({auto_chunk_threshold_rows}), with chunk-stable dtypes."
            )
        return ExecutionPlan(mode="chunked", rejections=rejections, reason=reason)
    rejections["chunked"] = chunked_rejection

    if has_fk:
        rejections["sequential_relationship"] = RELATIONSHIP_ROUTE_DEFERRED
        rejections["out_of_core_relationship"] = RELATIONSHIP_ROUTE_DEFERRED
        reason = (
            "job has FK relationship edges (relationship-route candidate); the "
            "live relationship route is chosen by run_pipeline's "
            "decide_execution_route (sequential / out-of-core / full-frame), "
            "not by this planner."
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
    work: list[Any],
    ordered_work: list[Any],
    substrate: str,
    source_tables: Mapping[str, pa.Table | LazySource] | None,
    auto_chunk_threshold_rows: int,
) -> str | None:
    """None when the job is admissible for chunked streaming; else why not.

    Reuses `check_chunked_compatibility` (the real compile-time gate) for
    the single-mask-table case so the planner and the chunked entrypoint
    cannot disagree on strategy admissibility. On top of it sit the gates
    the per-table check cannot see: job shape (generate tables elsewhere,
    more than one mask table), the pandas-substrate pin, composite bundle
    work, the fpe join-group warning, and -- when `source_tables` is
    provided -- the size threshold and chunk-stable-dtype runtime gates.
    All are fail-closed: any miss keeps the job on the full-frame path.
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
    if substrate != "pandas":
        reasons.append(
            f"resolved substrate is {substrate!r}; the chunked route constructs "
            "the pandas adapter, so routing would silently change the job's "
            "executed substrate"
        )
    # The relationships gate is checked here (not only via the per-table
    # call below) so multi-table FK jobs still surface it: the per-table
    # gate only runs for the single-mask-table shape.
    # Future hardening: accept has_fk as an explicit parameter so direct
    # callers (not run_pipeline) can pass a graph that's known to be consistent
    # with config["relationships"], defense-in-depth against inconsistent inputs.
    if config.get("relationships"):
        reasons.append(
            "chunked_relationships_unsupported: configs with FK relationships "
            "cannot run chunked (resolving a child key reads the whole parent frame)"
        )
    elif len(mask_tables) == 1:
        table = mask_tables[0]
        try:
            check_chunked_compatibility(config, table=table)
        except PlanCompileError as exc:
            reasons.append(f"{exc.code}: {exc.message}")
        # Composite bundles are recognized by provider capability, not
        # strategy name, so the per-column compat gate cannot see them;
        # their coherent-group state is whole-bundle, not value-keyed.
        non_scalar = sorted(
            {str(node.kind) for node in work if node.table == table and node.kind != "scalar"}
        )
        if non_scalar:
            reasons.append(
                f"non-scalar (composite bundle) work on table {table!r}: "
                f"{', '.join(non_scalar)}; bundle state is not value-keyed"
            )
        join_group_cols = _fpe_join_group_columns(config, table=table)
        if join_group_cols:
            reasons.append(
                f"fpe_join_group on column(s) {', '.join(join_group_cols)}: the "
                "full-frame run emits the fpe_join_group_active QualityWarning, "
                "which the chunked entrypoint cannot carry"
            )
        reasons.extend(_whole_column_state_rejections(config, table=table))
        if source_tables is not None:
            reasons.extend(
                _runtime_source_rejections(
                    source_tables,
                    table=table,
                    auto_chunk_threshold_rows=auto_chunk_threshold_rows,
                    bucketize_columns=_bucketize_columns(config, table=table),
                    ordered_work=ordered_work,
                )
            )
    return "; ".join(reasons) if reasons else None


def _table_column_entries(config: dict[str, Any], *, table: str) -> list[dict[str, Any]]:
    """The dict column entries of `table` in the raw config, else []."""
    tables = config.get("tables") or []
    table_cfg = next((t for t in tables if isinstance(t, dict) and t.get("name") == table), None)
    if table_cfg is None:
        return []
    return [c for c in table_cfg.get("columns") or [] if isinstance(c, dict)]


def _whole_column_state_rejections(config: dict[str, Any], *, table: str) -> list[str]:
    """Config-level gates for strategies whose output can depend on state
    the whole column carries but a single chunk does not.

    `check_chunked_compatibility` admits these strategies for the explicit
    chunked entrypoint, but auto-routing must be byte-identity-safe without
    operator judgment, so it additionally requires the whole-column input
    to be pinned in config:

    - date_shift: without an explicit `provider_config.date_format` the
      strategy DETECTS the format from whole-column samples; disjoint
      per-chunk samples can lock different formats and reformat the same
      value differently.
    - `when`: predicates evaluate against the frame they are handed, so a
      per-chunk frame is a different evaluation scope than the whole
      frame (schema-validated configs cannot carry `when` today, but
      run_pipeline does not re-validate its dict input and `when` is a
      shipped ColumnSeed field, so the gate must see it).
    """
    reasons: list[str] = []
    when_cols: list[str] = []
    undated_cols: list[str] = []
    for col_entry in _table_column_entries(config, table=table):
        name = str(col_entry.get("name", "?"))
        if col_entry.get("when"):
            when_cols.append(name)
        if col_entry.get("strategy") == "date_shift" and not (
            col_entry.get("provider_config") or {}
        ).get("date_format"):
            undated_cols.append(name)
    if when_cols:
        reasons.append(
            f"when_predicate_not_chunk_stable: column(s) {', '.join(sorted(when_cols))} "
            "carry a `when` predicate, which is evaluated per frame; per-chunk "
            "evaluation is a different scope than the whole frame"
        )
    if undated_cols:
        reasons.append(
            f"date_shift_requires_explicit_format: column(s) {', '.join(sorted(undated_cols))} "
            "use date_shift without provider_config.date_format; format detection "
            "samples the whole column, so per-chunk detection can lock a different "
            "format"
        )
    return reasons


def _bucketize_columns(config: dict[str, Any], *, table: str) -> list[str]:
    return sorted(
        str(c.get("name", "?"))
        for c in _table_column_entries(config, table=table)
        if c.get("strategy") == "bucketize"
    )


def _fpe_join_group_columns(config: dict[str, Any], *, table: str) -> list[str]:
    """Columns on `table` whose fpe config sets `fpe_join_group`."""
    tables = config.get("tables") or []
    table_cfg = next((t for t in tables if isinstance(t, dict) and t.get("name") == table), None)
    if table_cfg is None:
        return []
    out: list[str] = []
    for col_entry in table_cfg.get("columns") or []:
        if not isinstance(col_entry, dict) or col_entry.get("strategy") != "fpe":
            continue
        if (col_entry.get("provider_config") or {}).get("fpe_join_group"):
            out.append(str(col_entry.get("name", "?")))
    return sorted(out)


def _runtime_source_rejections(
    source_tables: Mapping[str, pa.Table | LazySource],
    *,
    table: str,
    auto_chunk_threshold_rows: int,
    bucketize_columns: list[str] | None = None,
    ordered_work: list[Any] | None = None,
) -> list[str]:
    """Runtime (loaded-source) gates: presence, size threshold, dtypes,
    and the bucketize source shape.

    All metadata reads: `num_rows` and `null_count` come from Arrow array
    metadata and the schema walk touches no values, so this is O(columns),
    not O(rows).
    """
    import pyarrow.types as pat

    reasons: list[str] = []
    extra = sorted(set(source_tables) - {table})
    if extra:
        # The full-frame adapter echoes every loaded source frame in its
        # outputs; the chunked route yields only the mask table, so extra
        # frames would silently vanish from the result.
        reasons.append(
            f"extra loaded source frame(s) {', '.join(extra)}: the full-frame "
            "adapter echoes them in outputs, the chunked route would drop them"
        )
    src = source_tables.get(table)
    if src is None:
        reasons.append(f"no loaded source frame for table {table!r}")
        return reasons
    # TB-1 defensive guard, unreachable in production today (see
    # `_pipeline_sources.lazy_source_rejection`'s docstring): only
    # relationship jobs carry a LazySource, and those are already rejected
    # upstream (line 367) before reaching this per-table check. The
    # `isinstance` check (rather than calling the helper unconditionally)
    # is what lets the type checker narrow `src` to `pa.Table` below.
    if isinstance(src, LazySource):
        reasons.append(lazy_source_rejection(src, table=table) or "")
        return reasons
    if src.num_rows < auto_chunk_threshold_rows:
        reasons.append(
            f"source holds {src.num_rows} rows, below the auto-chunk "
            f"threshold ({auto_chunk_threshold_rows}); full-frame is cheaper "
            "than streaming at this size"
        )
    unstable: list[str] = []
    for schema_field in src.schema:
        t = schema_field.type
        if pat.is_integer(t):
            # pandas widens int+null to float PER FRAME, so a null-free
            # chunk keeps int64 while a null-bearing one becomes float64;
            # only a null-free integer column round-trips chunk-stably.
            if src.column(schema_field.name).null_count > 0:
                unstable.append(f"{schema_field.name} (integer with nulls)")
        elif not (
            pat.is_string(t)
            or pat.is_large_string(t)
            or pat.is_floating(t)
            or pat.is_boolean(t)
            or pat.is_temporal(t)
            or pat.is_null(t)
        ):
            unstable.append(f"{schema_field.name} ({t})")
    if unstable:
        reasons.append(
            "column(s) with non-chunk-stable pandas round-trip dtypes: "
            f"{', '.join(sorted(unstable))}"
        )
    # bucketize output dtype depends on chunk content: null / non-numeric
    # positions fall through to the ORIGINAL value, so a null-bearing or
    # non-numeric chunk infers a different Arrow type than a clean one.
    # Only a null-free numeric source makes every chunk (and the full
    # frame) format to the same all-string column.
    bad_bucketize: list[str] = []
    for name in bucketize_columns or []:
        if name not in src.schema.names:
            continue  # unknown columns are the compile stage's problem
        t = src.schema.field(name).type
        if not (pat.is_integer(t) or pat.is_floating(t)):
            bad_bucketize.append(f"{name} ({t} is not numeric)")
        elif src.column(name).null_count > 0:
            bad_bucketize.append(f"{name} (numeric with nulls)")
    if bad_bucketize:
        reasons.append(
            "bucketize_source_not_null_free_numeric: column(s) "
            f"{', '.join(bad_bucketize)}; bucketize masks only clean numeric "
            "values and falls through elsewhere, so its output dtype is "
            "chunk-content-dependent unless the source column is null-free "
            "numeric"
        )
    # Trap E: a group_key group_by column whose effective type is not
    # provably safe (see _chunked_group_key.py). Same reason-collector the
    # manual entry's raising gate uses, so the two routes cannot disagree.
    from decoy_engine.execution._chunked_group_key import unsafe_group_key_group_by_columns

    offending_group_key = unsafe_group_key_group_by_columns(
        ordered_work or [], src.schema, table=table
    )
    if offending_group_key:
        reasons.append(
            "chunked_group_key_group_by_dtype_unsupported: group_key column(s) "
            f"{', '.join(offending_group_key)} read a group_by value whose "
            "effective type is not provably safe for chunked self-masking"
        )
    # text_mask requires a chunk-stable string source (a non-string int+null
    # source widens by chunk boundary under str()-conversion). Same collector
    # the manual entry's raising gate uses.
    from decoy_engine.execution._chunked_text_mask import unsafe_text_mask_source_columns

    offending_text_mask = unsafe_text_mask_source_columns(
        ordered_work or [], src.schema, table=table
    )
    if offending_text_mask:
        reasons.append(
            "chunked_text_mask_source_dtype_unsupported: text_mask column(s) "
            f"{', '.join(offending_text_mask)} apply to a non-string source, which "
            "diverges by chunk boundary under the handler's str()-conversion"
        )
    # code_set has the identical chunk-stable-string-source requirement (same
    # str()-conversion hazard). Same collector the manual entry's raising gate uses.
    from decoy_engine.execution._chunked_code_set import unsafe_code_set_source_columns

    offending_code_set = unsafe_code_set_source_columns(ordered_work or [], src.schema, table=table)
    if offending_code_set:
        reasons.append(
            "chunked_code_set_source_dtype_unsupported: code_set column(s) "
            f"{', '.join(offending_code_set)} apply to a non-string source, which "
            "diverges by chunk boundary under the handler's str()-conversion"
        )
    # bucket_perturb has the identical chunk-stable-string-source requirement
    # (a non-string source parses to a chunk-boundary-dependent date string).
    # Same collector the manual entry's raising gate uses.
    from decoy_engine.execution._chunked_bucket_perturb import unsafe_bucket_perturb_source_columns

    offending_bucket_perturb = unsafe_bucket_perturb_source_columns(
        ordered_work or [], src.schema, table=table
    )
    if offending_bucket_perturb:
        reasons.append(
            "chunked_bucket_perturb_source_dtype_unsupported: bucket_perturb "
            f"column(s) {', '.join(offending_bucket_perturb)} apply to a "
            "non-string source, which diverges by chunk boundary"
        )
    return reasons


__all__ = [
    "AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT",
    "EXECUTION_MODES",
    "FULL_FRAME_REJECT_ROWS_DEFAULT",
    "OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT",
    "PLANNER_ROUTING_ENABLED",
    "RELATIONSHIP_ROUTE_DEFERRED",
    "ExecutionPlan",
    "classify_job",
]
