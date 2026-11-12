"""Declared key checks: what the run introduced blocks, what the inputs already held does not."""

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
from decoy_engine.profiling import _scan_integrity
from decoy_engine.verification.postmask import (
    TableBatches,
    check_keys,
    from_table,
    keys_report,
)

P = reasons.post_scan
CANARY = "CANARY-7f3a"


def rel(
    parent: str, pcols: Sequence[str], *children: tuple[str, Sequence[str]], policy: str = "warn"
) -> RelationshipConfig:
    return RelationshipConfig.model_validate(
        {
            "parent": {"table": parent, "columns": list(pcols)},
            "children": [{"table": t, "columns": list(c)} for t, c in children],
            "orphan_policy": policy,
        }
    )


def tables(**spec: dict[str, list[Any]]) -> dict[str, TableBatches]:
    return {name: from_table(name, pa.table(cols)) for name, cols in spec.items()}


def run(
    tmp_path: Path,
    rels: Sequence[RelationshipConfig],
    source: dict[str, TableBatches],
    output: dict[str, TableBatches],
) -> dict[tuple[str, str], Any]:
    results = check_keys(rels, source, output, scratch=tmp_path / "s", memory_limit="64MB")
    return {(r.table, r.kind): r for r in results}


def codes(result: Any) -> set[str]:
    return {str(c) for c in result.codes}


def test_masking_that_collapses_parent_keys_blocks_but_source_duplicates_do_not(
    tmp_path: Path,
) -> None:
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    clean = tables(p={"id": [1, 2, 3]}, c={"pid": [1, 2, 3]})
    collapsed = tables(p={"id": [1, 1, 3]}, c={"pid": [1, 1, 3]})
    hit = run(tmp_path, rels, clean, collapsed)[("p", "primary")]
    assert codes(hit) == {"primary_key_duplicates_introduced"}
    assert (hit.source.duplicate_excess, hit.output.duplicate_excess) == (0, 1)
    inherited = tables(p={"id": [1, 1, 2]}, c={"pid": [1, 2, 2]})
    assert run(tmp_path, rels, inherited, inherited)[("p", "primary")].codes == ()


def test_a_null_primary_key_part_blocks_and_a_composite_is_one_tuple(tmp_path: Path) -> None:
    rels = [rel("p", ["a", "b"], ("c", ["a", "b"]))]
    source = tables(p={"a": [1, 1, 2], "b": ["x", "y", "x"]}, c={"a": [1], "b": ["x"]})
    nulled = tables(p={"a": [1, 1, 2], "b": ["x", None, "x"]}, c={"a": [1], "b": ["x"]})
    hit = run(tmp_path, rels, source, nulled)[("p", "primary")]
    assert codes(hit) == {"primary_key_nulls_introduced"}
    assert hit.output.null_key_rows == 1
    # (1, x) and (1, y) share k0 and are still two keys.
    same = tables(p={"a": [1, 1, 2], "b": ["x", "y", "x"]}, c={"a": [1], "b": ["x"]})
    clean = run(tmp_path, rels, source, same)[("p", "primary")]
    assert clean.output.duplicate_excess == 0
    assert clean.codes == ()


@pytest.mark.parametrize("policy", ["preserve", "warn", "remap"])
def test_child_keys_moved_outside_the_parent_block_but_source_orphans_do_not(
    tmp_path: Path, policy: str
) -> None:
    rels = [rel("p", ["id"], ("c", ["pid"]), policy=policy)]
    source = tables(p={"id": [1, 2]}, c={"pid": [1, 9]})
    kept = tables(p={"id": [1, 2]}, c={"pid": [1, 77]})  # a remapped orphan is still one orphan
    assert run(tmp_path, rels, source, kept)[("c", "foreign")].codes == ()
    moved = tables(p={"id": [1, 2]}, c={"pid": [55, 77]})
    hit = run(tmp_path, rels, source, moved)[("c", "foreign")]
    assert codes(hit) == {"foreign_key_orphans_introduced"}
    assert (hit.source.orphan_rows, hit.output.orphan_rows) == (1, 2)


