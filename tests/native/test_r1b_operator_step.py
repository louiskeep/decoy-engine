"""R1b: the shared kernel step's contract.

`run_kernel_step` is what both routes call. It returns the output, whether a compiled kernel
ran (None for an unkeyed transform that makes no such claim), and any date_shift format-error
positions. It does not catch the missing-companion error, touch evidence or know a route.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._categorical_prepared import PreparedCategorical
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._operator_params import (
    BucketPerturbParams,
    CategoricalParams,
    DateShiftParams,
    FakerParams,
    GroupKeyParams,
    HashParams,
    OperatorParams,
    PassthroughParams,
    RedactParams,
    TruncateParams,
)
from decoy_engine.execution.native._operator_step import StepResult, run_kernel_step
from tests.native._r1b_support import (
    INDEX_KERNEL,
    MASK_KEY,
    SIBLING,
    TARGET,
    real_kernels,
    recording_kernels,
    table_for,
)
from tests.native.test_r1b_characterization import _RAN, _SOURCES, NEEDS_COMPANION

_FMT = "%Y-%m-%d"
_SOURCE = pa.array(["a", None, "c"], pa.string())
_PREPARED = PreparedCategorical(("x", "y"), None)
_POSITIONAL = PreparedCategorical(("x", "y"), None, positional=True)

_KEYED_RAN: list[tuple[str, OperatorParams, bool | None]] = [
    ("passthrough", PassthroughParams(), None),
    ("redact", RedactParams("REDACTED"), None),
    ("truncate", TruncateParams(2, "head", None), None),
    ("hash", HashParams("ns", None), True),
    ("faker", FakerParams("ns"), True),
    ("categorical", CategoricalParams(_PREPARED, "ns"), True),
    ("categorical_positional", CategoricalParams(_POSITIONAL, "ns"), True),
]


def _step(params: OperatorParams, source: Any = _SOURCE, **extra: Any) -> StepResult:
    return run_kernel_step(
        params,
        source,
        mask_key=MASK_KEY,
        native_threads=1,
        index_kernel=extra.pop("index_kernel", INDEX_KERNEL),
        **extra,
    )


@pytest.mark.parametrize("params, expected", [pytest.param(p, e, id=i) for i, p, e in _KEYED_RAN])
def test_ran_is_none_for_unkeyed_transforms_and_true_for_the_keyed_operators(
    params: OperatorParams, expected: bool | None
) -> None:
    with recording_kernels() as calls:
        result = _step(params, pool=object())
    assert len(calls) == 1
    assert result.ran is expected
    assert result.format_error_positions == ()


def test_a_positional_categorical_on_zero_rows_makes_no_kernel_call() -> None:
    empty = pa.array([], pa.string())
    with recording_kernels() as calls:
        result = _step(CategoricalParams(_POSITIONAL, "ns"), empty, row_offset=7)
    assert calls == []
    assert result.ran is False
    assert result.out.type == pa.string()
    assert len(result.out) == 0


def test_a_deterministic_categorical_on_zero_rows_still_calls_the_kernel() -> None:
    empty = pa.array([], pa.string())
    with recording_kernels() as calls:
        result = _step(CategoricalParams(_PREPARED, "ns"), empty)
    assert [c.kernel for c in calls] == ["native_categorical"]
    assert result.ran is True


def test_the_positional_categorical_forwards_the_row_offset() -> None:
    with recording_kernels() as calls:
        _step(CategoricalParams(_POSITIONAL, "ns"), row_offset=41)
    assert [c.kernel for c in calls] == ["native_categorical_positional"]
    assert calls[0].kwargs["row_offset"] == 41


def test_the_step_does_not_catch_a_missing_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.execution.native import _operator_step

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise CryptoExtensionUnavailableError("companion missing in this test")

    monkeypatch.setattr(_operator_step, "native_keyed_hash", _raise)
    with pytest.raises(CryptoExtensionUnavailableError):
        _step(HashParams("ns", None))


_PARSE_PARAMS: dict[str, OperatorParams] = {
    "bucket_perturb": BucketPerturbParams("month", _FMT, "ns"),
    "date_shift": DateShiftParams(_FMT, -365, 365, "ns"),
    "group_key": GroupKeyParams(SIBLING, 16, "", f"group_key/{TARGET}"),
}


@NEEDS_COMPANION
@pytest.mark.parametrize(
    "operator, source",
    [pytest.param(op, s, id=f"{op}-{s}") for op in _PARSE_PARAMS for s in _SOURCES],
)
def test_ran_follows_each_kernels_own_derive_calls(operator: str, source: str) -> None:
    values = _SOURCES[source]
    index_kernel, raw_hex_kernel = real_kernels()
    sibling = table_for(operator, values).select([SIBLING]) if operator == "group_key" else None
    result = run_kernel_step(
        _PARSE_PARAMS[operator],
        pa.array(values, pa.string()),
        mask_key=MASK_KEY,
        native_threads=1,
        index_kernel=index_kernel,
        raw_hex_kernel=raw_hex_kernel,
        sibling=sibling,
    )
    assert result.ran is _RAN[operator][source]


@NEEDS_COMPANION
def test_date_shift_reports_the_batch_local_positions_of_unparseable_values() -> None:
    index_kernel, _ = real_kernels()
    result = run_kernel_step(
        _PARSE_PARAMS["date_shift"],
        pa.array(["2024-01-05", "bad", None, "worse"], pa.string()),
        mask_key=MASK_KEY,
        native_threads=1,
        index_kernel=index_kernel,
    )
    assert result.format_error_positions == (1, 3)
