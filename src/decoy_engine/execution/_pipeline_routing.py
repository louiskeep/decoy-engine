"""`run_pipeline`'s execution-route DECISIONS, split out to hold the
600-LOC orchestration cap (CLAUDE.md section "Engineering best practices").

This module decides which route a job takes; it never executes one --
`_pipeline_route_exec` owns dispatching to the underlying runner
(`run_sequential`, `run_fk_out_of_core`, the chunked lane via `run_mask_chunked`) and
packaging the result into an `ExecutionResult`. `run_pipeline` calls both
modules directly in the fixed order docs/execution-backends.md describes.

Route decision order and rationale: docs/execution-backends.md, "Route decision order (decide_execution_route)".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError, ooc_ineligible, seq_ineligible

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
    post_validation: bool = False,
    resolved_substrate: str = "pandas",
    post_scan_integrity: bool = False,
) -> tuple[bool, str]:
    """Decide whether a mask job may take the bounded-memory sequential path.

    Returns (eligible, reason). `reason` is a stable telemetry token; when
    eligible it is "pure_mask_fk", otherwise it names the disqualifier.

    The sequential path streams/evicts table by table, so any run_pipeline
    post-mask step that needs every masked output resident at once
    disqualifies it: job-level validators (compare positionally against all
    sources), the fidelity report, the post-execution validation suite (it
    materializes whole columns to compare output against source), and the
    token-vault collection. Pure-generate and mixed generate+mask jobs are
    disqualified because generate tables are not masked table-by-table through
    this path.

    `post_validation` (A1) mirrors `fidelity_report` exactly: the opt-in
    post-execution scan suite needs the full frame resident, so an opted-in job
    must NOT take the sequential (or the strict-subset out-of-core) route, where
    the checks cannot run. Declining here diverts it to full-frame, or to the
    fail-closed reject-before-read when it is too large for any full-frame box.
    A silent out-of-core skip -- reporting success while the scans never ran --
    is the costly defect this decline prevents.

    `resolved_substrate` (S3 reconciliation, P1 x S2): `run_sequential` is
    pandas-only by construction. Pandas is the only masking substrate, so any
    other resolved substrate value disqualifies sequential eligibility here
    (fail-closed) rather than silently ignoring it -- a future non-pandas
    substrate would need its own explicit handling before this guard could
    admit it. Defaults to `"pandas"` so callers that never pass a substrate
    keep today's routing (the common case: a pure-mask FK job is bounded-memory
    by default) and existing unit tests that construct this predicate directly
    without a substrate argument are unaffected.
    """
    if not profile.relationships:
        return False, reasons.route.NO_RELATIONSHIPS
    if has_generate_table:
        return False, reasons.route.GENERATE_PLUS_MASK
    if validators:
        return False, reasons.route.VALIDATORS_PRESENT
    if fidelity_report:
        return False, reasons.route.FIDELITY_REPORT_REQUESTED
    if post_validation:
        return False, reasons.route.POST_VALIDATION_REQUESTED
    if post_scan_integrity:
        # Post-scan integrity checks need the resident output (full-frame), like post_validation.
        return False, reasons.route.POST_SCAN_INTEGRITY_REQUESTED
    if vault_writer is not None:
        return False, reasons.route.VAULT_WRITER_REQUESTED
    if resolved_substrate != "pandas":
        return False, reasons.route.NON_PANDAS_SUBSTRATE_REQUESTED
    return True, reasons.route.PURE_MASK_FK


def reject_explicit_sequential(
    eligible: bool, route_reason: str, cyclic: bool, has_mask_table: bool
) -> None:
    """The fail-closed checks behind an explicit `execution_mode="sequential"`."""
    if not eligible:
        raise seq_ineligible(
            f"execution_mode='sequential' requested but the job is not "
            f"sequential-eligible ({route_reason})."
        )
    if cyclic:
        raise seq_ineligible(
            "execution_mode='sequential' requested but the FK graph has a "
            "cross-table cycle, which the sequential path cannot order; "
            "use execution_mode='full_frame' or 'auto'."
        )
    if not has_mask_table:
        # Without a mask-kind table the sequential branch in run_pipeline would
        # silently no-op and fall through to full-frame, ignoring the explicit
        # request. Fail closed instead.
        raise seq_ineligible(
            "execution_mode='sequential' requested but the job has no "
            "mask-kind table to run through the sequential path."
        )


def lazy_transform_route(eligible: bool, route_reason: str, cyclic: bool) -> tuple[str, str]:
    """Sequential when the job is eligible, else the base reject: the one outcome set
    for a relationship job with a lazy transform-bearing mask table under `auto`."""
    if eligible and not cyclic:
        return "sequential", route_reason
    raise ExecutionError(
        code=reasons.runtime.FK_FULL_FRAME_OOM_RISK_REJECTED,
        message=(
            "FK job rejected before read: a table that declares transforms is not "
            "resident when routing runs (a LazySource or loader-supplied source; the "
            "isolated worker reads relationship sources lazily), so its transformed "
            "size cannot be priced or probed and full-frame is not admitted, and no "
            f"bounded route applies ({reasons.route.CROSS_TABLE_CYCLE if cyclic else route_reason}). "
            "Make the job sequential-eligible, or force execution_mode='full_frame' "
            "(or isolate=False when running isolated) to override at your own memory risk."
        ),
    )


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
    post_validation: bool = False,
    post_scan_integrity: bool = False,
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
    lazy_transform_bearing: bool = False,
    fk_ooc_dtype_hazard: bool = False,
    vault_ooc_safe: bool = False,
    validators_ooc_safe: bool = False,
    mixed_admitted: bool = False,
) -> tuple[str, str]:
    """Decide `(route, route_reason)` -- `"out_of_core"`, `"sequential"`, `"mixed"`, or
    `"full_frame"` -- or RAISE a fail-closed reject-before-read.

    Route decision order and rationale: docs/execution-backends.md, "Route decision order (decide_execution_route)".
    """
    eligible, route_reason = _sequential_eligible(
        profile,
        has_generate_table=has_generate_table,
        validators=validators,
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
        post_validation=post_validation,
        resolved_substrate=resolved_substrate,
        post_scan_integrity=post_scan_integrity,
    )
    cyclic = _has_cross_table_fk_cycle(graph)
    # D3b: the vault is the ONLY disqualifier lifted, and only onto out-of-core.
    vault_ooc_ready = (
        vault_ooc_safe
        and vault_writer is not None
        and not cyclic
        and has_mask_table
        and out_of_core_compatible
        and not fk_ooc_dtype_hazard
        and _sequential_eligible(
            profile,
            has_generate_table=has_generate_table,
            validators=validators,
            fidelity_report=fidelity_report,
            vault_writer=None,
            post_validation=post_validation,
            resolved_substrate=resolved_substrate,
            post_scan_integrity=post_scan_integrity,
        )[0]
    )
    # D3a: validators are the only disqualifier lifted here, and only onto out-of-core.
    validators_ooc_ready = (
        validators_ooc_safe
        and bool(validators)
        and not cyclic
        and has_mask_table
        and out_of_core_compatible
        and not fk_ooc_dtype_hazard
        and _sequential_eligible(
            profile,
            has_generate_table=has_generate_table,
            validators=[],
            fidelity_report=fidelity_report,
            vault_writer=vault_writer,
            post_validation=post_validation,
            resolved_substrate=resolved_substrate,
            post_scan_integrity=post_scan_integrity,
        )[0]
    )
    # `eligible` is a job-SHAPE check and stays true for a cross-table cycle, so
    # `not cyclic` is what keeps a cyclic job (no bounded route can order it) off
    # the bounded path.
    bounded_eligible = eligible and not cyclic
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
        return "full_frame", reasons.route.OVERRIDE_FULL_FRAME

    if execution_mode == "out_of_core":
        # Fail closed, mirroring the sequential override: never silently ignore
        # an explicit request by falling through to another route.
        if not has_mask_table:
            raise ooc_ineligible(
                "execution_mode='out_of_core' requested but the job has no "
                "mask-kind table to run through the out-of-core path."
            )
        if not eligible:
            raise ooc_ineligible(
                f"execution_mode='out_of_core' requested but the job is not "
                f"out-of-core-eligible ({route_reason})."
            )
        if not out_of_core_compatible:
            raise ooc_ineligible(
                f"execution_mode='out_of_core' requested but the job is not "
                f"out-of-core-compatible "
                f"({out_of_core_reject_code or 'unsupported_recipe'}); use "
                f"execution_mode='full_frame' or 'auto'."
            )
        return "out_of_core", reasons.route.OVERRIDE_OUT_OF_CORE

    if execution_mode == "sequential":
        reject_explicit_sequential(eligible, route_reason, cyclic, has_mask_table)
        return "sequential", route_reason

    # `not post_scan_integrity`: Post-scan integrity needs the resident full-frame output, so such a job declines mixed here.
    if mixed_admitted and not post_scan_integrity:
        return "mixed", reasons.route.MIXED_GENERATE_MASK_CHUNKED

    if lazy_transform_bearing and has_relationships and has_mask_table:
        # A transform-bearing mask table that is not resident cannot be priced or
        # probed before it is read, so full-frame is never admitted for it under
        # `auto`, whatever the byte-estimate flag says (generate+mask included).
        # Out-of-core declines transforms.
        return lazy_transform_route(eligible, route_reason, cyclic)

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
        if (vault_ooc_ready or validators_ooc_ready) and full_frame_fits_estimate is True:
            # A vault (or per-cell validator) job that FITS takes the lower-memory OOC route (GAP6).
            # The vault accumulates O(distinct triples) in PYTHON memory, OUTSIDE the OOC spill
            # budget, so a NON-fitting or UNPRICEABLE vault job is NOT admitted here: it falls
            # through to the full-frame-fit check (which also fails) and is rejected, exactly as a
            # too-big vault job is today. The vault cannot be bounded for an oversized job; a
            # spillable-vault follow-on could lift this. (Codex final gate HIGH.)
            return "out_of_core", reasons.route.BYTE_ESTIMATE_BOUNDED_OUT_OF_CORE
        # §13's ruling: admit full_frame ONLY when the conservative
        # estimate clears the budget with margin (`fits` already applied
        # the asymmetric error_band). UNPRICEABLE (`None`) is treated
        # exactly like "does not fit" -- never admit an unknown estimate to
        # full_frame; B2's probe is the future fast-path recovery for it.
        if full_frame_fits_estimate is True and not bounded_eligible:
            return "full_frame", reasons.route.BYTE_ESTIMATE_FULL_FRAME_FITS
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
        probe_fits = use_probe_routing and probe_recovers_full_frame is True
        if probe_fits and not bounded_eligible:
            return "full_frame", reasons.route.PROBE_RECOVERED_FULL_FRAME
        # Only a bounded-eligible job gets past the two returns above with a fit, and it
        # falls through to a bounded route (the oracle is far slower per row). Out-of-core
        # rejects some key types the oracle and sequential both complete, so such a job
        # takes sequential instead.
        if (full_frame_fits_estimate is True or probe_fits) and fk_ooc_dtype_hazard:
            return "sequential", route_reason
        # Does not fit (or UNPRICEABLE): route bounded by ELIGIBILITY alone,
        # never by an estimate of out_of_core/sequential peak -- their k's
        # are unmeasured placeholders (§13), and out_of_core's own runtime
        # budget (Sprint 1b) caps it regardless of how this job got routed there.
        if eligible and not cyclic and out_of_core_compatible:
            return "out_of_core", reasons.route.BYTE_ESTIMATE_BOUNDED_OUT_OF_CORE
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
            reasons.route.CROSS_TABLE_CYCLE if (eligible and cyclic) else route_reason
        )
        raise ExecutionError(
            code=reasons.runtime.FK_FULL_FRAME_OOM_RISK_REJECTED,
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
        return "out_of_core", reasons.route.OUT_OF_CORE_LARGE_FK
    if eligible and not cyclic:
        return "sequential", route_reason
    # Full-frame-bound: no relationships, a cyclic FK graph (eligible), or a
    # relationship job disqualified from sequential. A cyclic pure-mask job
    # keeps its historical `cross_table_cycle` reason.
    full_frame_reason = reasons.route.CROSS_TABLE_CYCLE if (eligible and cyclic) else route_reason
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
                code=reasons.runtime.FK_FULL_FRAME_OOM_RISK_REJECTED_ESTIMATED,
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
            code=reasons.runtime.FK_FULL_FRAME_OOM_RISK_REJECTED,
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
