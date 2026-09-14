"""Task 4.4 C0: slice-only `ExecutionBinding` construction for `PhysicalNode`.

Called from the compiler's `_build_nodes` at compile time, for exactly the
four native-admitted slice strategies this task shadows: passthrough,
redact, truncate, keyed hash. Every other node is left unbound
(`PhysicalNode.execution is None`) -- out of scope for 4.4
(TASK-4.4-PLAN.md's slice boundary).

Secrets never appear here: `KeyBinding` carries only the non-secret
`KeySource` token (`native/_capabilities.py:51`) plus the namespace. The
resolved `KeyProvider` and mask-key bytes live exclusively in the runtime
`ShadowContext` (`_shadow_context.py`), never on this frozen binding.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution.native._capabilities import capabilities_for
from decoy_engine.execution.native._chunk_masking import _resolve_truncate_keep
from decoy_engine.execution.native._requirements import resolve_input_arrow_type
from decoy_engine.execution.physical._plan import ExecutionBinding, KeyBinding
from decoy_engine.plan._types import ColumnSeed

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.execution.native._requirements import NodeRequirements
    from decoy_engine.execution.physical._inputs import PhysicalPlanInputs

# The exact four operators Task 4.4 shadows (TASK-4.4-PLAN.md C2); a node
# outside this set is never bound, regardless of native admission.
SLICE_STRATEGIES: Final[frozenset[str]] = frozenset({"passthrough", "redact", "truncate", "hash"})

OPERATOR_ID_BY_STRATEGY: Final[dict[str, str]] = {
    "passthrough": "native_passthrough",
    "redact": "native_redact",
    "truncate": "native_truncate",
    "hash": "native_keyed_hash",
}

_SLICE_ADMITTED_REASON_PREFIX: Final = "slice_native_admitted"

__all__ = [
    "OPERATOR_ID_BY_STRATEGY",
    "SLICE_STRATEGIES",
    "execution_binding_for_slice_node",
]


def _batch_estimate(table: str, inputs: PhysicalPlanInputs) -> int | None:
    source = inputs.caller_sources.get(table)
    return source.num_rows if isinstance(source, pa.Table) else None


def execution_binding_for_slice_node(
    work_node: WorkNode,
    *,
    table: str,
    inputs: PhysicalPlanInputs,
    requirements: NodeRequirements,
) -> ExecutionBinding | None:
    """The Task 4.4 C0 binding for one compiled `WorkNode`, or `None` when it
    falls outside the shadowed slice: a different strategy, a composite/
    group node kind, or a strategy the native kernel did not admit at this
    node's resolved config (`requirements.fallback_policy != "native"`).
    """
    if work_node.kind != "scalar" or work_node.strategy not in SLICE_STRATEGIES:
        return None
    if requirements.fallback_policy != "native" or requirements.output_arrow_schema is None:
        return None
    # No slice strategy declares a prepass (all four are row-local,
    # non-global draw sites); a future strategy added to SLICE_STRATEGIES
    # without updating the shadow coordinator's "no prepasses" contract
    # (C1) must fail loudly here rather than bind silently.
    if requirements.required_prepasses:
        raise AssertionError(
            f"{table}:{work_node.columns!r}: strategy {work_node.strategy!r} declared "
            f"required prepasses {requirements.required_prepasses!r}, which the Task 4.4 "
            "shadow slice does not support; do not add it to SLICE_STRATEGIES."
        )

    plan_slice = work_node.plan_slice
    if not isinstance(plan_slice, ColumnSeed):  # pragma: no cover - scalar nodes always carry one
        return None

    strategy = work_node.strategy
    column = work_node.columns[0]
    cfg = provider_config_to_dict(plan_slice.provider_config)
    resolved_config: dict[str, Any] = dict(cfg)
    if strategy == "truncate":
        resolved_config["keep"] = _resolve_truncate_keep(cfg)

    input_type = resolve_input_arrow_type(table, column, inputs.profile) or pa.string()
    input_schema = pa.schema([pa.field(column, input_type)])

    caps = capabilities_for(strategy)
    key_binding: KeyBinding | None = None
    if strategy == "hash":
        if caps.key_source is None or plan_slice.namespace is None:
            # hash_requires_namespace is enforced upstream of the native
            # route too (native_keyed_hash's own guard); a namespace-less
            # hash column never reaches here as a "native"-admitted node.
            return None
        key_binding = KeyBinding(key_source=caps.key_source, namespace=plan_slice.namespace)

    return ExecutionBinding(
        operator_id=OPERATOR_ID_BY_STRATEGY[strategy],
        operator_reason=f"{_SLICE_ADMITTED_REASON_PREFIX}:{strategy}",
        resolved_config=tuple(sorted(resolved_config.items())),
        input_schema=input_schema,
        output_schema=requirements.output_arrow_schema,
        determinism_family=caps.draw_family,
        determinism_version=inputs.plan.seed_protocol_version,
        key_binding=key_binding,
        diagnostic_obligations=requirements.diagnostic_reducers,
        required_prepasses=requirements.required_prepasses,
        batch_estimate=_batch_estimate(table, inputs),
    )
