"""R1b: the marker predicates on `ExecutionBinding` keep their field-sensitive meaning.

The per-operator fields became one `params` object, but a half-built binding must still answer
as it did when each field stood alone: a bucket_perturb binding is a bucket_perturb node
because its `bucket` is set, whatever its date_format holds. Each case below is a partial
binding the old fields could express, plus `params=None`.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._categorical_prepared import PreparedCategorical
from decoy_engine.execution.native._operator_params import (
    BucketPerturbParams,
    CategoricalParams,
    DateShiftParams,
    FakerParams,
    GroupKeyParams,
    HashParams,
    OperatorParams,
    RedactParams,
)
from decoy_engine.execution.physical._plan import ExecutionBinding, PoolBinding

_FMT = "%Y-%m-%d"
_SCHEMA = pa.schema([pa.field("c", pa.string())])


def _binding(params: OperatorParams | None, **extra: Any) -> ExecutionBinding:
    return ExecutionBinding(
        operator_id="op",
        operator_reason="test",
        resolved_config=(),
        input_schema=_SCHEMA,
        output_schema=_SCHEMA,
        determinism_family=None,
        determinism_version=1,
        key_binding=None,
        diagnostic_obligations=(),
        required_prepasses=(),
        batch_estimate=None,
        params=params,
        **extra,
    )


_BUCKET = BucketPerturbParams("month", _FMT, "ns")
_DATE_SHIFT = DateShiftParams(_FMT, -365, 365, "ns")
_CATEGORICAL = CategoricalParams(PreparedCategorical(("a", "b"), None), "ns")
_POSITIONAL = CategoricalParams(PreparedCategorical(("a", "b"), None, positional=True), "ns")
_GROUP_KEY = GroupKeyParams("g", 16, "", "group_key/c")

_NEEDS_KERNEL: list[tuple[str, OperatorParams | None, bool]] = [
    ("no_params", None, False),
    ("redact", RedactParams("X"), False),
    ("hash", HashParams("ns", None), False),
    ("categorical", _CATEGORICAL, True),
    ("categorical_positional", _POSITIONAL, False),
    ("bucket_full", _BUCKET, True),
    ("bucket_without_date_format", dataclasses.replace(_BUCKET, date_format=None), True),
    ("bucket_without_bucket", dataclasses.replace(_BUCKET, bucket=None), False),
    ("date_shift_full", _DATE_SHIFT, True),
    ("date_shift_without_date_format", dataclasses.replace(_DATE_SHIFT, date_format=None), False),
    ("date_shift_without_min_days", dataclasses.replace(_DATE_SHIFT, min_days=None), True),
    ("group_key", _GROUP_KEY, False),
]


@pytest.mark.parametrize(
    "params, expected", [pytest.param(p, e, id=i) for i, p, e in _NEEDS_KERNEL]
)
def test_needs_index_kernel_is_field_sensitive(
    params: OperatorParams | None, expected: bool
) -> None:
    assert _binding(params).needs_index_kernel is expected


def test_a_pool_binding_alone_needs_the_index_kernel() -> None:
    binding = _binding(FakerParams("ns"), pool_binding=PoolBinding("person_first_name", 20))
    assert binding.needs_index_kernel is True
    assert _binding(FakerParams("ns")).needs_index_kernel is False


_SIBLING: list[tuple[str, OperatorParams | None, str | None]] = [
    ("no_params", None, None),
    ("group_key", _GROUP_KEY, "g"),
    ("group_key_without_group_by", dataclasses.replace(_GROUP_KEY, group_by=None), None),
    ("group_key_without_length", dataclasses.replace(_GROUP_KEY, length=None), "g"),
    ("bucket", _BUCKET, None),
    ("categorical", _CATEGORICAL, None),
]


@pytest.mark.parametrize("params, expected", [pytest.param(p, e, id=i) for i, p, e in _SIBLING])
def test_group_key_sibling_is_the_bound_group_by_or_none(
    params: OperatorParams | None, expected: str | None
) -> None:
    assert _binding(params).group_key_sibling == expected


_DETERMINISTIC: list[tuple[str, OperatorParams | None, bool]] = [
    ("no_params", None, False),
    ("deterministic", _CATEGORICAL, True),
    ("positional", _POSITIONAL, False),
    ("bucket", _BUCKET, False),
]


@pytest.mark.parametrize(
    "params, expected", [pytest.param(p, e, id=i) for i, p, e in _DETERMINISTIC]
)
def test_categorical_deterministic_means_a_source_keyed_categorical(
    params: OperatorParams | None, expected: bool
) -> None:
    assert _binding(params).categorical_deterministic is expected
