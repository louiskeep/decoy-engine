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
    assert run(tmp_path, rels, source, source)[("p", "primary")].codes == ()


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
    data = {"id": [1, 2, 3], "manager_id": [None, 1, 2]}
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
    res = run(tmp_path, rels, source, output)
    child = res[("c", "foreign")]
    assert child.output.orphan_rows == 1  # 3 is in neither parent; 1 and 2 are in one each
    assert child.parent == ("a", "b")
    assert res[("a", "primary")].codes == () and res[("b", "primary")].codes == ()
    assert len([k for k in res if k[1] == "primary"]) == 2
