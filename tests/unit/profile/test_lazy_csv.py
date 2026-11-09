"""AT-E2a (RUN-STREAM): `CsvLazySource` is a substitutable `LazySource` over the one CSV reader.

Written before the implementation. Do not delete one, add a skip or xfail, or weaken a
comparison to make a case pass.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.config._csv_dialect import CsvDialect
from decoy_engine.errors import PipelineValidationError
from decoy_engine.execution import ExecutionError
from decoy_engine.execution import _chunked_input as ci
from decoy_engine.profile import _lazy_csv
from decoy_engine.profile._csv_reader import read_csv_source
from decoy_engine.profile._lazy_csv import CsvLazySource
from decoy_engine.profile._readers import LazySource
from tests.unit.execution import _auto_chunk_support as support
from tests.unit.execution import _b6a_support as b6a

CORPUS: dict[str, tuple[bytes, CsvDialect]] = {
    "plain": (b"a,b\n1,x\n2,y\n3,\n", CsvDialect()),
    "quotes": (b'a,b\n"1,5","he said ""hi"""\n2,y\n', CsvDialect()),
    "embedded_newline": (b'a,b\n1,"line one\nline two"\n2,y\n', CsvDialect()),
    "bom": (b"\xef\xbb\xbfa,b\n1,x\n", CsvDialect()),
    "crlf": (b"a,b\r\n1,x\r\n2,y\r\n", CsvDialect()),
    "empty_cells": (b"a,b\n,\n1,\n,y\n", CsvDialect()),
    "headerless": (b"1,x\n2,y\n", CsvDialect(False, ("a", "b"), ",")),
    "semicolon": (b"a;b\n1;x\n", CsvDialect(True, None, ";")),
    "header_only": (b"a,b\n", CsvDialect()),
}


def source(path: Path, dialect: CsvDialect | None = None, table: str = "t") -> CsvLazySource:
    return CsvLazySource(path, dialect=dialect or CsvDialect(), table=table)


def resident(path: Path, dialect: CsvDialect) -> list[list[Any]]:
    frame = read_csv_source(path, dialect, table="t", dtype=str)
    return [[None if pd.isna(v) else v for v in row] for row in frame.to_numpy().tolist()]


def rows_of(batches: Any) -> list[list[Any]]:
    out: list[list[Any]] = []
    for batch in batches:
        cols = [batch.column(i).to_pylist() for i in range(batch.num_columns)]
        out.extend([list(r) for r in zip(*cols, strict=True)])
    return out


def test_subclass_relationship_is_pinned() -> None:
    assert issubclass(CsvLazySource, LazySource)


@pytest.mark.parametrize("name", sorted(CORPUS))
@pytest.mark.parametrize("batch_rows", [1, 2, 1000])
def test_batches_equal_the_resident_read(name: str, batch_rows: int, tmp_path: Path) -> None:
    raw, dialect = CORPUS[name]
    path = tmp_path / "f.csv"
    path.write_bytes(raw)
    lazy = source(path, dialect)
    opened = lazy.open_batches(batch_rows)
    got = rows_of(opened.batches)
    assert got == resident(path, dialect)
    assert all(pa.types.is_string(f.type) for f in opened.schema)
    assert opened.num_rows == len(got)
    assert rows_of(lazy.iter_batches(batch_rows)) == got
    assert lazy.to_table().to_pylist() == [
        dict(zip(opened.schema.names, r, strict=True)) for r in got
    ]


