"""Task 4.4 C0: slice-only `ExecutionBinding` construction for `PhysicalNode`.

Called from the compiler's `_build_nodes` at compile time, for exactly the
native-admitted slice strategies the shadow coordinator shadows: passthrough,
redact, truncate, keyed hash (Task 4.4), (Task 4.6 slice 1) deterministic
faker over the frozen C1 provider allowlist, and (Phase 5 Track B)
deterministic categorical over string categories, plus the two position-keyed
variants (the seeded non-deterministic categorical and the non-deterministic REUSE
faker), which `positional_categorical_bindable` / `positional_faker_bindable` admit
at this boundary because their `fallback_policy` is not native. Every other node is
left unbound (`PhysicalNode.execution is None`) -- out of scope for this slice.

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
from decoy_engine.execution._operator_registry import OPERATORS
from decoy_engine.execution.native._capabilities import capabilities_for
from decoy_engine.execution.native._categorical_prepared import (
    prepare_categorical,
    prepare_positional_categorical,
)
from decoy_engine.execution.native._determinism_protocol import draw_site_by_id
from decoy_engine.execution.native._faker_positional_admission import (
    positional_faker_config_for_column,
)
from decoy_engine.execution.native._operator_params import (
    DateShiftParams,
    FakerParams,
    GroupKeyParams,
    OperatorParams,
    PassthroughParams,
    RedactParams,
    TextRedactParams,
    TruncateParams,
    is_positional_faker_seed,
    positional_faker_params,
    resolve_operator_params,
)
from decoy_engine.execution.native._provider_class import classify_provider
from decoy_engine.execution.native._requirements import resolve_input_arrow_type
from decoy_engine.execution.physical._plan import ExecutionBinding, KeyBinding, PoolBinding
from decoy_engine.plan._types import ColumnSeed

if TYPE_CHECKING:
    from decoy_engine.execution._runner import WorkNode
    from decoy_engine.execution.native._categorical_prepared import PreparedCategorical
    from decoy_engine.execution.native._requirements import NodeRequirements
    from decoy_engine.execution.physical._inputs import PhysicalPlanInputs

# The slice strategies the shadow coordinator admits (TASK-4.4-PLAN.md C2 +
# Task 4.6 slice 1's faker addition); a node outside this set is never bound,
# regardless of native admission.
# Derived from the operator registry; edit the registry.
SLICE_STRATEGIES: Final[frozenset[str]] = frozenset(OPERATORS)

# The Faker providers the slice can pool natively (read from the operator registry).
_FAKER_PROVIDER_ALLOWLIST = OPERATORS["faker"].provider_allowlist or frozenset()

OPERATOR_ID_BY_STRATEGY: Final[dict[str, str]] = {
    spec.strategy: spec.operator_id for spec in OPERATORS.values()
}

_SLICE_ADMITTED_REASON_PREFIX: Final = "slice_native_admitted"

# The draw site each position-keyed variant runs, which names its determinism family.
_POSITIONAL_DRAW_SITE: Final = {
    "categorical": "mask.categorical_nondeterministic",
    "faker": "mask.faker_nondeterministic",
}

__all__ = [
    "OPERATOR_ID_BY_STRATEGY",
    "SLICE_STRATEGIES",
    "execution_binding_for_slice_node",
    "positional_categorical_bindable",
    "positional_faker_bindable",
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
    participation for `table`. The allowlist is the one the chunked route's
    `real_type_rejection` reads, so the two routes cannot disagree on it.
    """
    if plan_slice.when or plan_slice.vault:
        return False
    provider = plan_slice.provider
    if not isinstance(provider, str) or not provider:
        return False
    if provider not in _FAKER_PROVIDER_ALLOWLIST:
        return False
    if classify_provider(provider, None, registry=inputs.registry) != "pool_native":
        return False
    resident_type = _resident_source_type(table, column, inputs)
    if resident_type is None or not (
        pa.types.is_string(resident_type) or pa.types.is_large_string(resident_type)
    ):
        return False
    return not _table_in_fk_relationship(table, inputs)


def positional_categorical_bindable(
    plan_slice: ColumnSeed, table: str, column: str, inputs: PhysicalPlanInputs
) -> bool:
    """Whether a seeded non-deterministic categorical may bind natively: the config passes
    stage A (namespace, explicit all-string categories, buildable CDF, no `from_profile`),
    there is no `when:` gate or vault, the resident source is `string`, and the table is in
    no FK relationship. Decided here, at the binding boundary, because the compiler binds
    every table of a multi-table shadow run without the single-table admission in front of it.
    """
    if plan_slice.deterministic or plan_slice.when or plan_slice.vault:
        return False
    artifact, _reason = prepare_positional_categorical(
        column,
        namespace=plan_slice.namespace,
        provider_config=provider_config_to_dict(plan_slice.provider_config),
    )
    return (
        artifact is not None
        and _resident_source_type(table, column, inputs) == pa.string()
        and not _table_in_fk_relationship(table, inputs)
    )


