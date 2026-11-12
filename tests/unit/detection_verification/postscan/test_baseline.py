"""Baseline status of each declared key: what a zero baseline means, and what the verdict does."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.config import RelationshipConfig
from decoy_engine.execution import ExecutionError
from decoy_engine.fileaccess import FileAccessReadError
from decoy_engine.verification.postmask import (
    TableBatches,
    check_keys,
    from_table,
    keys_report,
    post_scan_verdict,
)

CANARY = "CANARY-4b1d"


def rel(parent: str, pcols: Sequence[str], *children: tuple[str, Sequence[str]]) -> Any:
    return RelationshipConfig.model_validate(
        {
            "parent": {"table": parent, "columns": list(pcols)},
            "children": [{"table": t, "columns": list(c)} for t, c in children],
            "orphan_policy": "warn",
        }
    )


def tables(**spec: dict[str, list[Any]]) -> dict[str, TableBatches]:
    return {name: from_table(name, pa.table(cols)) for name, cols in spec.items()}


def results(tmp_path: Path, rels: Any, source: Any, output: Any) -> dict[tuple[str, str], Any]:
    found = check_keys(rels, source, output, scratch=tmp_path / "s", memory_limit="64MB")
    return {(r.table, r.kind): r for r in found}


def verdict_of(tmp_path: Path, rels: Any, source: Any, output: Any) -> Any:
    found = check_keys(rels, source, output, scratch=tmp_path / "s", memory_limit="64MB")
    return post_scan_verdict({**VERSION, "keys": keys_report(found)})


def names(refs: Any) -> list[str]:
    return [r.code for r in refs]


VERSION = {"schema_version": "post-scan-integrity/v2"}
RELS = [rel("p", ["id"], ("c", ["pid"]))]


def failing(table: TableBatches, error: BaseException) -> TableBatches:
    def read(columns: Sequence[str], batch_rows: int) -> Iterator[pa.RecordBatch]:
        raise error

    return replace(table, read=read)


def test_a_measured_baseline_is_reported_with_its_counts(tmp_path: Path) -> None:
    data = tables(p={"id": [1, 2]}, c={"pid": [1, 9]})
    hit = results(tmp_path, RELS, data, data)[("c", "foreign")]
    assert (hit.baseline, hit.codes) == ("measured", ())
    row = next(r for r in keys_report([hit]))
    assert row["baseline"] == "measured"
    assert row["source"]["orphan_rows"] == 1
    assert "baseline_error_class" not in row


def test_an_output_missing_a_key_column_is_not_run_and_blocks(tmp_path: Path) -> None:
    source = tables(p={"id": [1]}, c={"pid": [1]})
    output = tables(p={"id": [1]}, c={"other": [1]})
    res = results(tmp_path, RELS, source, output)[("c", "foreign")]
    assert (res.status, res.baseline) == ("key_column_missing", "not_run")
    assert names(verdict_of(tmp_path, RELS, source, output).codes) == ["key_column_missing"]


def test_source_and_output_mismatch_together_warn_and_do_not_block(tmp_path: Path) -> None:
    data = tables(p={"id": [1]}, c={"pid": ["1"]})
    res = results(tmp_path, RELS, data, data)[("c", "foreign")]
    assert (res.status, res.baseline) == ("key_type_mismatch", "key_type_mismatch")
    verdict = verdict_of(tmp_path, RELS, data, data)
    assert (verdict.blocking, names(verdict.warnings)) == (False, ["key_type_mismatch"])


def test_a_comparable_source_and_an_incomparable_output_block(tmp_path: Path) -> None:
    source = tables(p={"id": [1]}, c={"pid": [1]})
    output = tables(p={"id": [1]}, c={"pid": ["1"]})
    res = results(tmp_path, RELS, source, output)[("c", "foreign")]
    assert (res.status, res.baseline) == ("key_type_mismatch", "not_run")
    assert names(verdict_of(tmp_path, RELS, source, output).codes) == ["key_type_mismatch"]


@pytest.mark.parametrize(("orphan", "blocks"), [(9, True), (1, False)])
def test_a_source_mismatch_with_a_comparable_output_blocks_only_on_an_orphan(
    tmp_path: Path, orphan: int, blocks: bool
) -> None:
    source = tables(p={"id": [1]}, c={"pid": ["1"]})
    output = tables(p={"id": [1]}, c={"pid": [orphan]})
    res = results(tmp_path, RELS, source, output)[("c", "foreign")]
    assert (res.status, res.baseline) == ("checked", "key_type_mismatch")
    verdict = verdict_of(tmp_path, RELS, source, output)
    assert verdict.blocking is blocks
    assert names(verdict.codes) == (["foreign_key_orphans_introduced"] if blocks else [])
    assert names(verdict.warnings) == ["key_type_mismatch"]


@pytest.mark.parametrize(("orphan", "blocks"), [(9, True), (1, False)])
def test_a_child_whose_source_parents_differ_in_type_takes_the_same_rows(
    tmp_path: Path, orphan: int, blocks: bool
) -> None:
    rels = [rel("a", ["id"], ("c", ["k"])), rel("b", ["id"], ("c", ["k"]))]
    source = tables(a={"id": [1]}, b={"id": ["x"]}, c={"k": [1]})
    output = tables(a={"id": [1]}, b={"id": [2]}, c={"k": [orphan]})
    res = results(tmp_path, rels, source, output)[("c", "foreign")]
    assert (res.status, res.baseline) == ("checked", "key_type_mismatch")
    verdict = verdict_of(tmp_path, rels, source, output)
    assert verdict.blocking is blocks
    assert names(verdict.warnings) == ["key_type_mismatch"]


@pytest.mark.parametrize(
    "error",
    [
        ExecutionError(code=reasons.runtime.LAZY_SOURCE_CHANGED, message="changed"),
        FileAccessReadError("sources.c.path"),
    ],
)
def test_a_coded_error_counting_the_source_propagates_as_the_same_object(
    tmp_path: Path, error: BaseException
) -> None:
    source = tables(p={"id": [1]}, c={"pid": [1]})
    source["c"] = failing(source["c"], error)
    output = tables(p={"id": [1]}, c={"pid": [1]})
    with pytest.raises(type(error)) as info:
        check_keys(RELS, source, output, scratch=tmp_path / "s", memory_limit="64MB")
    assert info.value is error


@pytest.mark.parametrize(("orphan", "blocks"), [(9, True), (1, False)])
def test_a_source_table_lacking_the_key_column_blocks_only_on_an_orphan(
    tmp_path: Path, orphan: int, blocks: bool
) -> None:
    source = tables(p={"id": [1]}, c={"other": [1]})
    output = tables(p={"id": [1]}, c={"pid": [orphan]})
    res = results(tmp_path, RELS, source, output)[("c", "foreign")]
    assert (res.status, res.baseline) == ("checked", "key_column_missing")
    verdict = verdict_of(tmp_path, RELS, source, output)
    assert (verdict.blocking, names(verdict.warnings)) == (blocks, [])
    assert names(verdict.codes) == (["foreign_key_orphans_introduced"] if blocks else [])


@pytest.mark.parametrize(("orphan", "blocks"), [(9, True), (1, False)])
def test_an_uncoded_source_side_error_warns_and_blocks_only_on_an_orphan(
    tmp_path: Path, orphan: int, blocks: bool
) -> None:
    source = tables(p={"id": [1]}, c={"pid": [1]})
    source["c"] = failing(source["c"], RuntimeError(f"cannot read {CANARY}"))
    output = tables(p={"id": [1]}, c={"pid": [orphan]})
    res = results(tmp_path, RELS, source, output)[("c", "foreign")]
    assert (res.status, res.baseline, res.baseline_error_class) == (
        "checked",
        "error",
        "RuntimeError",
    )
    found = check_keys(RELS, source, output, scratch=tmp_path / "t", memory_limit="64MB")
    assert CANARY not in repr(keys_report(found))
    verdict = post_scan_verdict({**VERSION, "keys": keys_report(found)})
    assert verdict.blocking is blocks
    assert names(verdict.codes) == (["foreign_key_orphans_introduced"] if blocks else [])
    assert names(verdict.warnings) == ["check_error"]


def test_a_generated_child_has_no_source_and_any_orphan_blocks(tmp_path: Path) -> None:
    source = tables(p={"id": [1]})
    output = tables(p={"id": [1]}, c={"pid": [9]})
    res = results(tmp_path, RELS, source, output)[("c", "foreign")]
    assert (res.baseline, res.source.orphan_rows) == ("no_source", 0)
    assert names(verdict_of(tmp_path, RELS, source, output).codes) == [
        "foreign_key_orphans_introduced"
    ]
    row = next(iter(keys_report([res])))
    assert row["source"]["orphan_rows"] == 0


def test_an_unmeasured_baseline_reports_null_source_counts(tmp_path: Path) -> None:
    cases = [
        (tables(p={"id": [1]}, c={"pid": ["1"]}), tables(p={"id": [1]}, c={"pid": [1]})),
        (tables(p={"id": [1]}, c={"other": [1]}), tables(p={"id": [1]}, c={"pid": [1]})),
        (tables(p={"id": [1]}, c={"pid": [1]}), tables(p={"id": [1]}, c={"other": [1]})),
        (
            failing(tables(p={"id": [1]}, c={"pid": [1]})["c"], RuntimeError("boom")),
            tables(p={"id": [1]}, c={"pid": [1]}),
        ),
    ]
    seen = set()
    for source, output in cases:
        res = results(tmp_path, RELS, source, output)[("c", "foreign")]
        seen.add(res.baseline)
        assert keys_report([res])[0]["source"] is None
    assert seen == {"key_type_mismatch", "key_column_missing", "not_run", "error"}


def test_every_code_in_the_report_is_a_namespace_and_code_mapping(tmp_path: Path) -> None:
    source = tables(p={"id": [1]}, c={"pid": ["1"]})
    output = tables(p={"id": [1]}, c={"pid": [9]})
    found = check_keys(RELS, source, output, scratch=tmp_path / "s", memory_limit="64MB")
    report = {"keys": keys_report(found)}
    verdict = post_scan_verdict({**VERSION, **report}).as_dict()
    coded = [c for k in report["keys"] for c in k["codes"]] + verdict["codes"] + verdict["warnings"]
    assert coded and all(set(c) == {"namespace", "code"} for c in coded)