def test_a_composite_child_is_an_orphan_when_no_single_parent_holds_the_whole_tuple(
    tmp_path: Path,
) -> None:
    rels = [rel("p", ["a", "b"], ("c", ["a", "b"]))]
    parent = {"a": [1, 4], "b": [3, 2]}
    source = tables(p=parent, c={"a": [4], "b": [2]})
    output = tables(p=parent, c={"a": [1], "b": [2]})  # (1, 2): both parts exist, never together
    hit = run(tmp_path, rels, source, output)[("c", "foreign")]
    assert hit.output.orphan_rows == 1
    assert codes(hit) == {"foreign_key_orphans_introduced"}


def test_a_child_key_with_a_null_part_is_counted_and_never_an_orphan(tmp_path: Path) -> None:
    rels = [rel("p", ["a", "b"], ("c", ["a", "b"]))]
    source = tables(p={"a": [1], "b": [2]}, c={"a": [1], "b": [2]})
    output = tables(p={"a": [1], "b": [2]}, c={"a": [1, None], "b": [None, 2]})
    hit = run(tmp_path, rels, source, output)[("c", "foreign")]
    assert (hit.output.null_key_rows, hit.output.orphan_rows) == (2, 0)
    assert hit.codes == ()


class _Counting:
    """Wraps a table so the test can see which column sets were read, and how often."""

    def __init__(self, table: TableBatches) -> None:
        self.calls: list[tuple[str, ...]] = []
        inner = table.read

        def read(columns: Sequence[str], batch_rows: int) -> Iterator[pa.RecordBatch]:
            self.calls.append(tuple(columns))
            return inner(columns, batch_rows)

        self.table = replace(table, read=read)


def test_a_self_reference_reads_both_ends_separately(tmp_path: Path) -> None:
    rels = [rel("emp", ["id"], ("emp", ["manager_id"]))]
    data: dict[str, list[Any]] = {"id": [1, 2, 3], "manager_id": [None, 1, 2]}
    out = _Counting(from_table("emp", pa.table(data)))
    res = run(tmp_path, rels, tables(emp=data), {"emp": out.table})
    assert res[("emp", "foreign")].codes == ()
    assert out.calls.count(("manager_id",)) == 1
    assert out.calls.count(("id",)) == 2  # once for the primary key, once as the parent end


def test_two_relationships_over_one_parent_check_its_key_once_in_any_order(
    tmp_path: Path,
) -> None:
    first = rel("cust", ["id"], ("orders", ["cid"]))
    second = rel("cust", ["id"], ("tickets", ["cid"]))
    source = tables(cust={"id": [1, 2]}, orders={"cid": [1]}, tickets={"cid": [2]})
    output = tables(cust={"id": [1, 2]}, orders={"cid": [1]}, tickets={"cid": [8]})
    seen = []
    for order in ([first, second], [second, first]):
        results = check_keys(order, source, output, scratch=tmp_path / "s", memory_limit="64MB")
        assert [r.table for r in results].count("cust") == 1
        seen.append({(r.table, str(c)) for r in results for c in r.codes})
    assert seen[0] == seen[1] == {("tickets", "foreign_key_orphans_introduced")}


def test_a_missing_key_column_and_incomparable_types_are_their_own_codes(tmp_path: Path) -> None:
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    source = tables(p={"id": [1]}, c={"pid": [1]})
    gone = tables(p={"id": [1]}, c={"other": [1]})
    res = run(tmp_path, rels, source, gone)[("c", "foreign")]
    assert (res.status, codes(res)) == ("key_column_missing", {"key_column_missing"})
    mixed = tables(p={"id": [1]}, c={"pid": ["1"]})
    res = run(tmp_path, rels, source, mixed)[("c", "foreign")]
    assert (res.status, codes(res)) == ("key_type_mismatch", {"key_type_mismatch"})