def test_facts_are_exact_and_describe_no_row_groups(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(CORPUS["empty_cells"][0])
    lazy = source(path)
    facts = lazy.footer_facts()
    assert facts.num_rows == 3
    assert dict(facts.null_counts) == {"a": 2, "b": 2}
    assert (facts.row_groups, facts.max_row_group_rows) == (0, 0)
    assert lazy.num_rows == 3
    assert lazy.column_null_counts() == {"a": 2, "b": 2}
    assert lazy.schema.names == ["a", "b"]


def test_existing_choke_points_admit_it_without_csv_branches(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(CORPUS["plain"][0])
    lazy = source(path)
    facts = ci.source_facts(lazy)
    assert facts.num_rows == 3 and facts.null_count("b") == 1
    assert set(ci.capture_source_facts({"t": lazy}, {"t": "mask"})) == {"t"}
    chunks = ci.open_input(lazy, table="t", chunk_size_rows=2, expected=facts, native_threads=1)
    assert [c.num_rows for c in chunks.chunks] == [2, 1]
    chunks.close()


def test_run_pipeline_equals_its_resident_twin(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    lines = ["id,name"] + [f"{i},person{i}" for i in range(40)]
    path.write_text("\n".join(lines) + "\n")
    cfg = support.make_cfg([support.pass_col("id"), support.hash_col("name")], path=str(path))
    cfg["sources"]["t"].update(format="csv", has_header=True)
    lazy = source(path)
    twin = pa.Table.from_pandas(
        read_csv_source(path, CsvDialect(), table="t", dtype=str),
        schema=lazy.schema,
        preserve_index=False,
    )
    from decoy_engine.execution import run_pipeline

    gsink, wsink = b6a.RecordingSink(), b6a.RecordingSink()
    got = run_pipeline(cfg, sources={"t": lazy}, sink=gsink, **support.run_kwargs())
    want = run_pipeline(cfg, sources={"t": twin}, sink=wsink, **support.run_kwargs())
    assert got.quality_metrics["auto_chunk"]["input"]["mode"] == "lazy"
    assert rows_of(gsink.batches["t"]) == rows_of(wsink.batches["t"])
    assert want.quality_metrics["auto_chunk"]["input"]["mode"] == "resident"


def test_preflight_reads_no_data_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(CORPUS["plain"][0])
    calls: list[int | None] = []
    real = _lazy_csv.read_csv_source

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("chunksize"))
        return real(*args, **kwargs)

    monkeypatch.setattr(_lazy_csv, "read_csv_source", spy)
    lazy = source(path)
    assert lazy.preflight().names == ["a", "b"]
    assert lazy.schema.names == ["a", "b"]
    assert calls == [None, None]


def test_facts_pass_starts_after_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(CORPUS["plain"][0])
    order: list[str] = []
    real = _lazy_csv.read_csv_source

    def spy(*args: Any, **kwargs: Any) -> Any:
        order.append("data" if kwargs.get("chunksize") else "header")
        return real(*args, **kwargs)

    monkeypatch.setattr(_lazy_csv, "read_csv_source", spy)
    source(path).footer_facts()
    assert order == ["header", "data"]


def test_a_ragged_headerless_file_is_refused_before_a_row_is_yielded(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"1,x\n2,y,CANARY\n")
    lazy = source(path, CsvDialect(False, ("a", "b"), ","))
    assert lazy.schema.names == ["a", "b"]
    with pytest.raises(PipelineValidationError) as exc:
        lazy.footer_facts()
    assert "CANARY" not in str(exc.value)
    with pytest.raises(PipelineValidationError):
        next(iter(lazy.iter_batches(10)))


def test_from_descriptor_resolves_the_dialect_once(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"1;x\n")
    descriptor = {
        "type": "file",
        "format": "csv",
        "path": str(path),
        "has_header": False,
        "column_names": ["a", "b"],
        "delimiter": ";",
    }
    lazy = CsvLazySource.from_descriptor(descriptor, table="t")
    assert lazy.dialect == CsvDialect(False, ("a", "b"), ";")
    assert rows_of(lazy.iter_batches(5)) == [["1", "x"]]


def test_a_file_changed_between_facts_and_batches_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n1,x\n2,y\n")
    lazy = source(path)
    expected = ci.source_facts(lazy)
    path.write_bytes(b"a,b\n1,x\n2,y\n3,z\n")
    with pytest.raises(ExecutionError) as exc:
        ci.open_input(lazy, table="t", chunk_size_rows=2, expected=expected, native_threads=1)
    assert exc.value.code == reasons.runtime.LAZY_SOURCE_CHANGED


def test_a_same_size_rewrite_is_refused_by_identity(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n1,x\n")
    lazy = source(path)
    lazy.footer_facts()
    stat = path.stat()
    path.write_bytes(b"a,b\n1,y\n")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    with pytest.raises(ExecutionError) as exc:
        lazy.open_batches(10)
    assert exc.value.code == reasons.runtime.LAZY_SOURCE_CHANGED


def test_a_file_changed_while_batches_are_read_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n" + b"1,x\n" * 6)
    lazy = source(path)
    opened = lazy.open_batches(2)
    it = iter(opened.batches)
    next(it)
    path.write_bytes(b"a,b\n" + b"1,x\n" * 6 + b"9,z\n")
    with pytest.raises(ExecutionError) as exc:
        list(it)
    assert exc.value.code == reasons.runtime.LAZY_SOURCE_CHANGED


def test_a_file_changed_while_facts_are_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n1,x\n")
    real = _lazy_csv.read_csv_source

    def spy(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("chunksize"):
            path.write_bytes(b"a,b\n1,x\n2,y\n")
        return real(*args, **kwargs)

    monkeypatch.setattr(_lazy_csv, "read_csv_source", spy)
    with pytest.raises(ExecutionError):
        source(path).footer_facts()


def test_close_before_the_first_batch_is_safe_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n1,x\n")
    opened = source(path).open_batches(2)
    opened.close()
    opened.close()


def _count_chunked(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    real = _lazy_csv.read_csv_source

    def spy(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("chunksize"):
            calls.append(kwargs["chunksize"])
        return real(*args, **kwargs)

    monkeypatch.setattr(_lazy_csv, "read_csv_source", spy)
    return calls


def _csv_cfg(path: Path) -> dict[str, Any]:
    cfg = support.make_cfg([support.pass_col("id"), support.hash_col("name")], path=str(path))
    cfg["sources"]["t"].update(format="csv", has_header=True)
    return cfg


@pytest.mark.parametrize("byte_estimate", [False, True])
def test_a_run_reads_the_file_twice_only(
    byte_estimate: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution import run_pipeline

    path = tmp_path / "f.csv"
    path.write_text("\n".join(["id,name"] + [f"{i},p{i}" for i in range(40)]) + "\n")
    calls = _count_chunked(monkeypatch)
    run_pipeline(
        _csv_cfg(path),
        sources={"t": source(path)},
        sink=b6a.RecordingSink(),
        **support.run_kwargs(use_byte_estimate_routing=byte_estimate),
    )
    assert len(calls) == 2


def test_cached_facts_serve_num_rows_and_null_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(CORPUS["empty_cells"][0])
    lazy = source(path)
    lazy.footer_facts()
    calls = _count_chunked(monkeypatch)
    for _ in range(3):
        assert lazy.num_rows == 3
        assert lazy.column_null_counts() == {"a": 2, "b": 2}
    assert calls == []


def test_accessors_recount_once_when_the_file_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n1,x\n")
    lazy = source(path)
    assert lazy.num_rows == 1
    path.write_bytes(b"a,b\n1,x\n2,y\n")
    calls = _count_chunked(monkeypatch)
    assert lazy.num_rows == 2
    assert lazy.num_rows == 2
    assert len(calls) == 1


def test_the_snapshot_is_refreshed_only_through_footer_facts(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n1,x\n")
    lazy = source(path)
    facts = lazy.footer_facts()
    assert lazy.footer_facts() is facts and lazy._seen["facts"] is facts
    path.write_bytes(b"a,b\n1,x\n2,y\n")
    assert lazy.num_rows == 2
    assert lazy._seen["facts"] is not facts and lazy._seen["facts"].num_rows == 2


def test_the_changed_refusal_has_one_constructor() -> None:
    from decoy_engine.execution import _chunked_input
    from decoy_engine.execution._errors import ExecutionError as Reexported
    from decoy_engine.execution._errors import lazy_source_changed

    err = lazy_source_changed("t", "x")
    assert isinstance(err, Reexported) and Reexported is ExecutionError
    assert err.code == reasons.runtime.LAZY_SOURCE_CHANGED
    assert type(err).__module__ == "decoy_engine.execution._errors"
    assert _chunked_input.lazy_source_changed is lazy_source_changed
    assert _lazy_csv.lazy_source_changed is lazy_source_changed


def test_the_lazy_csv_source_depends_on_no_execution_module() -> None:
    """The package root loads `execution` for every import, so `sys.modules` cannot show the
    layering; the module-level import statements do. `profile/_lazy_csv.py` and the leaf it
    takes its error from import nothing under `decoy_engine.execution`."""
    import ast

    src = Path(__file__).resolve().parents[3] / "src" / "decoy_engine"
    for rel in ("profile/_lazy_csv.py", "_execution_error.py"):
        tree = ast.parse((src / rel).read_text(encoding="utf-8"))
        names = [
            n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0
        ] + [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in names if m.startswith("decoy_engine.execution")], rel
    leaf = (src / "_execution_error.py").read_text(encoding="utf-8")
    assert "from decoy_engine.execution" not in leaf


def test_a_header_change_after_preflight_is_a_changed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n1,x\n")
    lazy = source(path)
    real = CsvLazySource.preflight

    def rewrite_after(self: CsvLazySource) -> pa.Schema:
        schema = real(self)
        path.write_bytes(b"c,d,e\n1,x,y\n")
        return schema

    monkeypatch.setattr(CsvLazySource, "preflight", rewrite_after)
    with pytest.raises(ExecutionError) as exc:
        lazy.footer_facts()
    assert exc.value.code == reasons.runtime.LAZY_SOURCE_CHANGED


class _Chunks:
    """A chunk iterator that records `close()` and can fail after its first frame."""

    def __init__(self, fail: bool) -> None:
        self.closed = False
        self._fail = fail
        self._n = 0

    def __iter__(self) -> _Chunks:
        return self

    def __next__(self) -> pd.DataFrame:
        self._n += 1
        if self._n == 1:
            return pd.DataFrame({"a": ["1"], "b": ["x"]})
        if self._fail:
            raise RuntimeError("boom")
        if self._n == 2:
            return pd.DataFrame({"a": ["2"], "b": ["y"]})
        raise StopIteration

    def close(self) -> None:
        self.closed = True


def _fake_reader(monkeypatch: pytest.MonkeyPatch, fail: bool) -> list[_Chunks]:
    made: list[_Chunks] = []
    real = _lazy_csv.read_csv_source

    def fake(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("chunksize"):
            made.append(_Chunks(fail))
            return made[-1]
        return real(*args, **kwargs)

    monkeypatch.setattr(_lazy_csv, "read_csv_source", fake)
    return made


@pytest.mark.parametrize("op", ["footer_facts", "to_table", "iter_batches"])
def test_a_failing_read_closes_the_chunk_iterator(
    op: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n1,x\n")
    made = _fake_reader(monkeypatch, fail=True)
    lazy = source(path)
    with pytest.raises(RuntimeError):
        if op == "iter_batches":
            list(lazy.iter_batches(1))
        else:
            getattr(lazy, op)()
    assert made and all(c.closed for c in made)


def test_an_abandoned_batch_iterator_closes_the_chunk_iterator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.csv"
    path.write_bytes(b"a,b\n1,x\n")
    made = _fake_reader(monkeypatch, fail=False)
    gen: Any = source(path).iter_batches(1)
    next(gen)
    gen.close()
    assert made[0].closed