def positional_faker_bindable(
    plan_slice: ColumnSeed, table: str, column: str, inputs: PhysicalPlanInputs
) -> bool:
    """Whether a non-deterministic REUSE faker may bind natively: the chunked route's stage A
    (explicit `pool_size`, allowlisted provider) over the raw config, plus the unified
    domain's own conditions (`_faker_pool_bindable`: no `when:` or vault, a poolable
    provider, a resident string source, no FK relationship). The slice's own determinism and
    cardinality mode are checked too, so a config and a compiled seed that disagree decline."""
    return (
        is_positional_faker_seed(plan_slice)
        and positional_faker_config_for_column(inputs.config, table, column) is not None
        and _faker_pool_bindable(plan_slice=plan_slice, table=table, column=column, inputs=inputs)
    )


def _key_binding(key_source: str | None, params: OperatorParams) -> KeyBinding | None:
    """The non-secret key reference of a keyed operator. Its namespace is read from the
    resolved parameters, so the binding and the operator cannot disagree on it."""
    if isinstance(params, (PassthroughParams, RedactParams, TruncateParams, TextRedactParams)):
        return None
    if key_source is None or params.namespace is None:
        return None  # pragma: no cover - the per-operator guards leave neither unset
    return KeyBinding(key_source=key_source, namespace=params.namespace)


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
    plan_slice = work_node.plan_slice
    if not isinstance(plan_slice, ColumnSeed):  # pragma: no cover - scalar nodes always carry one
        return None

    strategy = work_node.strategy
    column = work_node.columns[0]
    # A position-keyed variant resolves a non-native policy (the full-frame operators are
    # source-keyed), so its own predicate admits it here instead.
    positional = (
        strategy == "categorical"
        and positional_categorical_bindable(plan_slice, table, column, inputs)
    ) or (strategy == "faker" and positional_faker_bindable(plan_slice, table, column, inputs))
    if (
        requirements.fallback_policy != "native" and not positional
    ) or requirements.output_arrow_schema is None:
        return None
    # No slice strategy declares a prepass (every admitted strategy is
    # row-local, non-global); a future strategy added to SLICE_STRATEGIES
    # without updating the shadow coordinator's "no prepasses" contract
    # (C1) must fail loudly here rather than bind silently. date_shift declares
    # `format_detect` only without an explicit date_format, and that config is
    # rejected by `date_shift_config_rejection`, so it returned above.
    if (
        requirements.required_prepasses
    ):  # pragma: no cover - unreachable while SLICE_STRATEGIES stays prepass-free
        raise AssertionError(
            f"{table}:{work_node.columns!r}: strategy {work_node.strategy!r} declared "
            f"required prepasses {requirements.required_prepasses!r}, which the shadow "
            "slice does not support; do not add it to SLICE_STRATEGIES."
        )

    cfg = provider_config_to_dict(plan_slice.provider_config)
    namespace = plan_slice.namespace
    caps = capabilities_for(strategy)

    # Admission (`requirements.fallback_policy == "native"`, checked above) already proved each
    # operator's config gate, so most of the guards below are defensive: a node that slipped
    # past them stays unbound and the shadow coordinator never runs it. The resolver below
    # resolves defaults; it never declines.
    prepared: PreparedCategorical | None = None
    pool_binding: PoolBinding | None = None
    if strategy == "hash":
        if caps.key_source is None or namespace is None:
            # hash_requires_namespace is enforced upstream of the native
            # route too (native_keyed_hash's own guard); a namespace-less
            # hash column never reaches here as a "native"-admitted node.
            return None
    elif strategy == "faker":
        # JC-5 already guarantees deterministic + reuse + a namespace + a resolved pool_size;
        # the position-keyed variant needs no configured namespace (it defaults per column).
        # Captured into locals so mypy narrows them instead of re-reading `plan_slice.*`
        # after the predicate call below, which it cannot prove leaves them unchanged.
        pool_size = plan_slice.pool_size
        provider = plan_slice.provider
        if caps.key_source is None or pool_size is None or (namespace is None and not positional):
            return None  # pragma: no cover - JC-5 already guarantees these
        if not isinstance(provider, str) or not provider:
            return None  # pragma: no cover - faker always compiles a provider
        if not positional and not _faker_pool_bindable(
            plan_slice=plan_slice, table=table, column=column, inputs=inputs
        ):
            # Any miss in the shared slice-domain predicate (§3.2) leaves the node unbound.
            return None
        pool_binding = PoolBinding(provider=provider, plan_pool_size=pool_size)
    elif strategy == "categorical":
        # Deterministic, namespaced, string categories and a buildable CDF were proved by
        # `categorical_config_rejection`, which calls this same `prepare_categorical`.
        if caps.key_source is None or namespace is None:
            return None  # pragma: no cover - config gate guarantees both
        if positional:
            prepared, _reason = prepare_positional_categorical(
                column, namespace=namespace, provider_config=cfg
            )
        else:
            prepared, _reason = prepare_categorical(
                column, deterministic=True, namespace=namespace, provider_config=cfg
            )
        if prepared is None:
            return None  # pragma: no cover - config gate already proved it admissible
    elif strategy in ("bucket_perturb", "date_shift"):
        if caps.key_source is None or namespace is None:
            return None  # pragma: no cover - config gate guarantees both
        date_format = cfg.get("date_format")
        if not isinstance(date_format, str) or not date_format:
            return None  # pragma: no cover - config gate guarantees an explicit format
    elif strategy == "group_key":
        # group_key keys on a SIBLING column, not the target; `group_key_config_rejection`
        # already proved group_by is present and `length` is a valid even int in range.
        group_by = cfg.get("group_by")
        if not isinstance(group_by, str) or not group_by:
            return None  # pragma: no cover - config gate guarantees a group_by
        if caps.key_source is None:
            return None  # pragma: no cover - group_key is mask-keyed

    params: OperatorParams
    if positional and strategy == "faker":
        params = positional_faker_params(plan_slice, table=table, column=column)
    else:
        params = resolve_operator_params(
            strategy,
            target=column,
            provider_config=cfg,
            namespace=namespace,
            prepared_categorical=prepared,
        )
    resolved_config: dict[str, Any] = dict(cfg)
    input_schema_column = column
    if isinstance(params, TruncateParams):
        resolved_config["keep"] = params.keep
    elif isinstance(params, GroupKeyParams):
        if not isinstance(params.length, int) or isinstance(params.length, bool):
            return None  # pragma: no cover - config gate guarantees an int length
        input_schema_column = params.group_by
    elif isinstance(params, DateShiftParams):
        # An explicit null bound is rejected at admission (`date_shift_<key>_not_int`).
        if not isinstance(params.min_days, int) or not isinstance(params.max_days, int):
            return None  # pragma: no cover - config gate guarantees int bounds
    key_binding = _key_binding(caps.key_source, params)
    if isinstance(params, FakerParams) and params.positional:
        # The draw keys on the job seed, from the selection namespace: the binding names that.
        if params.selection_namespace is None:
            return None  # pragma: no cover - positional_faker_params always sets it
        key_binding = KeyBinding(key_source="job_seed", namespace=params.selection_namespace)

    # Track A Option 2: resident-Arrow-authoritative typing. `inputs.caller_sources` is the
    # same resident table BOTH the unified-slice and pandas-oracle routes actually mask, so
    # binding this node's input type from it (rather than the profiler's separate
    # descriptor-backed re-read) is what lets a loosely-typed source (csv, fixed_width) admit
    # and stay byte-parity-safe. group_key's input is its SIBLING column (the coordinator
    # feeds `batch.column(group_by)` and admission checks that sibling's type), so the
    # binding carries the sibling's resident type, not the target's.
    input_type = (
        resolve_input_arrow_type(
            table, input_schema_column, inputs.profile, resident_sources=inputs.caller_sources
        )
        or pa.string()
    )
    input_schema = pa.schema([pa.field(input_schema_column, input_type)])

    return ExecutionBinding(
        operator_id=OPERATOR_ID_BY_STRATEGY[strategy],
        operator_reason=f"{_SLICE_ADMITTED_REASON_PREFIX}:{strategy}",
        resolved_config=tuple(sorted(resolved_config.items())),
        input_schema=input_schema,
        output_schema=requirements.output_arrow_schema,
        determinism_family=(
            draw_site_by_id(_POSITIONAL_DRAW_SITE[strategy]).family
            if positional
            else caps.draw_family
        ),
        determinism_version=inputs.plan.seed_protocol_version,
        key_binding=key_binding,
        diagnostic_obligations=requirements.diagnostic_reducers,
        required_prepasses=requirements.required_prepasses,
        batch_estimate=_batch_estimate(table, inputs),
        pool_binding=pool_binding,
        params=params,
    )