def test_a_duckdb_failure_is_check_error_with_the_class_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(conn: Any) -> None:
        raise RuntimeError(f"cannot read {CANARY}")

    monkeypatch.setattr(_scan_integrity, "_before_fetch", boom)
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    data = tables(p={"id": [1]}, c={"pid": [1]})
    results = check_keys(rels, data, data, scratch=tmp_path / "s", memory_limit="64MB")
    failed = [r for r in results if r.status == "error"]
    assert [r.error_class for r in failed] == ["RuntimeError"]
    assert codes(failed[0]) == {"check_error"}
    assert CANARY not in repr(keys_report(results))


class _LocalBoomError(Exception):
    pass


def test_a_class_the_engine_does_not_define_is_reported_as_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(conn: Any) -> None:
        raise _LocalBoomError(CANARY)

    monkeypatch.setattr(_scan_integrity, "_before_fetch", boom)
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    data = tables(p={"id": [1]}, c={"pid": [1]})
    results = check_keys(rels, data, data, scratch=tmp_path / "s", memory_limit="64MB")
    assert [r.error_class for r in results if r.status == "error"] == ["Exception"]
    assert CANARY not in repr(keys_report(results))


def test_a_coded_engine_error_propagates_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coded = ExecutionError(code=reasons.runtime.LAZY_SOURCE_CHANGED, message="changed")

    def refuse(conn: Any) -> None:
        raise coded

    monkeypatch.setattr(_scan_integrity, "_before_fetch", refuse)
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    data = tables(p={"id": [1]}, c={"pid": [1]})
    with pytest.raises(ExecutionError) as info:
        check_keys(rels, data, data, scratch=tmp_path / "s", memory_limit="64MB")
    assert info.value is coded


def test_an_uncoded_failure_is_an_error_result_and_raises_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(conn: Any) -> None:
        raise TypeError(CANARY)

    monkeypatch.setattr(_scan_integrity, "_before_fetch", boom)
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    data = tables(p={"id": [1]}, c={"pid": [1]})
    results = check_keys(rels, data, data, scratch=tmp_path / "s", memory_limit="64MB")
    failed = [r for r in results if r.status == "error"]
    assert [r.error_class for r in failed] == ["TypeError"]
    assert codes(failed[0]) == {"check_error"}


def test_the_report_codes_are_namespace_and_code_mappings(tmp_path: Path) -> None:
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    source = tables(p={"id": [1, 2]}, c={"pid": [1]})
    output = tables(p={"id": [1, 1]}, c={"pid": [1]})
    report = keys_report(
        check_keys(rels, source, output, scratch=tmp_path / "s", memory_limit="64MB")
    )
    primary = next(r for r in report if r["kind"] == "primary")
    assert primary["codes"] == [
        {"namespace": "post_scan", "code": "primary_key_duplicates_introduced"}
    ]


def test_the_report_carries_no_key_value(tmp_path: Path) -> None:
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    source = tables(p={"id": ["a", "b"]}, c={"pid": ["a"]})
    output = tables(p={"id": [CANARY, CANARY]}, c={"pid": [f"{CANARY}-gone"]})
    results = check_keys(rels, source, output, scratch=tmp_path / "s", memory_limit="64MB")
    assert {str(c) for r in results for c in r.codes} >= {
        "primary_key_duplicates_introduced",
        "foreign_key_orphans_introduced",
    }
    assert CANARY not in repr(keys_report(results))


def test_a_child_shared_by_two_parents_is_checked_against_the_union(tmp_path: Path) -> None:
    rels = [rel("a", ["id"], ("c", ["k"])), rel("b", ["id"], ("c", ["k"]))]
    parents = {"a": {"id": [1, 1]}, "b": {"id": [2]}}  # a's duplicate is inherited
    source = tables(**parents, c={"k": [1, 2]})
    output = tables(**parents, c={"k": [1, 2, 3]})
    results = check_keys(rels, source, output, scratch=tmp_path / "s", memory_limit="64MB")
    assert [r.table for r in results if r.kind == "foreign"] == ["c"]
    res = {(r.table, r.kind): r for r in results}
    child = res[("c", "foreign")]
    assert child.output.orphan_rows == 1  # 3 is in neither parent; 1 and 2 are in one each
    assert child.parent == ("a", "b")
    assert res[("a", "primary")].codes == () and res[("b", "primary")].codes == ()
    assert len([k for k in res if k[1] == "primary"]) == 2


