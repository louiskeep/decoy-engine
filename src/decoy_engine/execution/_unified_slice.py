"""Task 4.5: the unified-slice production lane -- the DEFAULT-ON (activated
2026-09-20; `unified_slice_enabled=True`) per-run-flag route that runs one bounded
slice (a single non-FK Parquet mask table, the four native scalar strategies, a
resident `pa.Table` source) through the 4.3 physical plan + 4.4 shadow coordinator,
returning `ExecutionResult(outputs= ...)` identically to the pandas full-frame
route. An inert sink (the one the platform worker always attaches) is admitted
since it is never consumed on the full-frame route; a caller can force the legacy
route with `unified_slice_enabled=False`.

Shape: a per-run flag, a conservative admission predicate, and an early return of a complete
`ExecutionResult` when admitted, `None` (a single value, not a tuple -- see
`maybe_run_unified_slice`'s docstring) when not.

D1 (CRITICAL, Codex): imports are kept local so a flag-off caller never pulls in
`execution.physical`. Every
function below that reaches into `decoy_engine.execution.physical` (which imports eagerly,
`physical/__init__.py:62-97`) does so import-local, and NONE of those functions run before
`maybe_run_unified_slice` has already confirmed the flag is on and
`_unified_slice_admission.cheap_admission` (which has zero `execution.physical` reach) has
already admitted. A flag-off caller therefore never imports `execution.physical` at all --
proved by `tests/physical/test_unified_slice_inertness.py`.

The admission predicate itself (D3) lives in `_unified_slice_admission.py`, split out to hold
this module's own size under the ~600-LOC orchestration cap (CLAUDE.md "Engineering best
practices"); this module owns D6/D7/D8's execution + activation + exception-boundary concerns
and the one `run_pipeline` call site.

CHANGE 4 (Codex determination, module-size ratchet remediation): `run_from_pipeline_locals` --
not `maybe_run_unified_slice` itself -- is `_pipeline.py`'s actual call site now, so that
module's own 645-LOC ceiling (`tests/sentry/test_module_size.py`) does not have to carry this
lane's ~30-keyword argument block. See `run_from_pipeline_locals`'s docstring.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Final

from decoy_engine.errors import DecoyError
from decoy_engine.execution import _unified_slice_admission as _admission
from decoy_engine.execution._row_errors import RowErrorRecord
from decoy_engine.generation.pool._events import QualityWarning

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    import pyarrow as pa

    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._planner import ExecutionPlan
    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._readers import LazySource
    from decoy_engine.profile._types import Profile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "UnifiedSliceInvariantError",
    "maybe_run_unified_slice",
    "run_from_pipeline_locals",
]

# The one quality_metrics leaf D7's completed-execution evidence lands
# under; the differential parity harness (D9) asserts it present flag-on,
# absent flag-off, and compares every OTHER quality_metrics leaf exactly.
QUALITY_METRICS_KEY = "unified_slice_activation"

_logger = logging.getLogger(__name__)
_REROUTE_LOG = "unified_slice_unexpected_exception_reroute exc_type=%s table=%s"


class UnifiedSliceInvariantError(DecoyError):
    """Fail-closed boundary error for the unified-slice lane (D8).

    Raised ONLY once a job has already been ADMITTED: a coded
    `ShadowDifference` from the coordinator, or completed-execution evidence
    (D7) that does not match what admission planned. Either one means the
    admission predicate itself has a bug -- a real, otherwise-normal job
    reached execution when it should have declined -- not that this
    particular job failed for an ordinary reason. Deliberately NOT
    `ExecutionError` (the user-facing coded execution boundary,
    `execution/_errors.py:19-25`): a caller must never be able to catch this
    the way it catches a normal masking failure. Not exported publicly.
    Precedent: `NativeChunkSchemaDriftError` (`execution/native/_chunk_
    schema.py:17-35`), a sibling private `DecoyError` subclass raised by
    another lane's own internal-consistency guard.
    """

    code: str = "unified_slice_invariant_violation"


def _typed_warnings(items: tuple[object, ...]) -> tuple[QualityWarning, ...]:
    """`ShadowRunResult.warnings` is typed `tuple[object, ...]` purely for
    shape parity with the oracle's `ExecutionResult` (`_shadow_coordinator.
    py`'s own docstring); every strategy this slice admits is zero-
    diagnostic, so it is always empty in practice. Asserts the runtime type
    rather than a bare cast, so a future non-empty shadow warning of the
    wrong shape fails loudly here instead of reaching a caller silently
    mistyped."""
    for item in items:
        if not isinstance(item, QualityWarning):
            raise UnifiedSliceInvariantError(
                f"unified slice: coordinator emitted a non-QualityWarning warning: {item!r}"
            )
    return items  # type: ignore[return-value]  # narrowed by the loop above


def _typed_row_errors(items: tuple[object, ...]) -> tuple[RowErrorRecord, ...]:
    """The `row_errors` counterpart to `_typed_warnings`; see its docstring."""
    for item in items:
        if not isinstance(item, RowErrorRecord):
            raise UnifiedSliceInvariantError(
                f"unified slice: coordinator emitted a non-RowErrorRecord row error: {item!r}"
            )
    return items  # type: ignore[return-value]  # narrowed by the loop above


def _execute_admitted(
    *,
    candidate: _admission.CheapCandidate,
    config: dict[str, Any],
    plan: Plan,
    profile: Profile,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    table_kinds: dict[str, str],
    caller_sources: Mapping[str, pa.Table | LazySource],
    execution_mode: str,
    fidelity_report: bool,
    vault_writer: Any,
    auto_chunk: bool,
    chunk_size_rows: int,
    auto_chunk_threshold_rows: int,
    out_of_core_threshold_rows: int,
    full_frame_reject_rows: int,
    use_byte_estimate_routing: bool,
    use_probe_routing: bool,
    fpe_chunk_count: int,
    max_workers: int,
    fallback_to_pandas: bool,
    out_of_core_reorder_threshold_rows: int | None,
    out_of_core_budget_bytes: int | None,
    engine_version: str,
    key_provider: KeyProvider | None,
    route_reason: str,
    substrate: str | None,
    resolved_substrate: str,
    explain_plan: bool,
    execution_plan_decision: ExecutionPlan | None,
) -> ExecutionResult | None:
    """Everything past the cheap admission gate: build the LIVE (D4)
    `PhysicalPlanInputs`, compile the real physical plan, run the remaining
    admission checks against it, and -- only if every one holds -- execute
    through the 4.4 coordinator and assemble the returned `ExecutionResult`.
    First `execution.physical` reach in this module's call chain.

    Accepts `resolved_substrate` from the caller rather than re-deriving it:
    `run_pipeline` resolves the substrate exactly ONCE (`_pipeline.py`'s own
    `resolved_substrate = resolve_substrate(substrate)`), and a second
    `resolve_substrate(substrate)` call here would re-read `DECOY_SUBSTRATE`
    from the process environment a second time -- a TOCTOU window where an
    env change mid-run could disagree with what admission already gated on.
    `adapter` (`select_execution_adapter(...)`) is still built here from the
    now-single `resolved_substrate` value plus the other raw knobs already
    required for `stamp_execution_metrics` parity: it is a pure function of
    its arguments, not a second env read, so building it here (rather than
    accepting a third parameter) costs nothing and keeps `_pipeline.py`'s
    one call site to fewer duplicate pointers into the same state.
    """
    import pyarrow as pa

    from decoy_engine.execution import _pipeline_finalize, _pipeline_route_exec
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._substrate import select_execution_adapter
    from decoy_engine.execution.physical._activation import build_unified_slice_activation
    from decoy_engine.execution.physical._compiler import compile_physical_plan
    from decoy_engine.execution.physical._live_inputs import build_live_physical_plan_inputs
    from decoy_engine.execution.physical._shadow_context import ShadowContext
    from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
    from decoy_engine.execution.physical._shadow_diff_codes import ShadowDifference
    from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot

    try:
        adapter = select_execution_adapter(
            substrate=resolved_substrate,
            fpe_chunk_count=fpe_chunk_count,
            max_workers=max_workers,
            fallback_to_pandas=fallback_to_pandas,
        )

        inputs = build_live_physical_plan_inputs(
            config=config,
            plan=plan,
            profile=profile,
            registry=registry,
            graph=graph,
            table_kinds=table_kinds,
            caller_sources=caller_sources,
            resolved_substrate=resolved_substrate,
            execution_mode=execution_mode,
            fidelity_report=fidelity_report,
            vault_writer_present=vault_writer is not None,
            validators=tuple(config.get("validators") or ()),
            auto_chunk=auto_chunk,
            chunk_size_rows=chunk_size_rows,
            auto_chunk_threshold_rows=auto_chunk_threshold_rows,
            out_of_core_threshold_rows=out_of_core_threshold_rows,
            full_frame_reject_rows=full_frame_reject_rows,
            use_byte_estimate_routing=use_byte_estimate_routing,
            use_probe_routing=use_probe_routing,
            fpe_chunk_count=fpe_chunk_count,
            max_workers=max_workers,
            fallback_to_pandas=fallback_to_pandas,
            out_of_core_reorder_threshold_rows=out_of_core_reorder_threshold_rows,
            out_of_core_budget_bytes=out_of_core_budget_bytes,
            engine_version=engine_version,
        )
        physical_plan = compile_physical_plan(inputs)

        physical_table = _admission.resident_contract_admission(
            physical_plan,
            table=candidate.table,
            source=candidate.source,
            plan=plan,
            registry=registry,
            graph=graph,
        )
        if physical_table is None:
            return None

        activation = build_unified_slice_activation(
            physical_plan,
            table=candidate.table,
            legacy_disposition="full_frame",
            unified_slice_enabled=True,
        )

        ctx = ShadowContext.from_key_provider(plan=plan, key_provider=key_provider)
        snapshot = capture_shadow_snapshot({candidate.table: candidate.source})

        try:
            shadow_result = ShadowCoordinator(ctx=ctx).run(physical_plan, snapshot)
        except ShadowDifference as exc:
            # D8: an admitted job's execution boundary. Admission already
            # preflighted companion availability, node binding, coverage, and
            # invariants, so reaching a coded `ShadowDifference` here means the
            # admission predicate has a gap, not that this job is ineligible --
            # fail closed with a non-shadow type rather than leak the shadow
            # exception or return a partial `outputs` dict.
            raise UnifiedSliceInvariantError(
                f"unified slice: coded shadow difference {exc.code!r} on an admitted "
                f"table {candidate.table!r}; this indicates an admission-predicate gap, "
                "not a normal execution outcome."
            ) from exc

        # D7: stamp completed-execution evidence ONLY from the successfully-
        # returned, already-validated coordinator result -- never from the
        # pre-execution activation overlay alone (Settled decision 1).
        node_evidence: dict[str, dict[str, Any]] = {}
        for node in physical_table.nodes:
            binding = node.execution
            if binding is None:  # pragma: no cover - excluded by resident_contract_admission
                raise UnifiedSliceInvariantError(
                    f"unified slice: node {node.node_id!r} lost its admitted binding "
                    "between admission and execution."
                )
            evidence = shadow_result.route_evidence.get(node.node_id)
            if (
                evidence is None
                or not evidence.executed
                or evidence.actual_operator != binding.operator_id
            ):
                raise UnifiedSliceInvariantError(
                    f"unified slice: node {node.node_id!r} completed without matching "
                    "completed-execution evidence."
                )
            if (
                binding.operator_id == _admission.HASH_OPERATOR_ID
                and not evidence.compiled_kernel_executed
            ):
                raise UnifiedSliceInvariantError(
                    f"unified slice: hash node {node.node_id!r} completed without positive "
                    "compiled-kernel evidence."
                )
            node_evidence[node.node_id] = {
                "operator": evidence.actual_operator,
                "executed": evidence.executed,
                "compiled_kernel_executed": evidence.compiled_kernel_executed,
            }

        # CHANGE 2 (hardened D9 fix): SOURCE-SHAPED reconstruction, not a round-
        # trip of the coordinator's own metadata-free output. `candidate.
        # source_frame` is the SAME source-aware pandas conversion the legacy
        # adapter performs (`_pandas_adapter.py:210`'s `to_pandas_fk_safe`; here
        # `fk_columns` is empty (relationships declined) but a group_key `group_by`
        # SIBLING is fk-safe-typed so an integer sibling reads as its nullable
        # dtype exactly like the oracle -- see cheap_admission); leaving a passthrough
        # column untouched on it reproduces the legacy `PassthroughHandler`
        # exactly (it is a literal no-op, `_strategies/_passthrough.py`), and
        # overlaying a masked column's `to_pylist()` POSITIONALLY reproduces
        # every tokenizing handler's own `df[column] = masked.to_pylist()`
        # assignment (`_redact.py` / `_truncate.py` / `_hash.py`). The closing
        # `pa.Table.from_pandas(frame, preserve_index=False)` is then EXACTLY
        # the legacy adapter's own conversion (`_pandas_adapter.py:325`),
        # attaching the identical `b"pandas"` schema metadata by construction --
        # not by hand-copying bytes. `candidate.source_frame` is single-use
        # (admission built it once for this call only), so mutating it in place
        # costs no extra conversion beyond the one admission already paid for.
        frame = candidate.source_frame
        masked_table = shadow_result.outputs[candidate.table]
        # A ZERO-ROW overlay via to_pylist() assigns [], which pandas infers as
        # float64 -- right for the tokenizing oracles (empty -> float64) but wrong
        # for bucket_perturb, whose passed-through source object series is legacy
        # null. For an empty table, to_pandas() carries the coordinator's
        # authoritative empty dtype (from _assemble_column) through the
        # reconstruction so flag-on matches flag-off's dtype + metadata; a non-empty
        # column stays on to_pylist(), the exact legacy tokenizing assignment.
        empty = masked_table.num_rows == 0
        for node in physical_table.nodes:
            if node.strategy == "passthrough":
                continue
            column = node.columns[0]
            masked_col = masked_table.column(column)
            frame[column] = masked_col.to_pandas() if empty else masked_col.to_pylist()
        outputs = {candidate.table: pa.Table.from_pandas(frame, preserve_index=False)}
        quality_metrics: dict[str, Any] = {}
        _pipeline_finalize.stamp_execution_metrics(
            quality_metrics,
            adapter=adapter,
            substrate=substrate,
            resolved_substrate=resolved_substrate,
            fpe_chunk_count=fpe_chunk_count,
            max_workers=max_workers,
            fallback_to_pandas=fallback_to_pandas,
            route_chunked=False,
            auto_chunk=auto_chunk,
            chunk_size_rows=chunk_size_rows,
            auto_chunk_threshold_rows=auto_chunk_threshold_rows,
            table_kinds=table_kinds,
            caller_sources={candidate.table: candidate.source},
            execution_plan_decision=execution_plan_decision,
        )
        # Post-validation declines the unified slice, so its quarantine keep-mask
        # (second return) is unused here.
        outputs, _ = _pipeline_finalize.finalize_validators_and_quarantine(
            outputs,
            config=config,
            caller_sources={candidate.table: candidate.source},
            mask_row_errors=tuple(shadow_result.row_errors),
            quality_metrics=quality_metrics,
        )
        if explain_plan and execution_plan_decision is not None:
            quality_metrics["execution_plan"] = {
                "mode": execution_plan_decision.mode,
                "reason": execution_plan_decision.reason,
                "rejections": dict(execution_plan_decision.rejections),
            }
        quality_metrics["execution"] = _pipeline_route_exec.execution_telemetry(
            route="full_frame",
            route_reason=route_reason,
            sink=None,
            source_loader=None,
            sources_resident=True,
        )
        quality_metrics[QUALITY_METRICS_KEY] = {
            "activated": True,
            "table": candidate.table,
            "activation_hash": activation.activation_hash,
            "plan_hash": physical_plan.plan_hash,
            "nodes": node_evidence,
        }

        return ExecutionResult(
            outputs=outputs,
            timings=(),
            boundary_conversion_ms=0.0,
            warnings=_typed_warnings(shadow_result.warnings),
            quality_metrics=quality_metrics,
            table_kinds=dict(table_kinds),
            row_errors=_typed_row_errors(shadow_result.row_errors),
        )
    except UnifiedSliceInvariantError:
        raise
    except Exception as exc:
        _logger.warning(_REROUTE_LOG, type(exc).__name__, candidate.table)
        return None


