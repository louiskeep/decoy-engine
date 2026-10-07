"""Jobs that succeed before the nullable-int fix must produce the same output after it.

Each scenario's result (tables, schema, pandas metadata, warnings, row errors and quality
metrics) is recorded in `c5c_a_unchanged_jobs.json`, captured from the tree before the fix.
Regenerate only on purpose: `C5C_A_WRITE_GOLDENS=1 pytest <this file>`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from tests.unit.execution import _c5c_a_support as sup
from tests.unit.execution.test_c5c_a_gate_and_writers import composite_before_faker_job

pytestmark = pytest.mark.filterwarnings("ignore")

_GOLDEN = Path(__file__).parent / "c5c_a_unchanged_jobs.json"


def _snapshot(result: Any) -> str:
    tables = {
        name: {
            "schema": str(tbl.schema),
            "metadata": {k.decode(): v.decode() for k, v in (tbl.schema.metadata or {}).items()},
            "rows": [repr(r) for r in tbl.to_pylist()],
        }
        for name, tbl in sorted(result.outputs.items())
    }
    doc = {
        "tables": tables,
        "warnings": [repr(w) for w in result.warnings],
        "row_errors": [repr(r) for r in result.row_errors],
        "quality_metrics": result.quality_metrics,
    }
    return json.dumps(doc, sort_keys=True, default=repr)


_LIBRARY_VERSION_KEYS = ("creator", "pandas_version")


def _version_free(snapshot: str) -> str:
    """The snapshot minus library version stamps, so a pyarrow or pandas patch release is not a diff."""
    doc = json.loads(snapshot)
    for table in doc["tables"].values():
        raw = table["metadata"].get("pandas")
        if raw is not None:
            meta = json.loads(raw)
            for key in _LIBRARY_VERSION_KEYS:
                meta.pop(key, None)
            table["metadata"]["pandas"] = json.dumps(meta, sort_keys=True)
    return json.dumps(doc, sort_keys=True)


def _faker(**kw: Any) -> Any:
    return sup.faker_seed(**kw)


def _s_null_free_int() -> Any:
    table = pa.table({"n": pa.array([1, 2, 3, 2], type=pa.int64())})
    return sup.run(sup.plan_of({"n": _faker()}), table)


def _flags_table(n: pa.Array, **extra: Any) -> pa.Table:
    cols: dict[str, Any] = {"n": n, "f": pa.array([1, 0, 1, 0])}
    cols.update(extra)
    return pa.table(cols)


_NULLABLE = pa.array([1, None, 3, 2], type=pa.int64())


def _s_zero_row_gate() -> Any:
    plan = sup.plan_of({"n": _faker(when="f == 99"), "f": sup.seed_of("passthrough")})
    return sup.run(plan, _flags_table(_NULLABLE))


def _s_predicate_reads_faker_column() -> Any:
    plan = sup.plan_of(
        {
            "n": _faker(when="f == 99"),
            "f": sup.seed_of("passthrough"),
            "s": sup.seed_of("redact", when="n > 1"),
        }
    )
    return sup.run(plan, _flags_table(_NULLABLE, s=pa.array(["a", "b", "c", "d"])))


def _s_derived_reads_faker_column() -> Any:
    plan = sup.plan_of(
        {
            "n": _faker(when="f == 99"),
            "f": sup.seed_of("passthrough"),
            "d": sup.seed_of("derived", expression="n + 1"),
        }
    )
    return sup.run(plan, _flags_table(_NULLABLE, d=pa.array([0.0, 0.0, 0.0, 0.0])))


def _s_group_by_sibling() -> Any:
    plan = sup.plan_of(
        {
            "n": _faker(),
            "g": sup.seed_of("group_key", group_by="n", length=8),
        }
    )
    table = pa.table({"n": _NULLABLE, "g": pa.array(["a", "b", "c", "d"])})
    return sup.run(plan, table)


def _s_int64_metadata_source() -> Any:
    df = pd.DataFrame({"n": pd.array([1, None, 3, 2], dtype="Int64")})
    table = pa.Table.from_pandas(df, preserve_index=False)
    return sup.run(sup.plan_of({"n": _faker()}), table)


def _s_all_null() -> Any:
    table = pa.table({"n": pa.array([None, None], type=pa.int64())})
    return sup.run(sup.plan_of({"n": _faker()}), table)


def _s_empty() -> Any:
    table = pa.table({"n": pa.array([], type=pa.int64())})
    return sup.run(sup.plan_of({"n": _faker()}), table)


def _s_redact_under_na_predicate() -> Any:
    flags = pd.array([1, None, 1, 0], dtype="Int64")
    table = pa.Table.from_pandas(
        pd.DataFrame({"f": flags, "s": ["a", "b", "c", "d"]}), preserve_index=False
    )
    plan = sup.plan_of({"s": sup.seed_of("redact", when="f == 1"), "f": sup.seed_of("passthrough")})
    return sup.run(plan, table)


def _s_gate_row_error_remap() -> Any:
    flags = pd.array([1, 1, 0, 1], dtype="Int64")
    table = pa.Table.from_pandas(
        pd.DataFrame({"f": flags, "a": ["10", "bad", "30", "40"]}), preserve_index=False
    )
    plan = sup.plan_of(
        {
            "a": sup.seed_of("bucketize", when="f == 1", width=10),
            "f": sup.seed_of("passthrough"),
        }
    )
    return sup.run(plan, table)


def _s_earlier_composite_writer() -> Any:
    return composite_before_faker_job([1, None, 3])


_SCENARIOS: dict[str, Callable[[], Any]] = {
    "earlier_composite_writer": _s_earlier_composite_writer,
    "null_free_int": _s_null_free_int,
    "zero_row_gate": _s_zero_row_gate,
    "predicate_reads_faker_column": _s_predicate_reads_faker_column,
    "derived_reads_faker_column": _s_derived_reads_faker_column,
    "group_by_sibling": _s_group_by_sibling,
    "int64_metadata_source": _s_int64_metadata_source,
    "all_null": _s_all_null,
    "empty": _s_empty,
    "redact_under_na_predicate": _s_redact_under_na_predicate,
    "gate_row_error_remap": _s_gate_row_error_remap,
}


def _load() -> dict[str, str]:
    return json.loads(_GOLDEN.read_text()) if _GOLDEN.exists() else {}


@pytest.mark.skipif(not os.environ.get("C5C_A_WRITE_GOLDENS"), reason="golden writer")
def test_write_goldens() -> None:
    _GOLDEN.write_text(
        json.dumps({k: _snapshot(fn()) for k, fn in _SCENARIOS.items()}, indent=1, sort_keys=True)
        + "\n"
    )


@pytest.mark.parametrize("name", sorted(_SCENARIOS))
def test_job_output_is_unchanged(name: str) -> None:
    golden = _load()
    assert name in golden, "golden missing; regenerate from the pre-fix tree"
    assert _version_free(_snapshot(_SCENARIOS[name]())) == _version_free(golden[name])
