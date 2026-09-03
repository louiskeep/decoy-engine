"""`run_pipeline`'s execution-route DECISIONS, split out to hold the
600-LOC orchestration cap (CLAUDE.md section "Engineering best practices").

This module decides which route a job takes; it never executes one --
`_pipeline_route_exec` owns dispatching to the underlying runner
(`run_sequential`, `run_fk_out_of_core`, `run_mask_pipeline_chunked`) and
packaging the result into an `ExecutionResult`. `run_pipeline` calls both
modules directly in the fixed order this docstring describes.

Two independent routing layers compose here, in a fixed decision order
`run_pipeline` enforces by calling them in sequence:

1. S2 relationship routing + SC2 out-of-core auto-routing:
   `decide_execution_route` is the SINGLE live router for
   relationship-bearing (FK) jobs -- it runs first and early-returns
   before the layer-2 chunk classifier is ever consulted for routing.
   For a relationship-bearing PURE-MASK job (no generate tables, no
   validators, no fidelity_report, no vault_writer -- see
   `_sequential_eligible`) under `execution_mode="auto"` it picks, in
   priority order (SC2):

     a. **out_of_core** -- the bounded-RAM DuckDB route -- when the job is
        out-of-core-compatible (`check_out_of_core_compatibility` admits
        the plan: an accepted strategy set, an acyclic table-level FK
        graph, no `when` predicate, single-parent-per-child) AND the
        largest mask table is at/above `out_of_core_threshold_rows`.
        `_pipeline_route_exec.run_out_of_core_route` executes + packages it.
        Inside that route, a sink-path table auto-selects the reorder driver
        over `_batch_join` once its largest incoming edge's deduped
        parent-key count reaches `out_of_core_reorder_threshold_rows`
        (default 2M; `0` forces every eligible table, per-run override) --
        an independent, narrower-scoped threshold from this layer's own
        `out_of_core_threshold_rows` (see `_route_policy.decide_route`).
     b. **sequential** -- the bounded-memory (but O(cardinality), so still
        OOM-able at extreme scale) `run_sequential` path -- for a
        pure-mask FK job that is NOT out-of-core-eligible (an unsupported
        strategy, a self-referential / cyclic FK, a multi-parent child)
        or is below the out-of-core size threshold.
        `_pipeline_route_exec.run_sequential_route` executes + packages it.
     c. **full_frame** -- the adapter-selected `run` path -- for a job with
        no relationships, and for a relationship job disqualified from
        sequential (generate+mask, validators, fidelity, vault,
        non-pandas substrate) that is small enough to be full-frame-safe.

   And it REJECTS before the read/mask step (fail-closed
   `ExecutionError` code `fk_full_frame_oom_risk_rejected`) when a
   relationship job can ONLY run full-frame (neither out-of-core- nor
   sequential-eligible) AND the largest mask table is at/above
   `full_frame_reject_rows` -- the "never a silent OOM" half of GATE-1's
   prevention posture: a too-big job that no bounded route can take is
   diverted to a clean typed error, not left to the OOM-killer.

   `execution_mode="full_frame"` always forces full-frame (even when
   eligible, even when large -- an explicit operator escape hatch that
   bypasses the reject: the operator owns the OOM risk they asked for);
   `execution_mode="sequential"` forces sequential and
   `execution_mode="out_of_core"` forces out-of-core, each raising
   `ConfigError` if the job is not eligible for the forced route
   (fail-closed: never silently ignore an explicit request). The
   sequential path is pandas-only by construction (`run_sequential` is
   typed to `PandasExecutionAdapter`), so it always constructs its own
   adapter regardless of the caller's `substrate` knob; the out-of-core
   route is likewise pandas-oracle-parity by construction.
2. S3 (engine-efficiencies P3) auto-chunk routing: reached only when (1)
   did NOT take the sequential early return -- every path into
   `decide_chunk_route` is either explicitly forced full_frame or was
   found sequential-ineligible, relationship-bearing jobs in particular.
   `classify_job`'s own `chunked` gate independently excludes any job
   with FK edges or a `relationships` block (`_planner._chunked_rejection`),
   so a relationship-bearing job that reaches this layer (e.g.
   generate+mask FK, or an FK job disqualified from sequential by
   validators) cannot be misrouted into the chunked path either -- the
   two layers compose without overlap: (1) owns relationship routing
   (sequential vs. full_frame); (2) owns single-table non-relationship
   routing (chunked vs. full_frame). When the job classifies `chunked`,
   `_pipeline_route_exec.run_mask_chunked` streams it through
   `run_mask_pipeline_chunked` in `chunk_size_rows`-row slices instead of
   one full-frame adapter call; this is a peak-memory win only, never a
   semantic change, and every eligibility miss fails CLOSED to the exact
   full-frame path.

Row-error honesty (D7/D8, S1): the chunked route is never eligible for
row-error quarantine -- `run_mask_pipeline_chunked`'s H1 fail-closed
check (`_chunked.py`) raises `RowErrorsFailedError` the moment ANY chunk
reports a row error, so `run_pipeline` never sees row errors to
quarantine on that route (same policy the pre-existing manual chunked
entrypoint already enforced).

Sprint B1b (OOM-avoidance routing redesign, docs/plans/2026-07-10-oom-
avoidance-routing-redesign.md §13): `decide_execution_route`'s
`use_byte_estimate_routing` flag (TB-5 default ON) wires the B1a byte-level
estimator (`_mem_estimate.fits`) into the "auto" full_frame-admission
decision, additively -- see the flag's docstring on `decide_execution_route`
for the exact scope and rule. Forced OFF (the rollback path), this module's
routing is BYTE-FOR-BYTE the pre-B1b row-count logic described above.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from decoy_engine.errors import ConfigError
from decoy_engine.execution._errors import ExecutionError

# The size + out-of-core-admission signal helpers, and the S3 auto-chunk
# routing layer, live in sibling modules to hold the 600-LOC orchestration
# cap; re-exported here so `run_pipeline` keeps a single
# `_pipeline_routing.<name>` call surface for the routing seam.
from decoy_engine.execution._pipeline_chunk_route import auto_chunk_stamp, decide_chunk_route
from decoy_engine.execution._pipeline_routing_signals import (
    byte_estimate_full_frame_fits,
    largest_mask_table_rows,
    largest_mask_table_rows_from_profile,
    out_of_core_admission,
    out_of_core_routing_signals,
    resolve_execution_route,
    resolve_full_frame_fits_estimate,
    resolve_probe_recovery,
)
from decoy_engine.execution._planner import (
    FULL_FRAME_REJECT_ROWS_DEFAULT,
    OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
)

if TYPE_CHECKING:
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "auto_chunk_stamp",
    "byte_estimate_full_frame_fits",
    "decide_chunk_route",
    "decide_execution_route",
    "largest_mask_table_rows",
    "largest_mask_table_rows_from_profile",
    "out_of_core_admission",
    "out_of_core_routing_signals",
    "resolve_execution_route",
    "resolve_full_frame_fits_estimate",
    "resolve_probe_recovery",
]


def _has_cross_table_fk_cycle(graph: RelationshipGraph) -> bool:
    """True if the table-level FK graph has a cycle across DISTINCT tables.

    Sequential masking orders whole tables (table_topo_order), so a
    cross-table cycle cannot be sequenced; self-edges (self-ref FK) mask
    within one table and are not a table-level cycle (S2 remediation guide
    r3 section 6).
    """
    from collections import defaultdict

    succ: dict[str, set[str]] = defaultdict(set)
    for edge in graph.edges:
        if edge.parent_table != edge.child_table:
            succ[edge.parent_table].add(edge.child_table)
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}

    def visit(node: str) -> bool:
        color[node] = GRAY
        for nxt in succ.get(node, ()):  # DFS back-edge = cycle
            c = color.get(nxt, WHITE)
            if c == GRAY or (c == WHITE and visit(nxt)):
                return True
        color[node] = BLACK
        return False

    return any(color.get(n, WHITE) == WHITE and visit(n) for n in list(succ))


def _sequential_eligible(
    profile: Any,
    *,
    has_generate_table: bool,
    validators: list[Any],
    fidelity_report: bool,
    vault_writer: Any,
    resolved_substrate: str = "pandas",
) -> tuple[bool, str]:
    """Decide whether a mask job may take the bounded-memory sequential path.

    Returns (eligible, reason). `reason` is a stable telemetry token; when
    eligible it is "pure_mask_fk", otherwise it names the disqualifier.

    The sequential path streams/evicts table by table, so any run_pipeline
    post-mask step that needs every masked output resident at once
    disqualifies it: job-level validators (compare positionally against all
    sources), the fidelity report, and the token-vault collection. Pure-generate
    and mixed generate+mask jobs are disqualified because generate tables are
    not masked table-by-table through this path.

    `resolved_substrate` (S3 reconciliation, P1 x S2): `run_sequential` is
    pandas-only by construction. An explicit non-pandas `substrate` request
    (or `DECOY_SUBSTRATE=polars` resolving through `substrate=None`) must
    disqualify sequential eligibility rather than silently ignoring the
    request -- the full_frame branch's `select_execution_adapter` is what
    actually honors `substrate="polars"` (native polars execution, or its
    own explicit `fallback_to_pandas` contract, including
    `code='polars_substrate_strategy_unmigrated'` when fallback is
    disabled). Defaults to `"pandas"` so callers that never pass a
    substrate keep today's routing (the common case: a pure-mask FK job is
    bounded-memory by default) and existing unit tests that construct this
    predicate directly without a substrate argument are unaffected.
    """
    if not profile.relationships:
        return False, "no_relationships"
    if has_generate_table:
        return False, "generate_plus_mask"
    if validators:
        return False, "validators_present"
    if fidelity_report:
        return False, "fidelity_report_requested"
    if vault_writer is not None:
        return False, "vault_writer_requested"
    if resolved_substrate != "pandas":
        return False, "non_pandas_substrate_requested"
    return True, "pure_mask_fk"


def decide_execution_route(
    profile: Any,
    *,
    has_generate_table: bool,
    has_mask_table: bool,
    validators: list[Any],
    fidelity_report: bool,
    vault_writer: Any,
    execution_mode: str,
    graph: RelationshipGraph,
    resolved_substrate: str = "pandas",
    out_of_core_compatible: bool = False,
    out_of_core_reject_code: str | None = None,
    largest_table_rows: int | None = None,
    largest_table_rows_exact: bool = True,
    out_of_core_threshold_rows: int = OUT_OF_CORE_THRESHOLD_ROWS_DEFAULT,
    full_frame_reject_rows: int = FULL_FRAME_REJECT_ROWS_DEFAULT,
    use_byte_estimate_routing: bool = True,
    full_frame_fits_estimate: bool | None = None,
    use_probe_routing: bool = True,
    probe_recovers_full_frame: bool | None = None,
) -> tuple[str, str]:
    """Decide `(route, route_reason)` -- `"out_of_core"`, `"sequential"`, or
    `"full_frame"` -- or RAISE a fail-closed reject-before-read.

    `use_byte_estimate_routing` (Sprint B1b; TB-5 flipped the default to
    `True`, additive + forceable OFF for rollback): OFF, this function is
    BYTE-FOR-BYTE the row-count logic below -- `full_frame_fits_estimate` is
    never read, the rollback path. ON (the default), for a job IN SCOPE
    (`has_relationships and has_mask_table and not has_generate_table` --
    see `byte_estimate_full_frame_fits`'s docstring for why generate+mask is
    excluded), the "auto" decision is REPLACED by the §13 conservative-filter
    ruling: full_frame only when `full_frame_fits_estimate is True` (the
    caller precomputes this via `byte_estimate_full_frame_fits` +
    `resolve_budget`); otherwise -- UNPRICEABLE (`None`) treated identically
    to `False`, per §13 -- the job routes to whichever BOUNDED path
    eligibility admits (out_of_core when compat-admitted, else sequential),
    or the existing reject when NEITHER applies (§13's one irreducible reject
    class). The choice AMONG bounded routes stays eligibility-based, never an
    estimate of out_of_core/sequential peak (unmeasured placeholders, §13);
    `out_of_core_threshold_rows` is not consulted in this mode. Out of scope,
    the flag has NO effect, even when `full_frame_fits_estimate` is set.

    Sprint B2 (§3.3/§11/§13): `use_probe_routing` (TB-5 default `True`,
    additive, composes with `use_byte_estimate_routing` -- it has NO effect
    unless that flag is also `True`) is the fast-path RECOVERY for a job the
    conservative
    B1b estimate over-downgrades. `probe_recovers_full_frame` is the caller's
    precomputed `_pipeline_routing_signals.resolve_probe_recovery` result:
    `True` only when the two-point micro-probe (`_probe.probe_peak_bytes`)
    actually MEASURED this job's real full_frame peak (at small scale,
    extrapolated) and confirmed it clears the budget with margin. Checked
    ONLY after `full_frame_fits_estimate is True` fails to admit the job (a
    confirmed static fit never needs a probe) -- when it is `True`, this
    function returns `full_frame` even for a job that would otherwise be
    out_of_core-eligible: a MEASURED fit beats a bounded-by-eligibility
    default, because full_frame is faster and the probe's `True` means the
    measurement, not a model, backs the decision. Anything other than `True`
    (`False` or `None` -- an inconclusive probe, or one that was never run
    because it was out of scope/unnecessary) falls straight through to the
    exact same bounded-by-eligibility logic below as if the probe did not
    exist -- an inconclusive probe NEVER yields full_frame, only a
    CONFIRMED fit does (the load-bearing safety property: recovery only on
    a measured fit).

    Priority (SC2), for a relationship-bearing PURE-MASK job under `auto`:
    out-of-core (when compat-admitted AND large) > sequential (bounded but
    O(cardinality)) > full-frame. A large relationship job that no bounded
    route can take is REJECTED before read (`ExecutionError`
    `fk_full_frame_oom_risk_rejected`) rather than left to OOM full-frame.

    Out-of-core eligibility is a STRICT SUBSET of sequential eligibility: it
    needs the pure-mask FK shape (`_sequential_eligible`), an acyclic
    single-parent FK graph with only supported strategies
    (`out_of_core_compatible`), AND a largest mask table at/above
    `out_of_core_threshold_rows`. So a pure-mask FK job with an unsupported
    strategy, a cyclic/self-referential FK, or below the size threshold keeps
    taking the existing sequential route; only large, fully-supported FK jobs
    divert to streaming.

    `largest_table_rows` carries the largest mask table's row count from the
    (SC7a bounded) profile metadata even on the lazy `source_loader` path where
    no source is resident -- so the SC2 size gates fire there too, closing the
    F2 `None`-on-lazy-path hole. `largest_table_rows_exact` is False when that
    count is a CSV byte-size estimate (CSV has no footer); an estimated
    full-frame-bound table at/above `full_frame_reject_rows` is rejected with
    the distinct `fk_full_frame_oom_risk_rejected_estimated` code (convert to
    Parquet or set `execution_mode`), while an estimated OOC-eligible table
    still reroutes to out-of-core as usual. `largest_table_rows` is None only
    when there is genuinely no size signal (no mask table); the size gates then
    never fire.

    A mutual cross-table FK cycle (A -> B -> A) cannot be ordered by
    table_topo_order, so `auto` must not route it to sequential (it ran
    fine under full_frame before this program; routing it to sequential is
    a functional regression, not a leak -- see
    docs/backlog/s2-fk-leak-remediation-r3-guide.md section 6). A
    self-referencing table (one table, not a cross-table cycle) is NOT
    flagged and still routes normally.

    `resolved_substrate` disqualifies sequential eligibility when it is
    not `"pandas"` (see `_sequential_eligible`'s docstring): an explicit
    `substrate="polars"` (or an env-resolved one) must route to full_frame
    so `select_execution_adapter` -- not the pandas-only sequential path --
    is what actually decides polars-native-vs-fallback and stamps the
    caller-visible `executed_substrate` / `execution_adapter` telemetry.

    Threshold ordering (L2): the reject-before-read branch reads most naturally
    when `out_of_core_threshold_rows <= full_frame_reject_rows`, and that is the
    default (5M <= 7.5M). The two knobs are validated positive+typed at the
    `run_pipeline` submit choke point but their ORDERING is deliberately NOT
    force-validated, because an inverted override
    (`full_frame_reject_rows < out_of_core_threshold_rows`) is a legitimate,
    SAFE config -- e.g. a small-RAM host that rejects full-frame-bound FK jobs
    early while leaving the out-of-core reroute at its default. The reject
    branch's correctness does not actually depend on the ordering: an
    out-of-core-eligible job can NEVER reach the reject branch regardless of
    threshold order, because out-of-core eligibility is a strict subset of
    sequential eligibility, and a sequential-eligible non-cyclic job takes the
    `sequential` early return above before the reject test is evaluated. The
    reject branch is reached ONLY by full-frame-bound jobs (not
    sequential-eligible, or a cyclic FK graph), which no bounded route can take
    at any threshold ordering. So the ordering is an explanatory expectation,
    not a load-bearing invariant, and is left to the operator rather than
    rejected at submit time.
    """
    eligible, route_reason = _sequential_eligible(
        profile,
        has_generate_table=has_generate_table,
        validators=validators,
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
        resolved_substrate=resolved_substrate,
    )
    cyclic = _has_cross_table_fk_cycle(graph)
    has_relationships = bool(profile.relationships)
    rows = largest_table_rows if largest_table_rows is not None else 0
    # Out-of-core is only auto-selected for the pure-mask FK shape (a strict
    # subset of sequential eligibility) that the compat gate admits and that is
    # large enough for the route's overhead to be worth it. `cyclic` is already
    # excluded by the compat gate (`out_of_core_relationship_cycle_unsupported`),
    # but is re-checked here for defense in depth.
    out_of_core_ready = (
        eligible
        and not cyclic
        and has_mask_table
        and out_of_core_compatible
        and largest_table_rows is not None
        and rows >= out_of_core_threshold_rows
    )

    if execution_mode == "full_frame":
        # Explicit operator escape hatch: force full-frame even when large.
        # Bypasses the reject -- the operator owns the OOM risk they requested.
        return "full_frame", "override_full_frame"

    if execution_mode == "out_of_core":
        # Fail closed, mirroring the sequential override: never silently ignore
        # an explicit request by falling through to another route.
        if not has_mask_table:
            raise ConfigError(
                "execution_mode='out_of_core' requested but the job has no "
                "mask-kind table to run through the out-of-core path."
            )
        if not eligible:
            raise ConfigError(
                f"execution_mode='out_of_core' requested but the job is not "
                f"out-of-core-eligible ({route_reason})."
            )
        if not out_of_core_compatible:
            raise ConfigError(
                f"execution_mode='out_of_core' requested but the job is not "
                f"out-of-core-compatible "
                f"({out_of_core_reject_code or 'unsupported_recipe'}); use "
                f"execution_mode='full_frame' or 'auto'."
            )
        return "out_of_core", "override_out_of_core"

    if execution_mode == "sequential":
        if not eligible:
            raise ConfigError(
                f"execution_mode='sequential' requested but the job is not "
                f"sequential-eligible ({route_reason})."
            )
        if cyclic:
            raise ConfigError(
                "execution_mode='sequential' requested but the FK graph has a "
                "cross-table cycle, which the sequential path cannot order; "
                "use execution_mode='full_frame' or 'auto'."
            )
        if not has_mask_table:
            # NIT (S2 remediation guide section 8): without a mask-kind table
            # the sequential branch in run_pipeline would silently no-op and
            # fall through to full-frame, ignoring the explicit request.
            # Fail closed instead.
            raise ConfigError(
                "execution_mode='sequential' requested but the job has no "
                "mask-kind table to run through the sequential path."
            )
        return "sequential", route_reason

    # "auto"
    # B1b (§13): flag-gated byte-estimate admission, scoped to
    # relationship-bearing PURE-MASK jobs. `use_byte_estimate_routing`
    # defaults False, so this branch can never fire unless a caller opts
    # in -- the row-count logic below it is untouched and unreachable-
    # bypassed only when the flag is explicitly on AND the job is in scope.
    byte_estimate_in_scope = (
        use_byte_estimate_routing
        and has_relationships
        and has_mask_table
        and not has_generate_table
    )
    if byte_estimate_in_scope:
        # §13's ruling: admit full_frame ONLY when the conservative
        # estimate clears the budget with margin (`fits` already applied
        # the asymmetric error_band). UNPRICEABLE (`None`) is treated
        # exactly like "does not fit" -- never admit an unknown estimate to
        # full_frame; B2's probe is the future fast-path recovery for it.
        if full_frame_fits_estimate is True:
            return "full_frame", "byte_estimate_full_frame_fits"
        # B2 (§13): the static estimate did NOT confirm a fit -- give the
        # probe a chance to RECOVER full_frame with a real measurement
        # before falling back to bounded-by-eligibility. `probe_recovers_
        # full_frame` is precomputed by the caller (`resolve_probe_recovery`)
        # and is `True` ONLY when the probe actually measured a fit; `False`/
        # `None` (inconclusive, out of scope, or never run) fall straight
        # through to the unchanged bounded logic below. `use_probe_routing`
        # is re-checked HERE too (not just trusted from the caller): a
        # `probe_recovers_full_frame=True` passed in with the flag off must
        # have NO effect, matching `use_byte_estimate_routing`'s own
        # defense-in-depth discipline above.
        if use_probe_routing and probe_recovers_full_frame is True:
            return "full_frame", "probe_recovered_full_frame"
        # Does not fit (or UNPRICEABLE): route bounded by ELIGIBILITY alone,
        # never by an estimate of out_of_core/sequential peak -- their k's
        # are unmeasured placeholders (§13), and out_of_core's own runtime
        # budget (Sprint 1b) caps it regardless of how this job got routed
        # there.
        if eligible and not cyclic and out_of_core_compatible:
            return "out_of_core", "byte_estimate_bounded_out_of_core"
        if eligible and not cyclic:
            return "sequential", route_reason
        # Irreducible reject class (§13): no bounded route applies (a
        # cross-table FK cycle, or a job disqualified from sequential for
        # another reason) AND the byte estimate does not confirm full_frame
        # fits either. Reuses the EXISTING reject code
        # (`fk_full_frame_oom_risk_rejected`) with a BYTE-BASED reason -- the
        # TB-5 contract migration (§B3) kept the code and deprecated the
        # row-count basis, not the reject itself, so this remains the one
        # irreducible reject class byte-based routing cannot absorb.
        byte_estimate_full_frame_reason = (
            "cross_table_cycle" if (eligible and cyclic) else route_reason
        )
        raise ExecutionError(
            code="fk_full_frame_oom_risk_rejected",
            message=(
                "FK job rejected before read: the byte-level memory estimator "
                "(docs/plans/2026-07-10-oom-avoidance-routing-redesign.md §13) "
                "predicts this job would not fit the full_frame budget within "
                "margin, and no bounded route applies -- the job is not "
                f"out-of-core-eligible ({out_of_core_reject_code or 'not a pure-mask FK recipe'}) "
                f"and not sequential-eligible ({byte_estimate_full_frame_reason}). Reduce "
                "the job size, make it out-of-core-eligible (supported "
                "strategies + an acyclic single-parent FK graph), or force "
                "execution_mode='full_frame' to override at your own memory risk."
            ),
        )
    if out_of_core_ready:
        return "out_of_core", "out_of_core_large_fk"
    if eligible and not cyclic:
        return "sequential", route_reason
    # Full-frame-bound: no relationships, a cyclic FK graph (eligible), or a
    # relationship job disqualified from sequential. A cyclic pure-mask job
    # keeps its historical `cross_table_cycle` reason.
    full_frame_reason = "cross_table_cycle" if (eligible and cyclic) else route_reason
    # Reject before read (fail-closed): a LARGE relationship job that no bounded
    # route can take would risk a silent full-frame OOM. Divert to a clean typed
    # error instead of the OOM-killer (GATE-1 #4 prevention half). Reaching this
    # branch ALREADY implies no bounded route applies -- and this holds at ANY
    # threshold ordering (L2), not because of one: out-of-core eligibility is a
    # strict subset of sequential eligibility, and a sequential-eligible
    # non-cyclic job took the `sequential` early return above before this test,
    # so an out-of-core-eligible job can never land here regardless of whether
    # `out_of_core_threshold_rows` sits below `full_frame_reject_rows`. That is
    # why the condition does NOT re-test `out_of_core_compatible`: the compat GATE
    # can admit an FK structure
    # (edges/strategies) for a job whose SHAPE still bars out-of-core (a
    # generate+mask FK the route cannot generate), and re-testing the flag would
    # wrongly let such a job fall through to a full-frame OOM. No-relationship
    # jobs are never rejected here: a large flat single table is the layer-2
    # auto-chunk route's concern, not a full-frame FK OOM.
    if has_relationships and largest_table_rows is not None and rows >= full_frame_reject_rows:
        # An OOC-eligible job never lands here (it is sequential-eligible and so
        # took the out_of_core / sequential early return above), so an ESTIMATED
        # (CSV) count that is good enough to prefer the bounded route already did
        # so. Reaching
        # the reject branch on an estimate means the job is full-frame-bound and
        # the estimate says "too big" -- but a CSV byte-estimate can be wrong in
        # both directions, so we fail toward an operator-visible choice with a
        # distinct code rather than silently rejecting a source that might be
        # fine, and point at the exact-count fix (Parquet) or the escape hatch.
        if not largest_table_rows_exact:
            raise ExecutionError(
                code="fk_full_frame_oom_risk_rejected_estimated",
                message=(
                    f"FK job rejected before read: the largest mask table's row "
                    f"count is a CSV size estimate (~{rows:,} rows), at or above "
                    f"the full-frame reject threshold ({full_frame_reject_rows:,}); "
                    f"full-frame FK masking at this scale would risk an "
                    f"out-of-memory kill and no bounded route applies -- the job "
                    f"is not out-of-core-eligible "
                    f"({out_of_core_reject_code or 'not a pure-mask FK recipe'}) "
                    f"and not sequential-eligible ({full_frame_reason}). Convert "
                    f"the source to Parquet for an exact count or pass an explicit "
                    f"`execution_mode` (e.g. 'full_frame' to override at your own "
                    f"memory risk)."
                ),
            )
        raise ExecutionError(
            code="fk_full_frame_oom_risk_rejected",
            message=(
                f"FK job rejected before read: largest mask table has {rows:,} "
                f"rows, at or above the full-frame reject threshold "
                f"({full_frame_reject_rows:,}); full-frame FK masking at this "
                f"scale would risk an out-of-memory kill. No bounded route "
                f"applies -- the job is not out-of-core-eligible "
                f"({out_of_core_reject_code or 'not a pure-mask FK recipe'}) and "
                f"not sequential-eligible ({full_frame_reason}). Reduce the job "
                f"size, make it out-of-core-eligible (supported strategies + an "
                f"acyclic single-parent FK graph), or force "
                f"execution_mode='full_frame' to override at your own memory risk."
            ),
        )
    return "full_frame", full_frame_reason
