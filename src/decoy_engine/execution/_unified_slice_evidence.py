"""Completed-execution evidence for the unified-slice lane (D7), assembled purely.

`assemble_node_evidence` turns the coordinator's per-node operator evidence and the
timing collector's records into the JSON-safe `nodes` leaf under
`quality_metrics["unified_slice_activation"]`. It takes no context, so every invariant
is unit-testable without running a pipeline. Each node entry carries the same four
fields the chunked route publishes per column (`planned_backend`, `executed_backend`,
`calls`, `elapsed_ms`) next to the original three, and the executed backend comes from
the one rule the chunked route also uses (`_chunked_evidence.executed_backend`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from decoy_engine.execution import _unified_slice_admission as _admission
from decoy_engine.execution._unified_slice import UnifiedSliceInvariantError
from decoy_engine.execution.native import _chunked_evidence

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from decoy_engine.execution.physical._plan import PhysicalNode
    from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence
    from decoy_engine.instrumentation.timing import StrategyTimingRecord

__all__ = ["assemble_node_evidence"]

# Operators whose "the compiled kernel really ran" claim must be observed, not
# inferred: a completed node of one of these without positive evidence is an
# admission bug. The value-dependent kernels (bucket_perturb, date_shift, group_key)
# are deliberately absent: running no compiled call on idle input is legitimate and is
# reported as `arrow_python` instead of failing.
_POSITIVE_KERNEL_EVIDENCE_OPERATOR_IDS: Final = frozenset(
    {_admission.HASH_OPERATOR_ID, _admission.FAKER_OPERATOR_ID}
)

_ELAPSED_DECIMALS = 3


def _timing_by_node(
    nodes: Iterable[PhysicalNode], timing_records: Iterable[StrategyTimingRecord]
) -> dict[str, float]:
    """Join the collector's records to nodes on `(strategy, column)`, as a bijection.

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
    elapsed_by_node = _timing_by_node(nodes, timing_records)
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
            "elapsed_ms": round(float(elapsed_by_node[node.node_id]), _ELAPSED_DECIMALS),
        }
    return node_evidence