def test_parents_with_different_key_types_are_a_type_mismatch(tmp_path: Path) -> None:
    rels = [rel("a", ["id"], ("c", ["k"])), rel("b", ["id"], ("c", ["k"]))]
    data = tables(a={"id": [1]}, b={"id": ["x"]}, c={"k": [1]})
    res = run(tmp_path, rels, data, data)[("c", "foreign")]
    assert (res.status, codes(res)) == ("key_type_mismatch", {"key_type_mismatch"})


def test_an_int32_parent_then_an_int64_parent_count_a_value_above_the_int32_range(
    tmp_path: Path,
) -> None:
    big = 2**40
    rels = [rel("a", ["id"], ("c", ["k"])), rel("b", ["id"], ("c", ["k"]))]
    data = {
        "a": from_table("a", pa.table({"id": pa.array([1, 2], pa.int32())})),
        "b": from_table("b", pa.table({"id": pa.array([big, 3], pa.int64())})),
        "c": from_table("c", pa.table({"k": pa.array([1, big, 3, 7], pa.int64())})),
    }
    child = run(tmp_path, rels, data, data)[("c", "foreign")]
    assert (child.status, child.codes) == ("checked", ())
    assert (child.output.rows, child.output.orphan_rows) == (4, 1)  # only 7 is in neither parent


def test_a_table_absent_from_the_inputs_has_a_zero_baseline(tmp_path: Path) -> None:
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    source = tables(p={"id": [1]})
    output = tables(p={"id": [1]}, c={"pid": [9]})
    hit = run(tmp_path, rels, source, output)[("c", "foreign")]
    assert (hit.source.orphan_rows, hit.output.orphan_rows) == (0, 1)
    assert codes(hit) == {"foreign_key_orphans_introduced"}


def test_a_source_side_type_mismatch_gives_a_zero_baseline(tmp_path: Path) -> None:
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    source = tables(p={"id": [1]}, c={"pid": ["1"]})
    output = tables(p={"id": [1]}, c={"pid": [9]})
    hit = run(tmp_path, rels, source, output)[("c", "foreign")]
    assert (hit.status, hit.source.orphan_rows, hit.output.orphan_rows) == ("checked", 0, 1)
    assert codes(hit) == {"foreign_key_orphans_introduced"}


def test_an_empty_table_is_checked_and_clean(tmp_path: Path) -> None:
    rels = [rel("p", ["id"], ("c", ["pid"]))]
    empty = {
        name: from_table(name, pa.table({col: pa.array([], pa.int64())}))
        for name, col in (("p", "id"), ("c", "pid"))
    }
    res = run(tmp_path, rels, empty, empty)
    assert [(r.status, r.codes, r.output.rows) for r in res.values()] == [("checked", (), 0)] * 2


def test_one_column_that_is_a_primary_and_a_foreign_key_is_two_independent_checks(
    tmp_path: Path,
) -> None:
    rels = [rel("a", ["id"], ("b", ["aid"])), rel("b", ["aid"], ("c", ["bid"]))]
    source = tables(a={"id": [1, 2]}, b={"aid": [1, 2]}, c={"bid": [1, 2]})
    output = tables(a={"id": [1, 2]}, b={"aid": [1, 1]}, c={"bid": [1, 2]})
    res = run(tmp_path, rels, source, output)
    assert codes(res[("b", "primary")]) == {"primary_key_duplicates_introduced"}
    assert res[("b", "foreign")].codes == ()
    assert codes(res[("c", "foreign")]) == {"foreign_key_orphans_introduced"}
