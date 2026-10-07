"""The operator registry is the one place per-operator facts live (R1).

Totality and consistency checks here replace the old hand-synchronized parallel tables.
The snapshot file `test_operator_tables_snapshot.py` pins each derived view's value.
"""

from __future__ import annotations

import dataclasses

import pyarrow as pa
import pytest

from decoy_engine.execution._operator_registry import OPERATORS, OperatorSpec, operator_spec
from decoy_engine.execution.native._chunked_evidence import (
    ARROW_PYTHON,
    RUST_COMPANION,
    RUST_POOL_SELECT,
)
from decoy_engine.execution.physical._shadow_bindings import SLICE_STRATEGIES


def test_registry_keys_equal_slice_strategies() -> None:
    assert set(OPERATORS) == set(SLICE_STRATEGIES)
    assert all(spec.strategy == key for key, spec in OPERATORS.items())


def test_operator_ids_are_unique() -> None:
    ids = [spec.operator_id for spec in OPERATORS.values()]
    assert len(ids) == len(set(ids))


def test_backend_and_kernel_are_consistent() -> None:
    for spec in OPERATORS.values():
        assert (spec.planned_backend == ARROW_PYTHON) == (spec.required_kernel is None), spec
        assert (spec.planned_backend == RUST_POOL_SELECT) == (spec.shape == "pool"), spec
        assert spec.planned_backend in {ARROW_PYTHON, RUST_COMPANION, RUST_POOL_SELECT}


def test_unknown_strategy_raises_key_error() -> None:
    with pytest.raises(KeyError):
        operator_spec("nope")
    assert operator_spec("hash") is OPERATORS["hash"]


def test_spec_is_frozen() -> None:
    spec = operator_spec("hash")
    assert isinstance(spec, OperatorSpec)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.operator_id = "x"  # type: ignore[misc]


def test_group_key_has_no_target_resident_types_and_faker_has_the_allowlist() -> None:
    assert OPERATORS["group_key"].unified_resident_types is None
    assert OPERATORS["faker"].provider_allowlist == frozenset(
        {"person_first_name", "person_last_name"}
    )
    assert OPERATORS["passthrough"].unified_resident_types == frozenset(
        {pa.string(), pa.int64(), pa.bool_()}
    )
    assert all(spec.provider_allowlist is None for key, spec in OPERATORS.items() if key != "faker")


def test_capability_diagnostics_are_routed_for_every_slice_operator() -> None:
    """A slice operator whose capabilities declare a diagnostic the unified coordinator
    does not route must be a conscious decision (add it to `routed_diagnostics` with a
    real routing path, or keep the operator declined), never an automatic admission."""
    from decoy_engine.execution.native._capabilities import capabilities_for
    from decoy_engine.execution.native._requirements import _diagnostic_reducers

    for spec in OPERATORS.values():
        declared = frozenset(_diagnostic_reducers(capabilities_for(spec.strategy)))
        assert declared <= spec.routed_diagnostics, (spec.strategy, declared)


def test_routed_obligations_are_policy_not_derived_from_capabilities() -> None:
    """The unified slice's routed-diagnostics table is coordinator policy read from each
    descriptor's `routed_diagnostics`. Deriving it from the capability reducers would make
    the admission gate `obligations <= routed` pass for every operator, silently admitting
    a future operator whose diagnostics the coordinator never routes."""
    import ast
    import inspect

    from decoy_engine.execution import _unified_slice_admission as adm

    expected = {
        s.operator_id: s.routed_diagnostics for s in OPERATORS.values() if s.routed_diagnostics
    }
    assert expected == adm._ROUTED_DIAGNOSTIC_OBLIGATIONS
    names = {n.id for n in ast.walk(ast.parse(inspect.getsource(adm))) if isinstance(n, ast.Name)}
    assert "_diagnostic_reducers" not in names


def test_group_key_sibling_types_exclude_floats_even_if_passthrough_widens() -> None:
    import pyarrow as pa

    from decoy_engine.execution.native._operator_config_rejections import group_key_sibling_types

    widened = frozenset({pa.string(), pa.int64(), pa.bool_(), pa.float64(), pa.decimal128(10, 2)})
    assert group_key_sibling_types(widened) == frozenset({pa.string(), pa.int64(), pa.bool_()})
    assert group_key_sibling_types(None) == frozenset()


def test_only_faker_declares_a_positional_resident_domain() -> None:
    from decoy_engine.execution._operator_registry import POSITIONAL_FAKER_SOURCE_TYPES

    numeric = frozenset(
        {
            pa.int8(),
            pa.int16(),
            pa.int32(),
            pa.int64(),
            pa.uint8(),
            pa.uint16(),
            pa.uint32(),
            pa.uint64(),
            pa.bool_(),
            pa.float32(),
            pa.float64(),
        }
    )
    assert numeric == POSITIONAL_FAKER_SOURCE_TYPES
    assert OPERATORS["faker"].positional_resident_types == numeric | {pa.string()}
    assert OPERATORS["faker"].unified_resident_types == frozenset({pa.string()})
    assert all(
        spec.positional_resident_types is None for key, spec in OPERATORS.items() if key != "faker"
    )