def maybe_run_unified_slice(
    *,
    unified_slice_enabled: bool,
    config: dict[str, Any],
    plan: Plan,
    profile: Profile,
    graph: RelationshipGraph,
    table_kinds: dict[str, str],
    caller_sources: Mapping[str, pa.Table | LazySource],
    source_loader: Callable[[str], pa.Table] | None,
    sink: TransactionalSink | None,
    fidelity_report: bool,
    post_validation: bool = False,
    vault_writer: Any,
    route: str,
    route_chunked: bool,
    registry: ProviderRegistry,
    substrate: str | None,
    resolved_substrate: str,
    fpe_chunk_count: int,
    max_workers: int,
    fallback_to_pandas: bool,
    auto_chunk: bool,
    chunk_size_rows: int,
    auto_chunk_threshold_rows: int,
    out_of_core_threshold_rows: int,
    full_frame_reject_rows: int,
    use_byte_estimate_routing: bool,
    use_probe_routing: bool,
    out_of_core_budget_bytes: int | None,
    out_of_core_reorder_threshold_rows: int | None,
    execution_mode: str,
    explain_plan: bool,
    execution_plan_decision: ExecutionPlan | None,
    route_reason: str,
    key_provider: KeyProvider | None,
    engine_version: str,
) -> ExecutionResult | None:
    """`run_pipeline`'s single call site for the Task 4.5 unified-slice lane.

    Sits after both routing layers (Layer 1 relationship routing, Layer 2
    auto-chunk), before `resolve_resident_sources`.

    Returns `None` on absolutely any doubt: the flag is off, the job has no
    mask table, or either admission stage declines. It returns a single
    `ExecutionResult | None` -- the unified slice does not
    add a new field to `ExecutionResult` for route evidence (D7's proof
    lives under `quality_metrics[QUALITY_METRICS_KEY]` instead, which is
    populated only on the admitted-and-executed path), so there is no
    second value for a report object to carry.
    """
    # D1: checked before this module reaches for anything under
    # `decoy_engine.execution.physical` -- that package's `__init__.py`
    # imports eagerly, so even a "just import one submodule" reach would
    # pull in the whole seam. Nothing below this line runs for a flag-off
    # caller.
    has_mask_table = any(kind == "mask" for kind in table_kinds.values())
    if not (has_mask_table and unified_slice_enabled):
        return None

    # D1 (Codex final-gate BLOCKER): `resolved_substrate` is `run_pipeline`'s ONE resolution of
    # the substrate (`_pipeline.py`'s own `resolve_substrate(substrate)`
    # call), threaded straight through -- never re-read here. A second
    # `resolve_substrate(substrate)` call in this module would re-read
    # `DECOY_SUBSTRATE` from the process environment a second time, opening a
    # TOCTOU window where an env change between `run_pipeline`'s resolution
    # and this admission gate could disagree with what was already decided.
    candidate = _admission.cheap_admission(
        route=route,
        route_chunked=route_chunked,
        resolved_substrate=resolved_substrate,
        sink=sink,
        source_loader=source_loader,
        fidelity_report=fidelity_report,
        post_validation=post_validation,
        vault_writer=vault_writer,
        config=config,
        profile=profile,
        table_kinds=table_kinds,
        caller_sources=caller_sources,
    )
    if candidate is None:
        return None

    return _execute_admitted(
        candidate=candidate,
        config=config,
        plan=plan,
        profile=profile,
        registry=registry,
        graph=graph,
        table_kinds=table_kinds,
        caller_sources=caller_sources,
        execution_mode=execution_mode,
        fidelity_report=fidelity_report,
        vault_writer=vault_writer,
        auto_chunk=auto_chunk,
        chunk_size_rows=chunk_size_rows,
        auto_chunk_threshold_rows=auto_chunk_threshold_rows,
        out_of_core_threshold_rows=out_of_core_threshold_rows,
        full_frame_reject_rows=full_frame_reject_rows,
        use_byte_estimate_routing=use_byte_estimate_routing,
        use_probe_routing=use_probe_routing,
        fpe_chunk_count=fpe_chunk_count,
        max_workers=max_workers,
        fallback_to_pandas=fallback_to_pandas,
        out_of_core_reorder_threshold_rows=out_of_core_reorder_threshold_rows,
        out_of_core_budget_bytes=out_of_core_budget_bytes,
        engine_version=engine_version,
        key_provider=key_provider,
        route_reason=route_reason,
        substrate=substrate,
        resolved_substrate=resolved_substrate,
        explain_plan=explain_plan,
        execution_plan_decision=execution_plan_decision,
    )


