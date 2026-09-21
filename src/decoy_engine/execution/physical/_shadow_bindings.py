"""Task 4.4 C0: slice-only `ExecutionBinding` construction for `PhysicalNode`.

Called from the compiler's `_build_nodes` at compile time, for exactly the
native-admitted slice strategies the shadow coordinator shadows: passthrough,
redact, truncate, keyed hash (Task 4.4), (Task 4.6 slice 1) deterministic
faker over the frozen C1 provider allowlist, and (Phase 5 Track B)
deterministic categorical over string categories. Every other node is left
unbound (`PhysicalNode.execution is None`) -- out of scope for this slice.

Secrets never appear here: `KeyBinding` carries only the non-secret
`KeySource` token (`native/_capabilities.py:51`) plus the namespace, and
`PoolBinding` carries only `provider`/`plan_pool_size`. The resolved
`KeyProvider` and mask-key bytes live exclusively in the runtime
`ShadowContext` (`_shadow_context.py`), never on this frozen binding.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa

from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._categorical import _build_cdf
from decoy_engine.execution.native._capabilities import capabilities_for
from decoy_engine.execution.native._chunk_masking import _resolve_truncate_keep
from decoy_engine.execution.native._phase3_eligibility import C1_PROVIDER_ALLOWLIST
from decoy_engine.execution.native._provider_class import classify_provider
from decoy_engine.execution.native._requirements import resolve_input_arrow_type
from decoy_engine.execution.physical._plan import ExecutionBinding, KeyBinding, PoolBinding
from decoy_engine.plan._types import ColumnSeed

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.execution.native._requirements import NodeRequirements
    from decoy_engine.execution.physical._inputs import PhysicalPlanInputs

# The slice strategies the shadow coordinator admits (TASK-4.4-PLAN.md C2 +
# Task 4.6 slice 1's faker addition); a node outside this set is never bound,
# regardless of native admission.
SLICE_STRATEGIES: Final[frozenset[str]] = frozenset(
    {"passthrough", "redact", "truncate", "hash", "faker", "categorical"}
)

OPERATOR_ID_BY_STRATEGY: Final[dict[str, str]] = {
    "passthrough": "native_passthrough",
    "redact": "native_redact",
    "truncate": "native_truncate",
    "hash": "native_keyed_hash",
    "faker": "native_faker_select",
    "categorical": "native_categorical",
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


def _table_in_fk_relationship(table: str, inputs: PhysicalPlanInputs) -> bool:
    """Whether `table` sits on either side of an FK edge -- checked against
    BOTH the resolved `RelationshipGraph` (the post-namespace-resolution
    edge list `_compiler.relationship_role` also reads) and the raw
    config-declared relationships (mirroring the native pool route's own
    belt-and-suspenders guard, `native._dispatch._table_in_declared_
    relationship`). The shadow coordinator masks one table independently of
    its FK neighbors, so a faker node bound on an FK parent or child would
    diverge from the oracle's cross-table resolution
    (`_pandas_adapter.py:365`); rejecting both sides here, rather than only
    one, is what keeps this a narrower admission than the oracle's.
    """
    for edge in inputs.graph.edges:
        if edge.parent_table == table or edge.child_table == table:
            return True
    for rel_entry in inputs.config.get("relationships") or ():
        if not isinstance(rel_entry, Mapping):
            continue
        parent = rel_entry.get("parent")
        if isinstance(parent, Mapping) and parent.get("table") == table:
            return True
        for child_info in rel_entry.get("children") or ():
            if isinstance(child_info, Mapping) and child_info.get("table") == table:
                return True
    return False


def _resident_source_type(
    table: str, column: str, inputs: PhysicalPlanInputs
) -> pa.DataType | None:
    """The column's ACTUAL resident Arrow type, or `None` when the source is
    not a resident `pa.Table` (a `LazySource`, or absent). Read from the
    real array rather than the profile's coarse dtype label, matching the
    native route's own faker source-type guard (`native/_dispatch.py`'s
    `faker_source_type_not_string`): the profile collapses object/string/
    category to one label, which is not proof enough for the one-shot
    (non-streaming) shadow admission decision this slice makes at compile
    time.
    """
    source = inputs.caller_sources.get(table)
    if not isinstance(source, pa.Table):
        return None
    if column not in source.schema.names:
        # An uncovered faker column (config names a column the resident source
        # lacks) declines to bind here, exactly like the hash path, and defers
        # to the same downstream coverage gate -- never a bare KeyError.
        return None
    return source.schema.field(column).type


def _faker_pool_bindable(
    *, plan_slice: ColumnSeed, table: str, column: str, inputs: PhysicalPlanInputs
) -> bool:
    """The slice domain's remaining requirements beyond JC-5 (already proven
    by the caller's `requirements.fallback_policy == "native"` check): no
    `when` gate or vault persistence, a registered POOLABLE provider in the
    frozen C1 allowlist, a resident string/large_string source, and no FK
    participation for `table`. Mirrors the native route's own
    `_phase3_eligibility._faker_column_rejection` predicate rather than
    inventing a weaker parallel one.
    """
    if plan_slice.when or plan_slice.vault:
        return False
    provider = plan_slice.provider
    if not isinstance(provider, str) or not provider:
        return False
    if provider not in C1_PROVIDER_ALLOWLIST:
        return False
    if classify_provider(provider, None, registry=inputs.registry) != "pool_native":
        return False
    resident_type = _resident_source_type(table, column, inputs)
    if resident_type is None or not (
        pa.types.is_string(resident_type) or pa.types.is_large_string(resident_type)
    ):
        return False
    return not _table_in_fk_relationship(table, inputs)


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
    # No slice strategy declares a prepass (every admitted strategy is
    # row-local, non-global); a future strategy added to SLICE_STRATEGIES
    # without updating the shadow coordinator's "no prepasses" contract
    # (C1) must fail loudly here rather than bind silently.
    if (
        requirements.required_prepasses
    ):  # pragma: no cover - unreachable while SLICE_STRATEGIES stays prepass-free
        raise AssertionError(
            f"{table}:{work_node.columns!r}: strategy {work_node.strategy!r} declared "
            f"required prepasses {requirements.required_prepasses!r}, which the shadow "
            "slice does not support; do not add it to SLICE_STRATEGIES."
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
    pool_binding: PoolBinding | None = None
    categorical_deterministic = False
    categorical_categories: tuple[str, ...] | None = None
    categorical_cdf: tuple[int, ...] | None = None
    if strategy == "hash":
        if caps.key_source is None or plan_slice.namespace is None:
            # hash_requires_namespace is enforced upstream of the native
            # route too (native_keyed_hash's own guard); a namespace-less
            # hash column never reaches here as a "native"-admitted node.
            return None
        key_binding = KeyBinding(key_source=caps.key_source, namespace=plan_slice.namespace)
    elif strategy == "faker":
        # JC-5 (`requirements.fallback_policy == "native"`, checked above)
        # already guarantees deterministic + reuse + a namespace + a
        # resolved pool_size; captured into locals so mypy narrows them
        # instead of re-reading `plan_slice.*` after the predicate call
        # below, which it cannot prove leaves them unchanged.
        namespace = plan_slice.namespace
        pool_size = plan_slice.pool_size
        provider = plan_slice.provider
        if caps.key_source is None or namespace is None or pool_size is None:
            return None  # pragma: no cover - JC-5 already guarantees these
        if not isinstance(provider, str) or not provider:
            return None  # pragma: no cover - faker always compiles a provider
        if not _faker_pool_bindable(
            plan_slice=plan_slice, table=table, column=column, inputs=inputs
        ):
            # Any miss in the shared slice-domain predicate (§3.2) leaves the
            # node unbound; the shadow coordinator simply never runs it.
            return None
        key_binding = KeyBinding(key_source=caps.key_source, namespace=namespace)
        pool_binding = PoolBinding(provider=provider, plan_pool_size=pool_size)
    elif strategy == "categorical":
        # `requirements.fallback_policy == "native"` (checked above) already
        # proved `categorical_config_rejection` passed: deterministic, a
        # namespace, a non-empty STRING category list, and a buildable weight
        # CDF. Capture the resolved categories + CDF onto the binding so the
        # operator never re-reads/re-validates config per batch.
        namespace = plan_slice.namespace
        if caps.key_source is None or namespace is None:
            return None  # pragma: no cover - config gate guarantees both
        categories_raw = cfg.get("categories")
        if not isinstance(categories_raw, (list, tuple)) or not all(
            isinstance(c, str) for c in categories_raw
        ):
            return None  # pragma: no cover - config gate guarantees string categories
        categorical_categories = tuple(categories_raw)
        weights_raw = cfg.get("weights")
        if weights_raw is not None:
            try:
                categorical_cdf = tuple(_build_cdf([float(w) for w in weights_raw]))
            except StrategyError:
                return None  # pragma: no cover - config gate already proved buildable
        key_binding = KeyBinding(key_source=caps.key_source, namespace=namespace)
        categorical_deterministic = True

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
        pool_binding=pool_binding,
        categorical_deterministic=categorical_deterministic,
        categorical_categories=categorical_categories,
        categorical_cdf=categorical_cdf,
    )
