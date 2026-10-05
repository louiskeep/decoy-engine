"""Completed-execution evidence for the unified-slice lane (D7), assembled purely.

`assemble_node_evidence` turns the coordinator's per-node operator evidence and the
timing collector's records into the JSON-safe `nodes` leaf under
`quality_metrics["unified_slice_activation"]`. It takes no context, so every invariant
is unit-testable without running a pipeline. Each node entry carries the fields the
chunked route publishes per column (`planned_backend`, `executed_backend`, `calls`) next
to the original three, and the executed backend comes from the one rule the chunked route
also uses (`_chunked_evidence.executed_backend`). Elapsed time is NOT published here: as
on the chunked route (`_pipeline_auto_chunk._without_elapsed`), it lives only in
`ExecutionResult.timings` so `quality_metrics` stays deterministic. The timing records are
still checked one-to-one against the nodes, which guarantees every node has exactly one
`timings` entry under its `(strategy, column)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa

from decoy_engine.execution import _unified_slice_admission as _admission
from decoy_engine.execution._unified_slice import UnifiedSliceInvariantError
from decoy_engine.execution.native import _chunked_evidence

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    import pandas as pd

    from decoy_engine.execution.physical._plan import PhysicalNode
    from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence
    from decoy_engine.instrumentation.timing import StrategyTimingRecord

__all__ = ["assemble_node_evidence", "reconstruct_source_shaped_output"]

# Operators whose "the compiled kernel really ran" claim must be observed, not
# inferred: a completed node of one of these without positive evidence is an
# admission bug. The value-dependent kernels (bucket_perturb, date_shift, group_key)
# are deliberately absent: running no compiled call on idle input is legitimate and is
# reported as `arrow_python` instead of failing.
_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS: Final = frozenset(
    {_admission.HASH_OPERATOR_ID, _admission.FAKER_OPERATOR_ID}
)


def _timing_by_node(
    nodes: Iterable[PhysicalNode], timing_records: Iterable[StrategyTimingRecord]
) -> dict[str, float]:
    """Check the collector's records are one-to-one with the nodes on `(strategy, column)`.

    Production calls this only for the check; the returned node-to-elapsed mapping
    exists so the attribution can be tested directly.

    The coordinator opens one `timed_strategy(node.strategy, ",".join(node.columns))`
    scope per node, and admission makes each pair unique, so anything but exactly one
    record per node (and no record without a node) is a bug."""
    keys = {node.node_id: (node.strategy, ",".join(node.columns)) for node in nodes}
    by_key: dict[tuple[str, str], float] = {}
    for record in timing_records:
        key = (record.strategy_type, record.column)
        if key in by_key:
            raise UnifiedSliceInvariantError(
                f"unified slice: duplicate timing record for {key!r}; admitted nodes are "
                "unique per (strategy, column)."
            )
        by_key[key] = record.elapsed_ms
    missing = sorted(set(keys.values()) - set(by_key))
    extra = sorted(set(by_key) - set(keys.values()))
    if missing or extra:
        raise UnifiedSliceInvariantError(
            f"unified slice: timing records do not match the admitted nodes "
            f"(missing={missing}, unexpected={extra})."
        )
    return {node_id: by_key[key] for node_id, key in keys.items()}


def assemble_node_evidence(
    nodes: Iterable[PhysicalNode],
    route_evidence: Mapping[str, OperatorCallEvidence],
    timing_records: Iterable[StrategyTimingRecord],
) -> dict[str, dict[str, Any]]:
    """Validate completed execution against the admitted plan and return per-node evidence.

    Raises `UnifiedSliceInvariantError` for a missing or not-executed node, an operator
    mismatch, a hash or Faker node without positive kernel evidence, or timing records
    that are not one-to-one with the nodes."""
    nodes = tuple(nodes)
    _timing_by_node(nodes, timing_records)
    node_evidence: dict[str, dict[str, Any]] = {}
    for node in nodes:
        binding = node.execution
        if binding is None:  # pragma: no cover - excluded by resident_contract_admission
            raise UnifiedSliceInvariantError(
                f"unified slice: node {node.node_id!r} lost its admitted binding "
                "between admission and execution."
            )
        evidence = route_evidence.get(node.node_id)
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
            binding.operator_id in _POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS
            and not evidence.compiled_kernel_executed
        ):
            raise UnifiedSliceInvariantError(
                f"unified slice: {binding.operator_id} node {node.node_id!r} completed "
                "without positive compiled-kernel evidence."
            )
        planned = _admission.BACKEND_BY_OPERATOR_ID[binding.operator_id]
        # Arrow operators never run a compiled kernel, so they are never "idle".
        kernel_idle = planned != _chunked_evidence.ARROW_PYTHON and not (
            evidence.compiled_kernel_executed
        )
        node_evidence[node.node_id] = {
            "operator": evidence.actual_operator,
            "executed": evidence.executed,
            "compiled_kernel_executed": evidence.compiled_kernel_executed,
            "planned_backend": planned,
            "executed_backend": _chunked_evidence.executed_backend(
                planned, native_admitted=True, kernel_idle=kernel_idle
            ),
            "calls": evidence.batches_run,
        }
    return node_evidence


def reconstruct_source_shaped_output(
    *,
    table: str,
    frame: pd.DataFrame,
    masked_table: pa.Table,
    nodes: Iterable[PhysicalNode],
) -> dict[str, pa.Table]:
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
    # The output bridge (Arrow column extraction and overlay through the
    # final `Table.from_pandas`) is added to the admission crossings, so
    # `boundary_conversion_ms` covers all lane boundary work outside the
    # per-node scopes and never overlaps `timings`.
    # A ZERO-ROW overlay via to_pylist() assigns [], which pandas infers as
    # float64 -- right for the tokenizing oracles (empty -> float64) but wrong
    # for bucket_perturb, whose passed-through source object series is legacy
    # null. For an empty table, to_pandas() carries the coordinator's
    # authoritative empty dtype (from _assemble_column) through the
    # reconstruction so flag-on matches flag-off's dtype + metadata; a non-empty
    # column stays on to_pylist(), the exact legacy tokenizing assignment.
    empty = masked_table.num_rows == 0
    for node in nodes:
        if node.strategy == "passthrough":
            continue
        column = node.columns[0]
        masked_col = masked_table.column(column)
        frame[column] = masked_col.to_pandas() if empty else masked_col.to_pylist()
    outputs = {table: pa.Table.from_pandas(frame, preserve_index=False)}
    return outputs