# CHANGE 4: the two `maybe_run_unified_slice` keyword names whose value
# lives under a DIFFERENT name in `run_pipeline`'s own locals (both are that
# function's one-time-resolved value, `resolved_registry` / `resolved_
# key_provider` -- its own naming convention for them, not this lane's).
# Every other keyword below is a bare same-name pass-through.
_PIPELINE_RESOLVED_NAMES: Final[dict[str, str]] = {
    "registry": "resolved_registry",
    "key_provider": "resolved_key_provider",
}

# Every OTHER `maybe_run_unified_slice` keyword: `run_pipeline` binds a
# local of the identical name by the time it reaches this lane's call site.
_PIPELINE_LOCAL_KWARGS: Final[tuple[str, ...]] = (
    "unified_slice_enabled",
    "config",
    "plan",
    "profile",
    "graph",
    "table_kinds",
    "caller_sources",
    "source_loader",
    "sink",
    "fidelity_report",
    "post_validation",
    "vault_writer",
    "route",
    "route_chunked",
    "substrate",
    "resolved_substrate",
    "fpe_chunk_count",
    "max_workers",
    "fallback_to_pandas",
    "auto_chunk",
    "chunk_size_rows",
    "auto_chunk_threshold_rows",
    "out_of_core_threshold_rows",
    "full_frame_reject_rows",
    "use_byte_estimate_routing",
    "use_probe_routing",
    "out_of_core_budget_bytes",
    "out_of_core_reorder_threshold_rows",
    "execution_mode",
    "explain_plan",
    "execution_plan_decision",
    "route_reason",
    "engine_version",
)


