"""AT-21: `fk_preservation` findings derive from the declared keys' facts (4.8).

Introduced orphans `fail`, orphans all inherited from the source `warning`, none `info`; a key
that carries only warning codes derives `warning`, and only a key that blocks without counts
derives `error`. Severity never depends on an orphan rate. All values are made up.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig, RelationshipConfig
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._pipeline_finalize import compute_post_scan_integrity
from decoy_engine.verification.postmask import (
    SCHEMA_VERSION,
    TableBatches,
    check_keys,
    from_table,
    post_scan_verdict,
    run_post_mask,
)

CANARY = "CANARY-77e2"


def rel_dict(parent: str, pcols: Sequence[str], *children: tuple[str, Sequence[str]]) -> Any:
    return {
        "parent": {"table": parent, "columns": list(pcols)},
        "children": [{"table": t, "columns": list(c)} for t, c in children],
        "orphan_policy": "preserve",
        "namespace": "ns",
    }


REL = rel_dict("p", ["id"], ("c", ["pid"]))
CONFIG = {"relationships": [REL]}


def frames(**spec: dict[str, list[Any]]) -> dict[str, pd.DataFrame]:
    return {name: pd.DataFrame(cols) for name, cols in spec.items()}


def fk(source: Any, output: Any, config: Any = CONFIG, **kw: Any) -> list[dict[str, Any]]:
    return run_post_mask(source, output, config=config, **kw)["fk_preservation"]


def keys_of(tmp_path: Path, source: dict[str, TableBatches], output: Any) -> Any:
    rels = [RelationshipConfig.model_validate(REL)]
    return check_keys(rels, source, output, scratch=tmp_path / "s", memory_limit="64MB")


def batches(**spec: dict[str, list[Any]]) -> dict[str, TableBatches]:
    return {name: from_table(name, pa.table(cols)) for name, cols in spec.items()}


def failing(table: TableBatches) -> TableBatches:
    def read(columns: Sequence[str], batch_rows: int) -> Iterator[pa.RecordBatch]:
        raise RuntimeError(f"cannot read {CANARY}")

    return replace(table, read=read)


def test_orphans_the_run_introduced_fail() -> None:
    (f,) = fk(
        frames(p={"id": [1, 2]}, c={"pid": [1, 2, 2]}),
        frames(p={"id": [1, 2]}, c={"pid": [1, 2, 9]}),
    )
    assert (f["severity"], f["orphan_count"], f["total_child_rows"]) == ("fail", 1, 3)
    assert f["source_orphan_count"] == 0


def test_one_introduced_orphan_in_two_thousand_rows_still_fails() -> None:
    # The old 1% fail threshold would call this `info`: severity no longer reads a rate.
    clean = [1] * 2000
    (f,) = fk(
        frames(p={"id": [1]}, c={"pid": clean}), frames(p={"id": [1]}, c={"pid": [*clean[:-1], 9]})
    )
    assert (f["severity"], f["orphan_count"], f["total_child_rows"]) == ("fail", 1, 2000)
    assert f["orphan_rate"] == 1 / 2000


def test_orphans_all_inherited_from_the_source_warn() -> None:
    data = frames(p={"id": [1, 2]}, c={"pid": [1, 9, 9, 9]})
    (f,) = fk(data, data)
    assert (f["severity"], f["orphan_count"], f["source_orphan_count"]) == ("warning", 3, 3)


def test_no_orphan_is_info() -> None:
    # Nullable, so the null does not turn the key into floats.
    data = frames(p={"id": [1, 2]}, c={"pid": pd.array([1, 2, None], dtype="Int64")})
    (f,) = fk(data, data)
    assert (f["severity"], f["orphan_count"], f["total_child_rows"]) == ("info", 0, 2)


def test_a_mismatch_over_a_mismatched_source_warns(tmp_path: Path) -> None:
    data = batches(p={"id": [1]}, c={"pid": ["1"]})
    keys = keys_of(tmp_path, data, data)
    (f,) = fk({}, {}, keys=keys)
    assert f["severity"] == "warning"


def test_a_lone_source_side_check_error_warns(tmp_path: Path) -> None:
    source = batches(p={"id": [1]}, c={"pid": [1]})
    source["c"] = failing(source["c"])
    keys = keys_of(tmp_path, source, batches(p={"id": [1]}, c={"pid": [1]}))
    (f,) = fk({}, {}, keys=keys)
    assert (f["severity"], f["source_orphan_count"]) == ("warning", None)
    assert CANARY not in repr(f)


@pytest.mark.parametrize("case", ["error", "key_column_missing", "key_type_mismatch"])
def test_only_a_key_that_blocks_without_counts_derives_error(tmp_path: Path, case: str) -> None:
    source = batches(p={"id": [1]}, c={"pid": [1]})
    output = {
        "error": batches(p={"id": [1]}, c={"pid": [1]}),
        "key_column_missing": batches(p={"id": [1]}, c={"other": [1]}),
        "key_type_mismatch": batches(p={"id": [1]}, c={"pid": ["1"]}),
    }[case]
    if case == "error":
        output["c"] = failing(output["c"])
    keys = keys_of(tmp_path, source, output)
    assert {k.status for k in keys if k.kind == "foreign"} == {case}
    report = run_post_mask({}, {}, config=CONFIG, keys=keys)
    (f,) = report["fk_preservation"]
    assert (f["severity"], f["orphan_count"], f["source_orphan_count"]) == ("error", 0, None)
    assert report["error_count"] == 1
    assert CANARY not in repr(report)


def test_an_unmeasured_baseline_never_reads_as_a_measured_zero(tmp_path: Path) -> None:
    # The source lacks the key column, so its orphan count is unknown: null, never 0.
    source = batches(p={"id": [1]}, c={"other": [1]})
    keys = keys_of(tmp_path, source, batches(p={"id": [1]}, c={"pid": [9]}))
    (f,) = fk({}, {}, keys=keys)
    assert (f["severity"], f["orphan_count"], f["source_orphan_count"]) == ("fail", 1, None)


def test_an_edge_without_a_key_fact_is_an_error() -> None:
    (f,) = fk({}, {}, keys=())
    assert (f["severity"], f["child_table"], f["parent_table"]) == ("error", "c", "p")


def test_a_composite_key_fills_the_tuples() -> None:
    config = {"relationships": [rel_dict("p", ["a", "b"], ("c", ["fa", "fb"]))]}
    source = frames(p={"a": [1, 2], "b": [1, 99]}, c={"fa": [1, 2], "fb": [1, 99]})
    output = frames(p={"a": [1, 2], "b": [1, 99]}, c={"fa": [1, 2], "fb": [99, 99]})
    (f,) = fk(source, output, config)
    assert (f["parent_columns"], f["child_columns"]) == (("a", "b"), ("fa", "fb"))
    assert (f["parent_column"], f["child_column"]) == ("a, b", "fa, fb")
    assert (f["severity"], f["orphan_count"], f["namespace"]) == ("fail", 1, "ns")


def test_a_single_column_key_fills_one_name() -> None:
    data = frames(p={"id": [1]}, c={"pid": [1]})
    (f,) = fk(data, data)
    assert (f["parent_column"], f["parent_columns"], f["child_columns"]) == (
        "id",
        ("id",),
        ("pid",),
    )


def test_a_shared_child_tuple_resolves_against_every_parent() -> None:
    config = {
        "relationships": [rel_dict("a", ["id"], ("c", ["k"])), rel_dict("b", ["id"], ("c", ["k"]))]
    }
    data = frames(a={"id": [1]}, b={"id": [2]}, c={"k": [1, 2]})
    found = fk(data, data, config)
    assert [(f["parent_table"], f["severity"], f["orphan_count"]) for f in found] == [
        ("a", "info", 0),
        ("b", "info", 0),
    ]


def test_an_fk_fail_is_the_orphan_code_and_the_tag_stays_v2() -> None:
    report = run_post_mask(
        frames(p={"id": ["a"]}, c={"pid": ["a"]}),
        frames(p={"id": ["a"]}, c={"pid": [CANARY]}),
        config=CONFIG,
    )
    assert report["schema_version"] == SCHEMA_VERSION == "post-scan-integrity/v2"
    assert report["fk_preservation"][0]["severity"] == "fail"
    assert CANARY not in repr(report)
    assert [c.code for c in post_scan_verdict(report).codes] == ["foreign_key_orphans_introduced"]


def _pipeline_config(tmp_path: Path) -> dict[str, Any]:
    names = ("p", "c")
    raw = {
        "version": 2,
        "global_settings": {"seed": 1},
        "post_scan": {"integrity": True},
        "sources": {
            t: {
                "type": "file",
                "format": "csv",
                "has_header": True,
                "path": str(tmp_path / f"{t}.csv"),
            }
            for t in names
        },
        "tables": [
            {"name": "p", "columns": [{"name": "id", "strategy": "passthrough"}]},
            {"name": "c", "columns": [{"name": "pid", "strategy": "passthrough"}]},
        ],
        "targets": {
            t: {"type": "file", "format": "csv", "path": str(tmp_path / f"{t}.out.csv")}
            for t in names
        },
        "relationships": [REL],
    }
    return PipelineConfig.model_validate(raw).model_dump()


@pytest.mark.parametrize(("child", "severity"), [(["a", "b"], "info"), (["a", "z"], "fail")])
def test_the_in_run_report_keeps_fk_findings_without_keys(
    tmp_path: Path, child: list[str], severity: str
) -> None:
    sources = {"p": pa.table({"id": ["a", "b"]}), "c": pa.table({"pid": ["a", "b"]})}
    result = ExecutionResult(
        outputs={"p": sources["p"], "c": pa.table({"pid": child})},
        table_kinds={"p": "mask", "c": "mask"},
    )
    compute_post_scan_integrity(result, config=_pipeline_config(tmp_path), sources=sources)
    (f,) = result.quality_metrics["post_scan_integrity"]["fk_preservation"]
    assert (f["parent_table"], f["child_table"], f["severity"]) == ("p", "c", severity)


SELF = {"relationships": [rel_dict("e", ["id"], ("e", ["manager_id"]))]}
NAN = float("nan")


def test_a_float_self_reference_with_a_null_root_fails_on_an_introduced_orphan() -> None:
    # pandas reads a nullable integer key as float64; the whole-number column is still a key.
    source = frames(e={"id": [1, 2, 3], "manager_id": [NAN, 1, 1]})
    output = frames(e={"id": [1, 2, 3], "manager_id": [NAN, 1, 9]})
    (f,) = fk(source, output, SELF)
    assert (f["severity"], f["orphan_count"], f["total_child_rows"]) == ("fail", 1, 2)
    assert f["source_orphan_count"] == 0


def test_a_float_self_reference_with_only_inherited_orphans_warns() -> None:
    data = frames(e={"id": [1, 2, 3], "manager_id": [NAN, 1, 9]})
    (f,) = fk(data, data, SELF)
    assert (f["severity"], f["orphan_count"], f["source_orphan_count"]) == ("warning", 1, 1)


def test_a_non_whole_float_key_against_an_integer_parent_stays_a_mismatch() -> None:
    # One fraction at the end of the column decides it: the whole column is read, not a sample.
    whole = [1.0] * 2000
    source = frames(p={"id": [1, 2]}, c={"pid": [1] * 2001})
    output = frames(p={"id": [1, 2]}, c={"pid": [*whole, 1.5]})
    (f,) = fk(source, output)
    assert (f["severity"], f["orphan_count"]) == ("error", 0)


@pytest.mark.parametrize("source_side", ["error", "key_type_mismatch"])
def test_an_introduced_orphan_over_an_unusable_source_still_fails(
    tmp_path: Path, source_side: str
) -> None:
    if source_side == "error":
        source = batches(p={"id": [1]}, c={"pid": [1]})
        source["c"] = failing(source["c"])
    else:
        source = batches(p={"id": [1]}, c={"pid": ["1"]})
    keys = keys_of(tmp_path, source, batches(p={"id": [1]}, c={"pid": [1, 9]}))
    (f,) = fk({}, {}, keys=keys)
    assert (f["severity"], f["orphan_count"], f["source_orphan_count"]) == ("fail", 1, None)
    assert CANARY not in repr(f)


@pytest.mark.parametrize("source_child", [{"other": [1]}, None])
def test_an_output_mismatch_over_an_unmeasured_baseline_is_an_error(
    tmp_path: Path, source_child: dict[str, list[Any]] | None
) -> None:
    # `key_column_missing` when the source lacks the column, `no_source` when it lacks the table.
    source = batches(p={"id": [1]}) | ({} if source_child is None else batches(c=source_child))
    keys = keys_of(tmp_path, source, batches(p={"id": [1]}, c={"pid": ["1"]}))
    (key,) = (k for k in keys if k.kind == "foreign")
    assert (key.status, key.baseline) == (
        "key_type_mismatch",
        "no_source" if source_child is None else "key_column_missing",
    )
    (f,) = fk({}, {}, keys=keys)
    assert (f["severity"], f["orphan_count"], f["total_child_rows"]) == ("error", 0, 0)


FLOATS = ["float32", "Float32", "Float64"]


def floats(values: list[float | None], dtype: str) -> pd.Series:
    return pd.Series(values, dtype=dtype)


@pytest.mark.parametrize("dtype", FLOATS)
def test_a_whole_float_key_of_any_float_dtype_is_an_integer_key(dtype: str) -> None:
    # Nullable extension floats hold a null as NA, NumPy floats as NaN; both decide the same way.
    source = frames(p={"id": [1, 2]}, c={"pid": floats([1.0, 2.0, None], dtype)})
    output = frames(p={"id": [1, 2]}, c={"pid": floats([1.0, 9.0, None], dtype)})
    (f,) = fk(source, output)
    assert (f["severity"], f["orphan_count"], f["total_child_rows"]) == ("fail", 1, 2)


@pytest.mark.parametrize("dtype", FLOATS)
def test_a_non_whole_float_key_of_any_float_dtype_stays_a_mismatch(dtype: str) -> None:
    source = frames(p={"id": [1, 2]}, c={"pid": [1, 2]})
    output = frames(p={"id": [1, 2]}, c={"pid": floats([1.0, 1.5, None], dtype)})
    (f,) = fk(source, output)
    assert (f["severity"], f["orphan_count"]) == ("error", 0)


@pytest.mark.parametrize("dtype", FLOATS)
def test_an_all_null_float_key_of_any_float_dtype_has_nothing_to_check(dtype: str) -> None:
    data = frames(p={"id": [1, 2]}, c={"pid": floats([None, None], dtype)})
    (f,) = fk(data, data)
    assert (f["severity"], f["orphan_count"], f["total_child_rows"]) == ("info", 0, 0)
