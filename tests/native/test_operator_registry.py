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