def run_from_pipeline_locals(local_vars: Mapping[str, Any]) -> ExecutionResult | None:
    """`_pipeline.py`'s actual call site for this lane (CHANGE 4, Codex
    determination): that module's module-size sentry allowlist is SHRINK-
    ONLY (`tests/sentry/test_module_size.py:14`, "update the census only by
    shrinking, never by raising") and sits just under its 600-LOC sentry cap,
    so this lane's own ~30-keyword call cannot live there.

    By the time `run_pipeline` reaches its `maybe_run_unified_slice` call it
    has already bound every fact this lane needs as an ordinary local
    variable (most under the IDENTICAL name this lane's own keyword uses,
    the two exceptions named in `_PIPELINE_RESOLVED_NAMES`), so forwarding
    its own `locals()` verbatim keeps that call site itself to one line
    instead of the argument block this function now owns.

    The explicit opt-out (`unified_slice_enabled=False`) reads ONLY the stable
    `unified_slice_enabled` run_pipeline parameter and returns before indexing
    any other local. So a future rename of one of the forwarded locals can only
    break the flag-on path (now the default) -- caught loudly by the flag-on
    test matrix and the import-time `_assert_forwarding_covers_signature` check
    below -- and never the early opt-out return.
    """
    if not local_vars.get("unified_slice_enabled"):
        return None
    kwargs: dict[str, Any] = {name: local_vars[name] for name in _PIPELINE_LOCAL_KWARGS}
    for kwarg_name, local_name in _PIPELINE_RESOLVED_NAMES.items():
        kwargs[kwarg_name] = local_vars[local_name]
    return maybe_run_unified_slice(**kwargs)


def _assert_forwarding_covers_signature() -> None:
    """Fail at IMPORT if the `locals()` forwarding drifts from
    `maybe_run_unified_slice`'s own parameters. The forwarding is invisible to
    mypy (a `Mapping[str, Any]`), so this runtime coverage check is the
    lightweight stand-in for a typed carrier: a renamed, added, or removed
    keyword that the forwarding lists no longer reflect is a bug that must fail
    now, at import, not silently mis-forward at runtime."""
    import inspect

    params = set(inspect.signature(maybe_run_unified_slice).parameters)
    forwarded = set(_PIPELINE_LOCAL_KWARGS) | set(_PIPELINE_RESOLVED_NAMES)
    if params != forwarded:
        raise AssertionError(
            "unified-slice pipeline forwarding drifted from maybe_run_unified_slice's "
            f"signature: missing={sorted(params - forwarded)}, extra={sorted(forwarded - params)}"
        )


_assert_forwarding_covers_signature()
