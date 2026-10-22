"""A `when:`-gated faker column that would write strings into a numeric column is rejected cleanly.

A faker provider has no declared output type, so the plan-compile check cannot type it; the
gate wrapper checks the realized subset output and raises a coded error before the write,
replacing the raw Arrow error at reconstruction. Numeric output, string columns, ungated columns
and a gate that selects every non-null row (the column becomes uniformly string) are not guarded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._gated_faker_guard import reject_gated_faker_string_on_numeric
from decoy_engine.relationships._namespace import NamespaceRegistry
from tests.unit.execution import _c8_iii_c_support as sup
from tests.unit.execution import _multi_table_support as mt

_CODE = "gated_faker_string_on_numeric_unsupported"


def _guard(source: pd.Series, produced: list[Any], mask: list[bool]) -> None:
    m = pd.Series(mask, index=source.index)
    reject_gated_faker_string_on_numeric(
        source, pd.Series(produced, index=source.index[m]), m, table="t", column="c"
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
