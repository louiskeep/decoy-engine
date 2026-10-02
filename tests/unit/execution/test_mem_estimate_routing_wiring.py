"""The routing entry points must hand the pandas-nullable column set to the spec builder.

`source_nullable_columns` is covered on its own elsewhere; these tests pin the two
call sites that pass it as `masked_columns` (`_mask_specs` and the transforms
admission `_specs`). Without it a pandas-written no-null `Int64` column prices as
plain `int64` (8 bytes) instead of the 9-byte masked class.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _mem_estimate, _transforms_admission
from decoy_engine.execution import _pipeline_routing_signals as signals
from decoy_engine.execution._mem_estimate import _column_bytes
from decoy_engine.profile._readers import LazySource
from decoy_engine.profile._types import ColumnProfile, TableProfile

pd = pytest.importorskip("pandas")

_ROWS = 4


def _frame() -> Any:
    return pd.DataFrame({"i64": pd.array([1, 2, 3, 4], "Int64")})


def _profile_table() -> TableProfile:
    col = ColumnProfile(
        name="i64",
        dtype="int64",
        row_count=_ROWS,
        null_count=0,
        distinct_count=_ROWS,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )
    return TableProfile(name="t", row_count=_ROWS, columns=(col,))


class _FakeProfile:
    def __init__(self, tables: tuple[TableProfile, ...]) -> None:
        self.relationships: tuple[Any, ...] = ()
        self.tables = tables


def _resident() -> pa.Table:
    table = pa.Table.from_pandas(_frame(), preserve_index=False)
    assert table.column("i64").null_count == 0
    return table


def _lazy(tmp_path: Path) -> LazySource:
    path = tmp_path / "t.parquet"
    _frame().to_parquet(path, index=False)
    return LazySource(path)


def _assert_masked_int64(specs: tuple[Any, ...]) -> None:
    (spec,) = specs
    (col,) = spec.columns
    assert col.dtype == "Int64"
    assert _column_bytes(1, col) == 9


class _CapturedError(Exception):
    pass


def test_mask_specs_prices_resident_pandas_int64_masked() -> None:
    _assert_masked_int64(signals._mask_specs([_profile_table()], {"t": _resident()}))


def test_mask_specs_prices_lazy_pandas_int64_masked(tmp_path: Path) -> None:
    _assert_masked_int64(signals._mask_specs([_profile_table()], {"t": _lazy(tmp_path)}))


@pytest.mark.parametrize("kind", ["resident", "lazy"])
def test_byte_estimate_full_frame_fits_receives_masked_int64(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[Any, ...]] = []

    def fake_fits(specs: Any, *args: Any, **kwargs: Any) -> bool:
        seen.append(tuple(specs))
        return True

    monkeypatch.setattr(_mem_estimate, "fits", fake_fits)
    source = _resident() if kind == "resident" else _lazy(tmp_path)
    signals.byte_estimate_full_frame_fits(
        _FakeProfile((_profile_table(),)),
        caller_sources={"t": source},
        table_kinds={"t": "mask"},
        budget_bytes=10**9,
    )
    (specs,) = seen
    _assert_masked_int64(specs)


def test_resolve_probe_recovery_receives_masked_int64(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []

    def fake_raw(specs: Any) -> Any:
        seen.append(tuple(specs))
        raise _CapturedError

    monkeypatch.setattr(_mem_estimate, "raw_data_bytes", fake_raw)
    with pytest.raises(_CapturedError):
        signals.resolve_probe_recovery(
            True,
            True,
            _FakeProfile((_profile_table(),)),
            {"t": _resident()},
            {"t": "mask"},
            10**9,
            False,
            config={},
            engine_version="test",
        )
    (specs,) = seen
    _assert_masked_int64(specs)


def test_transforms_admission_specs_price_resident_pandas_int64_masked() -> None:
    specs = _transforms_admission._specs([_profile_table()], {"t": _resident()}, frozenset())
    _assert_masked_int64(specs)


def test_transforms_admission_specs_price_lazy_pandas_int64_masked(tmp_path: Path) -> None:
    specs = _transforms_admission._specs([_profile_table()], {"t": _lazy(tmp_path)}, frozenset())
    _assert_masked_int64(specs)
