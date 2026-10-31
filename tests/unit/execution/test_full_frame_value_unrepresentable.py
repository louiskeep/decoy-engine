"""§PANDAS-RANGE-ERROR: the full-frame pandas oracle converts every source
column to pandas up front. A value pandas cannot hold -- a date beyond year
9999, a sub-microsecond time64 -- used to crash there with an uncoded error
naming no column. `run_full_frame_route` now attributes it to its source column
and raises a coded `ExecutionError(code="full_frame_value_unrepresentable")`.

The attribution is error-path only: a run whose values all fit pandas is
byte-identical to before. The native (Rust) route keeps columns in Arrow and
never hits this limit, which is why the default path is unaffected.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution._errors import ExecutionError

_EV = "full-frame-range-test"


def _cfg(tmp_path, columns: list[dict]) -> dict:
    # The config source path must exist; the real data is supplied in-memory via
    # `sources=` (these temporal values do not round-trip through CSV), so the
    # stub only needs the right column names.
    names = ",".join(c["name"] for c in columns)
    (tmp_path / "t.csv").write_text(names + "\n")
    return PipelineConfig.model_validate(
        {
            "version": 2,
            "global_settings": {"seed": 42},
            "sources": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "t.csv")}},
            "tables": [{"name": "t", "columns": columns}],
            "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
        }
    ).model_dump()


def _run(tmp_path, columns: list[dict], src: pa.Table):
    return run_pipeline(_cfg(tmp_path, columns), sources={"t": src}, engine_version=_EV)


# A masked sibling forces the oracle to run (a table with nothing to mask never
# converts to pandas); the overflow column rides along as passthrough.
_MASK_SIBLING = {"name": "name", "strategy": "hash", "namespace": "n"}


def test_far_date_raises_coded_error_naming_the_column(tmp_path) -> None:
    # date32 at ~year 12921 (+4,000,000 days): valid in Arrow, overflows pandas.
    src = pa.table({"d": pa.array([4_000_000, 4_000_001], pa.date32()), "name": ["alice", "bob"]})
    with pytest.raises(ExecutionError) as exc:
        _run(tmp_path, [{"name": "d", "strategy": "passthrough"}, _MASK_SIBLING], src)
    assert exc.value.code == "full_frame_value_unrepresentable"
    assert "'d'" in str(exc.value)  # the offending column is named
    assert "'t'" in str(exc.value)  # and its table


def test_sub_microsecond_time_raises_coded_error(tmp_path) -> None:
    # time64[ns] with a non-zero nanosecond part: pandas cannot represent it.
    src = pa.table({"tod": pa.array([1, 2], pa.time64("ns")), "name": ["alice", "bob"]})
    with pytest.raises(ExecutionError) as exc:
        _run(tmp_path, [{"name": "tod", "strategy": "passthrough"}, _MASK_SIBLING], src)
    assert exc.value.code == "full_frame_value_unrepresentable"
    assert "'tod'" in str(exc.value)


def test_holdable_values_run_unchanged(tmp_path) -> None:
    # A date well inside pandas' range plus a sub-microsecond-free time: no false
    # positive, the run completes and the passthrough values are preserved.
    src = pa.table(
        {
            "d": pa.array([20_000, 20_001], pa.date32()),  # ~year 2024
            "tod": pa.array([1_000, 2_000], pa.time64("us")),  # whole microseconds
            "name": ["alice", "bob"],
        }
    )
    out = _run(
        tmp_path,
        [
            {"name": "d", "strategy": "passthrough"},
            {"name": "tod", "strategy": "passthrough"},
            _MASK_SIBLING,
        ],
        src,
    ).outputs["t"]
    assert out.num_rows == 2
    # passthrough columns are byte-identical to the source
    assert out.column("d").to_pylist() == src.column("d").to_pylist()
    assert out.column("tod").to_pylist() == src.column("tod").to_pylist()
