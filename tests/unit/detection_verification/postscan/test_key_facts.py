"""`key_facts` against the pre-scan counts, parent-only use, and the type-mismatch path."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config._csv_dialect import CsvDialect
from decoy_engine.execution.out_of_core import _duckdb
from decoy_engine.profile._lazy_csv import CsvLazySource
from decoy_engine.profile._readers import LazySource
from decoy_engine.profiling import KeyBatches, KeyFacts, key_facts
from decoy_engine.profiling._scan_integrity import declare, relationship_facts
from decoy_engine.verification.postmask._stream_input import from_lazy, from_table, key_batches

# Composite (k, j) keys; a row with either part null is incomplete.
PARENT = [("1", "a"), ("2", "a"), ("2", "a"), ("3", "a"), (None, "a"), ("4", None)]
CHILD = [("1", "a"), ("2", "a"), ("9", "a"), (None, "a"), ("9", "a"), ("3", "a"), ("1", None)]


def _files(tmp_path: Path, kind: str, parent: list[Any], child: list[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, rows in (("parent", parent), ("child", child)):
        if kind == "parquet":
            path = tmp_path / f"{name}.parquet"
            cols = {c: pa.array([r[i] for r in rows], pa.string()) for i, c in enumerate("kj")}
            pq.write_table(pa.table(cols), path)
            out[name] = {"type": "file", "format": "parquet", "path": str(path)}
        else:
            path = tmp_path / f"{name}.csv"
            path.write_text("k,j\n" + "".join(",".join(v or "" for v in r) + "\n" for r in rows))
            out[name] = {"type": "file", "format": "csv", "path": str(path), "has_header": True}
    return out


def _source(descriptor: dict[str, Any], name: str) -> LazySource:
    path = Path(descriptor["path"])
    if descriptor["format"] == "csv":
        return CsvLazySource(path, dialect=CsvDialect(), table=name)
    return LazySource(path)


@pytest.mark.parametrize("kind", ["parquet", "csv"])
def test_key_facts_equal_pre_scan_counts(tmp_path: Path, kind: str) -> None:
    sources = _files(tmp_path, kind, PARENT, CHILD)
    rel = {
        "parent": {"table": "parent", "columns": ["k", "j"]},
        "children": [{"table": "child", "columns": ["k", "j"]}],
        "orphan_policy": "warn",
    }
    (pre,) = relationship_facts(
        sources, declare(sources, [rel]), spill_dir=tmp_path / "pre", memory_limit="64MB"
    )
    parent = key_batches(from_lazy("parent", _source(sources["parent"], "parent")), ["k", "j"])
    child = key_batches(from_lazy("child", _source(sources["child"], "child")), ["k", "j"])
    got = key_facts(parent, [child], scratch=tmp_path / "kf", memory_limit="64MB")

    assert got.status == "checked" and pre.status == "checked"
    assert (got.parent_rows, got.parent_null_key_rows, got.parent_duplicate_excess) == (
        pre.parent_rows,
        pre.parent_null_key_rows,
        pre.parent_duplicate_excess,
    )
    (c,), (pc,) = got.children, pre.children
    assert (c.rows, c.null_key_rows, c.orphan_rows, c.orphan_distinct_keys) == (
        pc.rows,
        pc.null_key_rows,
        pc.orphan_rows,
        pc.orphan_distinct_keys,
    )
    # Non-trivial values, so a swapped or wrong-joined count cannot pass by coincidence.
    assert (c.rows, c.null_key_rows, c.orphan_rows, c.orphan_distinct_keys) == (7, 2, 2, 1)
    assert (got.parent_rows, got.parent_null_key_rows, got.parent_duplicate_excess) == (6, 2, 1)


def _keys(values: list[Any], type_: pa.DataType | None = None) -> KeyBatches:
    column = pa.array(values, type_ or pa.string())
    return key_batches(from_table("t", pa.table({"k": column})), ["k"])


def test_parent_only_facts_keep_null_and_duplicate_counts_apart(tmp_path: Path) -> None:
    facts = key_facts(
        _keys(["1", "1", None, None, None]), scratch=tmp_path / "s", memory_limit="64MB"
    )
    assert isinstance(facts, KeyFacts)
    assert facts.children == ()
    assert (facts.parent_rows, facts.parent_null_key_rows, facts.parent_duplicate_excess) == (
        5,
        3,
        1,
    )
    assert facts.bounded is True


def test_composite_key_is_complete_only_when_every_part_is_set(tmp_path: Path) -> None:
    table = pa.table({"a": ["1", "1", None], "b": ["x", None, "y"]})
    keys = key_batches(from_table("t", table), ["a", "b"])
    facts = key_facts(keys, [keys], scratch=tmp_path / "s", memory_limit="64MB")
    assert (facts.parent_rows, facts.parent_null_key_rows) == (3, 2)
    assert (facts.children[0].null_key_rows, facts.children[0].orphan_rows) == (2, 0)


def test_type_mismatch_returns_no_counts_and_opens_no_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(**_: Any) -> None:
        raise AssertionError("a connection was opened")

    monkeypatch.setattr(_duckdb, "connect_duckdb", refuse)
    facts = key_facts(
        _keys(["1"], pa.string()),
        [_keys([1], pa.int64())],
        scratch=tmp_path / "s",
        memory_limit="64MB",
    )
    assert facts.status == "key_type_mismatch"
    assert (facts.parent_rows, facts.parent_null_key_rows, facts.parent_duplicate_excess) == (
        0,
        0,
        0,
    )
    assert facts.children == ()


def test_one_parent_may_chain_several_tables(tmp_path: Path) -> None:
    one, two = _keys(["1", "2"]), _keys(["3", "3"])
    chained = KeyBatches(one.schema, lambda: iter([*one.batches(), *two.batches()]), bounded=True)
    facts = key_facts(chained, [_keys(["1", "9"])], scratch=tmp_path / "s", memory_limit="64MB")
    assert (facts.parent_rows, facts.parent_duplicate_excess) == (4, 1)
    assert facts.children[0].orphan_rows == 1
