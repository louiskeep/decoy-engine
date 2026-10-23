"""A `when:`-gated write that would mix strings into a numeric column is rejected cleanly.

Faker (no declared output type) and bucketize (range labels, not typed by `masked_output_type`)
cannot be typed by the plan-compile check, so the
gate wrapper checks the realized subset output and raises a coded error before the write,
replacing the raw Arrow error at reconstruction. Numeric output, string columns, ungated columns
and a gate that selects every non-null row (the column becomes uniformly string) are not guarded.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._gated_string_guard import reject_gated_string_on_numeric
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.unit.execution import _c8_iii_c_support as sup
from tests.unit.execution import _multi_table_support as mt

_CODE = "gated_string_output_on_numeric_unsupported"


def _guard(source: pd.Series, produced: list[Any], mask: list[bool]) -> None:
    m = pd.Series(mask, index=source.index)
    reject_gated_string_on_numeric(
        source,
        pd.Series(produced, index=source.index[m]),
        m,
        table="t",
        column="c",
        strategy="faker",
    )


class TestGuardUnit:
    @pytest.mark.parametrize("dtype", ["int64", "Int64", "float64"])
    def test_rejects_partial_gate_with_string_output(self, dtype: str) -> None:
        with pytest.raises(ExecutionError) as exc:
            _guard(pd.Series([1, 2, 3], dtype=dtype), ["Ann", "Bob"], [True, True, False])
        assert exc.value.code == _CODE
        assert "Ann" not in str(exc.value)

    def test_numeric_output_on_numeric_allowed(self) -> None:
        _guard(pd.Series([1, 2, 3], dtype="int64"), [7, 8], [True, True, False])

    def test_string_source_allowed(self) -> None:
        _guard(pd.Series(["x", "y", "z"]), ["Ann", "Bob"], [True, True, False])

    def test_gate_selecting_every_non_null_row_allowed(self) -> None:
        _guard(pd.Series([1.0, None, 3.0]), ["Ann", "Bob"], [True, False, True])

    def test_bool_source_allowed(self) -> None:
        _guard(pd.Series([True, False, True]), ["Ann", "Bob"], [True, True, False])


def _gate_col(strategy: str, **extra: Any) -> dict[str, Any]:
    return {"name": "c", "strategy": strategy, **extra}


def _faker_col(*, when: str | None) -> dict[str, Any]:
    col: dict[str, Any] = {
        "name": "c",
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": True,
        "namespace": "ns_c",
        "pool_size": 40,
    }
    if when is not None:
        col["when"] = when
    return col


def _run(tmp_path: Path, table: pa.Table, col: dict[str, Any]) -> pa.Table:
    cfg, sources = mt.build_job(tmp_path, {"t": ([col], table)})
    plan, graph, _ns, registry = sup.compile_job(cfg)
    out = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=registry,
        relationship_graph=graph,
        namespace_registry=NamespaceRegistry(bindings=()),
    )
    return out.outputs["t"]


class TestGuardEndToEnd:
    def test_partial_gate_string_faker_on_int_column_raises_coded_error(
        self, tmp_path: Path
    ) -> None:
        table = pa.table({"c": pa.array([100001, 200002, 300003, 400004], pa.int64())})
        with pytest.raises(ExecutionError) as exc:
            _run(tmp_path, table, _faker_col(when="c > 150000"))
        assert exc.value.code == _CODE

    def test_gate_selecting_every_row_not_guarded(self, tmp_path: Path) -> None:
        table = pa.table({"c": pa.array([100001, 200002, 300003, 400004], pa.int64())})
        out = _run(tmp_path, table, _faker_col(when="c > 0"))
        assert all(isinstance(v, str) for v in out.column("c").to_pylist())

    def test_gated_faker_on_string_column_not_guarded(self, tmp_path: Path) -> None:
        table = pa.table({"c": pa.array(["a", "b", "c", "d"])})
        out = _run(tmp_path, table, _faker_col(when="c > 'b'"))
        assert out.num_rows == 4

    def test_ungated_faker_on_int_column_not_guarded(self, tmp_path: Path) -> None:
        table = pa.table({"c": pa.array([1, 2, 3, 4], pa.int64())})
        out = _run(tmp_path, table, _faker_col(when=None))
        assert out.num_rows == 4


_INTS = pa.table({"c": pa.array([5, 15, 25, 35], pa.int64())})


class TestGuardIsGeneralised:
    def test_bucketize_partial_gate_on_numeric_raises_coded_error(self, tmp_path: Path) -> None:
        col = _gate_col("bucketize", provider_config={"width": 10}, when="c > 20")
        with pytest.raises(ExecutionError) as exc:
            _run(tmp_path, _INTS, col)
        assert exc.value.code == _CODE
        assert "bucketize" in str(exc.value)
        assert "c > 20" not in str(exc.value)

    def test_numeric_output_partial_gate_on_numeric_not_guarded(self, tmp_path: Path) -> None:
        col = _gate_col("redact", provider_config={"redact_with": 0}, when="c > 20")
        out = _run(tmp_path, _INTS, col)
        assert out.column("c").to_pylist() == [5, 15, 0, 0]

    def test_numeric_category_categorical_partial_gate_not_guarded(self, tmp_path: Path) -> None:
        col = _gate_col(
            "categorical",
            namespace="ns_cat",
            provider_config={"categories": [1, 2]},
            when="c > 20",
        )
        out = _run(tmp_path, _INTS, col)
        assert out.column("c").to_pylist()[:2] == [5, 15]

    def test_full_gate_string_output_replaces_column_without_upcast_warning(
        self, tmp_path: Path
    ) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            out = _run(tmp_path, _INTS, _faker_col(when="c > 0"))
        assert all(isinstance(v, str) for v in out.column("c").to_pylist())


def _run_two(tmp_path: Path, table: pa.Table, cols: list[dict[str, Any]]) -> pa.Table:
    cfg, sources = mt.build_job(tmp_path, {"t": (cols, table)})
    plan, graph, _ns, registry = sup.compile_job(cfg)
    out = PandasExecutionAdapter().run(
        plan,
        sources,
        registry=registry,
        relationship_graph=graph,
        namespace_registry=NamespaceRegistry(bindings=()),
    )
    return out.outputs["t"]


class TestNullablePredicateAndBucketPerturb:
    def test_nullable_predicate_na_rows_count_as_unmasked(self, tmp_path: Path) -> None:
        # `f == 1` over a nullable Int64 is <NA> for the null flag; `.loc[mask]` leaves that row
        # unwritten, so a string faker over the numeric column must still be rejected.
        flags = pd.array([1, None], dtype="Int64")
        table = pa.Table.from_pandas(
            pd.DataFrame({"c": pd.array([10, 20], dtype="int64"), "f": flags}),
            preserve_index=False,
        )
        cols = [_faker_col(when="f == 1"), {"name": "f", "strategy": "passthrough"}]
        with pytest.raises(ExecutionError) as exc:
            _run_two(tmp_path, table, cols)
        assert exc.value.code == _CODE

    def test_guard_unit_normalises_na_mask(self) -> None:
        source = pd.Series([10, 20], dtype="int64")
        mask = pd.Series([True, pd.NA], dtype="boolean")
        with pytest.raises(ExecutionError):
            reject_gated_string_on_numeric(
                source,
                pd.Series(["Ann"], index=[0]),
                mask,
                table="t",
                column="c",
                strategy="faker",
            )

    def test_bucket_perturb_passthrough_config_runs_and_preserves_numbers(
        self, tmp_path: Path
    ) -> None:
        nums = pa.table({"c": pa.array([1, 2, 3, 4], pa.int64())})
        col = _gate_col(
            "bucket_perturb", namespace="ns_bp", provider_config={"bucket": "month"}, when="c > 2"
        )
        assert _run(tmp_path, nums, col).column("c").to_pylist() == [1, 2, 3, 4]

    def test_bucket_perturb_that_emits_strings_is_caught_by_execution_guard(
        self, tmp_path: Path
    ) -> None:
        dates = pa.table({"c": pa.array([20240115, 20240216, 20240317, 20240418], pa.int64())})
        col = _gate_col(
            "bucket_perturb",
            namespace="ns_bp",
            provider_config={"bucket": "month", "date_format": "%Y%m%d"},
            when="c > 20240300",
        )
        with pytest.raises(ExecutionError) as exc:
            _run(tmp_path, dates, col)
        assert exc.value.code == _CODE
