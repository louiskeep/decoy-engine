"""Per-strategy resident-type gates for the unified-slice admission predicate,
split out of `_unified_slice_admission.py` (pure move) to hold its size census.
`_unified_slice_admission.resident_contract_admission` is the one caller."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._operator_registry import OPERATORS

if TYPE_CHECKING:
    from decoy_engine.execution.physical._plan import PhysicalTable

# The fixed, reviewed resident-type domain per slice strategy -- the actual
# set the 4.4 shadow corpus characterizes, not the compiler's coarse profile
# label. A resident type outside its strategy's set declines regardless of
# whether it happens to match the compiled `input_schema` type (e.g. a
# genuinely int64 column bound to redact, which the compiler's own config-
# only gate never rejects). Widening this later is a separately-proven
# slice, never a default. Derived from the operator registry; edit the registry
# (`unified_resident_types`). group_key has no target-type gate (its admission
# checks the sibling column), so it has no entry. Reasons behind individual
# domains: hash also requires null-freedom, enforced separately via the same
# `reject_null_bearing_int` guard the legacy adapter runs; categorical,
# bucket_perturb, date_shift and Pooled Faker key on a STRING source (the
# compiled index kernel's admitted input; Faker's binder also accepts
# large_string, the slice does not, like every sibling index operator).
_ADMITTED_RESIDENT_TYPES: dict[str, frozenset[pa.DataType]] = {
    spec.strategy: spec.unified_resident_types
    for spec in OPERATORS.values()
    if spec.unified_resident_types is not None
}

_POSITIONAL_RESIDENT_TYPES: dict[str, frozenset[pa.DataType]] = {
    spec.strategy: spec.positional_resident_types
    for spec in OPERATORS.values()
    if spec.positional_resident_types is not None
}

_DETERMINISTIC_RESIDENT_TYPES: dict[str, frozenset[pa.DataType]] = {
    spec.strategy: spec.deterministic_resident_types
    for spec in OPERATORS.values()
    if spec.deterministic_resident_types is not None
}


def positional_resident_types(strategy: str, params: Any) -> frozenset[pa.DataType] | None:
    """The wider domain of a bound position-keyed node, or `None` for any other node. The
    variant reads only its source's null mask, so it admits more than the value-keyed one."""
    if getattr(params, "positional", False):
        return _POSITIONAL_RESIDENT_TYPES.get(strategy)
    return None


def deterministic_resident_types(strategy: str, params: Any) -> frozenset[pa.DataType] | None:
    """The wider domain of a bound DETERMINISTIC (non-positional) Faker node (C5c-ii: string plus
    bool/int/uint), or `None` for a positional node or any strategy with no deterministic domain.
    The deterministic variant keys from the source VALUE, so it admits only the families
    `_canonicalize_source` keys without loss (no float, no temporal)."""
    if getattr(params, "positional", False):
        return None
    return _DETERMINISTIC_RESIDENT_TYPES.get(strategy)


def _group_key_sibling_admitted(
    binding: Any, physical_table: PhysicalTable, source: pa.Table
) -> bool:
    """Whether a bound group_key node's `group_by` SIBLING column is admissible:
    resident, an admitted type (v1: `{string, int64, bool}` via
    `group_key_sibling_type_admitted`), matching the binding's input_schema, and
    NOT itself masked by another node in the table (the order-dependence
    decline).

    Runs on the SIBLING, not the target: the oracle keys on `df[group_by]` at
    group_key's execution point, so native parity holds only when
    `batch.column(group_by)` (the original source value) equals what the oracle
    reads -- which requires the sibling to be resident, safe-typed, and left
    UNMASKED (a passthrough node). A masked sibling means the pandas adapter
    would have mutated that column in the frame before group_key reads it, so
    native declines to the oracle (v1 does not model the effective-input
    dependency)."""
    from decoy_engine.execution.native._operator_config_rejections import (
        group_key_sibling_type_admitted,
    )

    group_by = binding.group_key_sibling
    if not isinstance(group_by, str) or not group_by:
        return False
    if group_by not in source.schema.names:
        # A non-resident sibling declines cleanly (never a raising KeyError).
        return False
    sibling_type = source.schema.field(group_by).type
    if not group_key_sibling_type_admitted(sibling_type):
        # float / decimal / dictionary (and any unlisted type) decline.
        return False
    # Track A Option 2 guard reconciliation: `execution_binding_for_slice_node`
    # now builds `input_schema` from this SAME resident sibling type (not a
    # profile re-read), so comparing the two types here would always be true --
    # a tautology, not a check. What is still load-bearing is the STRUCTURAL
    # shape (exactly one field, named `group_by`): a binding of any other shape
    # would be a compiler bug, not a resident-type question.
    input_schema = binding.input_schema
    if len(input_schema) != 1 or input_schema.names != [group_by]:
        return False
    # Order-dependence: the sibling must be an UNMASKED (passthrough) node. Any
    # other node masking it means the oracle would key on the mutated value.
    for other in physical_table.nodes:
        if group_by in other.columns and other.strategy != "passthrough":
            return False
    return True
