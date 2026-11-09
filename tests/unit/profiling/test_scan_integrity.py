"""Declared-relationship integrity facts (T5 acceptance tests)."""

from __future__ import annotations

import ast
import json
import os
import stat
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess, profile, reasons
from decoy_engine.config import PipelineConfigError
from decoy_engine.fileaccess import FileAccessDenied
from decoy_engine.profiling import (
    ScanSetCore,
    ScanSetCoreRead,
    _scan_integrity,
    scan_set_to_dict,
    scan_sources,
)
from decoy_engine.profiling._scan_integrity import ChildFacts, RelationshipFacts
from decoy_engine.reason_adapter import reason_from_exception

CANARY = "CANARY-5d1e"
LAYOUT = {"columns": [{"name": "id", "start": 0, "width": 3}]}


def _csv(tmp_path: Path, name: str, text: str, **extra: Any) -> dict[str, Any]:
    path = tmp_path / f"{name}.csv"
    path.write_text(text)
    return {"type": "file", "format": "csv", "path": str(path), "has_header": True, **extra}


def _parquet(tmp_path: Path, name: str, table: pa.Table) -> dict[str, Any]:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return {"type": "file", "format": "parquet", "path": str(path)}


def _rel(parent: tuple[str, list[str]], child: tuple[str, list[str]]) -> dict[str, Any]:
    return {
        "parent": {"table": parent[0], "columns": parent[1]},
        "children": [{"table": child[0], "columns": child[1]}],
        "orphan_policy": "warn",
    }


def _facts(sources: dict[str, Any], rel: dict[str, Any], **kw: Any) -> RelationshipFacts:
    scan = scan_sources(sources, tables=[], relationships=[rel], **kw)
    assert len(scan.relationships) == 1
    return scan.relationships[0]


def test_orphans_nulls_and_distinct(tmp_path: Path) -> None:
    parent = "id\n" + "".join(f"{i}\n" for i in range(1, 101))
    child_rows = ["1", "2", "101", "101", "102", "103", "103", "", "", "", "", ""]
    # A second column keeps pandas from skipping the blank single-field lines.
    child = "pid,n\n" + "".join(f"{v},z\n" for v in child_rows)
    src = {"p": _csv(tmp_path, "p", parent), "c": _csv(tmp_path, "c", child)}
    f = _facts(src, _rel(("p", ["id"]), ("c", ["pid"])))
    assert f.status == "checked" and f.exact and f.bounded
    assert (f.parent_rows, f.parent_null_key_rows, f.parent_duplicate_excess) == (100, 0, 0)
    c = f.children[0]
    assert (c.rows, c.null_key_rows) == (12, 5)
    assert (c.orphan_rows, c.orphan_distinct_keys) == (5, 3)


def test_orphans_seven_rows_three_keys(tmp_path: Path) -> None:
    ids = pa.table({"id": pa.array(range(1, 101), pa.int64())})
    pids = [1, 2, 101, 101, 102, 102, 103, 103, 103, None, None, None, None, None]
    src = {
        "p": _parquet(tmp_path, "p", ids),
        "c": _parquet(tmp_path, "c", pa.table({"pid": pa.array(pids, pa.int64())})),
    }
    c = _facts(src, _rel(("p", ["id"]), ("c", ["pid"]))).children[0]
    assert (c.orphan_rows, c.orphan_distinct_keys, c.null_key_rows) == (7, 3, 5)


def test_composite_partial_match_is_an_orphan(tmp_path: Path) -> None:
    src = {
        "p": _csv(tmp_path, "p", "a,b\n1,x\n2,y\n"),
        "c": _csv(tmp_path, "c", "a,b\n1,x\n1,y\n2,y\n"),
    }
    c = _facts(src, _rel(("p", ["a", "b"]), ("c", ["a", "b"]))).children[0]
    assert (c.orphan_rows, c.orphan_distinct_keys) == (1, 1)


def test_duplicate_excess_counts_complete_keys_only(tmp_path: Path) -> None:
    unary = "id,n\n1,a\n1,a\n1,a\n2,a\n,a\n,a\n"
    composite = "a,b\n1,x\n1,x\n1,\n1,\n,x\n,x\n2,y\n"
    src = {
        "u": _csv(tmp_path, "u", unary),
        "k": _csv(tmp_path, "k", composite),
        "c": _csv(tmp_path, "c", "id\n1\n"),
        "d": _csv(tmp_path, "d", "a,b\n1,x\n"),
    }
    u = _facts(src, _rel(("u", ["id"]), ("c", ["id"])))
    assert (u.parent_rows, u.parent_null_key_rows, u.parent_duplicate_excess) == (6, 2, 2)
    k = _facts(src, _rel(("k", ["a", "b"]), ("d", ["a", "b"])))
    assert (k.parent_rows, k.parent_null_key_rows, k.parent_duplicate_excess) == (7, 4, 1)


def test_type_mismatch_gives_no_counts(tmp_path: Path) -> None:
    src = {
        "p": _parquet(tmp_path, "p", pa.table({"id": pa.array([1, 2], pa.int64())})),
        "c": _csv(tmp_path, "c", "pid\n1\n9\n"),
    }
    f = _facts(src, _rel(("p", ["id"]), ("c", ["pid"])))
    assert f.status == "key_type_mismatch"
    assert f.parent_rows is None and f.children[0].orphan_rows is None
    assert f.warnings == (reasons.warning.SCAN_KEY_TYPE_MISMATCH.spec.ref,)


def test_undeclared_names_infer_nothing(tmp_path: Path) -> None:
    src = {"a": _csv(tmp_path, "a", "user_id\n1\n"), "b": _csv(tmp_path, "b", "user_id\n2\n")}
    assert scan_sources(src).relationships == ()


def test_bounded_with_small_memory_limit_spills(tmp_path: Path, monkeypatch: Any) -> None:
    parent = tmp_path / "p.parquet"
    child = tmp_path / "c.parquet"
    keys = [f"{i:040d}" for i in range(1000)]
    pq.write_table(pa.table({"id": keys}), parent)
    schema = pa.schema([("pid", pa.string())])
    with pq.ParquetWriter(child, schema) as writer:
        for block in range(50):
            base = block * 100_000
            col = [f"{(base + i) % 1300:040d}" for i in range(100_000)]
            writer.write_table(pa.table({"pid": col}, schema=schema))
    src = {
        "p": {"type": "file", "format": "parquet", "path": str(parent)},
        "c": {"type": "file", "format": "parquet", "path": str(child)},
    }
    seen: dict[str, Any] = {}

    def probe(conn: Any) -> None:
        # A cursor, so the orphan query this runs inside keeps its pending result.
        files = conn.cursor().execute("SELECT path FROM duckdb_temporary_files()").fetchall()
        seen["files"] = [r[0] for r in files]
        seen["dir"] = sorted(p.name for p in spill.iterdir())

    spill = tmp_path / "spill"
    monkeypatch.setattr(_scan_integrity, "MEMORY_LIMIT", "64MB")
    monkeypatch.setattr(_scan_integrity, "_before_fetch", probe)
    f = _facts(src, _rel(("p", ["id"]), ("c", ["pid"])), spill_dir=spill)
    assert f.children[0].rows == 5_000_000
    assert f.children[0].orphan_rows > 0
    assert seen["files"] and all(str(spill) in p for p in seen["files"])
    assert seen["dir"]
    assert list(spill.iterdir()) == []


def test_csv_keys_come_through_the_central_reader_in_requested_order(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[dict[str, Any]] = []
    real = profile.read_csv_source

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(profile, "read_csv_source", spy)
    headerless = _csv(
        tmp_path, "h", "1,x,7\n2,y,8\n", has_header=False, column_names=["a", "b", "c"]
    )
    src = {"p": _csv(tmp_path, "p", "a,b,c\n1,x,7\n"), "c": headerless}
    f = _facts(src, _rel(("p", ["c", "a"]), ("c", ["c", "a"])))
    assert f.children[0].orphan_rows == 1
    chunked = [k for k in calls if "chunksize" in k]
    assert [k["columns"] for k in chunked] == [["c", "a"], ["c", "a"]]


def test_integrity_module_never_touches_pandas_csv_or_dialect_keys() -> None:
    tree = ast.parse(Path(_scan_integrity.__file__).read_text())
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    froms = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert "read_csv" not in attrs and "pandas" not in names | froms and "csv" not in names
    text = Path(_scan_integrity.__file__).read_text()
    assert "has_header" not in text and "delimiter" not in text


def test_denied_policy_stops_the_csv_key_read(tmp_path: Path) -> None:
    class Deny:
        def open_read(self, request: Any) -> Any:
            raise FileAccessDenied(request.field_key)

    src = {"p": _csv(tmp_path, "p", "id\n1\n"), "c": _csv(tmp_path, "c", "id\n1\n")}
    with pytest.raises(FileAccessDenied):
        scan_sources(
            src,
            tables=[],
            relationships=[_rel(("p", ["id"]), ("c", ["id"]))],
            file_access=Deny(),  # type: ignore[arg-type]
        )


def _small(tmp_path: Path) -> dict[str, Any]:
    return {"p": _csv(tmp_path, "p", "id\n1\n2\n"), "c": _csv(tmp_path, "c", "id\n1\n3\n")}


def test_spill_dir_is_used_and_removed_also_on_error(tmp_path: Path, monkeypatch: Any) -> None:
    spill = tmp_path / "spill"
    rel = _rel(("p", ["id"]), ("c", ["id"]))
    inside: list[list[str]] = []
    monkeypatch.setattr(
        _scan_integrity, "_before_fetch", lambda conn: inside.append(os.listdir(spill))
    )
    _facts(_small(tmp_path), rel, spill_dir=spill)
    assert inside and inside[0] and list(spill.iterdir()) == []

    def boom(conn: Any) -> None:
        raise RuntimeError(CANARY)

    monkeypatch.setattr(_scan_integrity, "_before_fetch", boom)
    with pytest.raises(Exception) as info:
        _facts(_small(tmp_path), rel, spill_dir=spill)
    assert CANARY not in str(info.value) and CANARY not in repr(info.value)
    assert list(spill.iterdir()) == []


def test_default_spill_comes_from_the_protected_allocator(tmp_path: Path, monkeypatch: Any) -> None:
    issued: list[Path] = []
    modes: list[int] = []
    real = fileaccess.protected_scratch_dir

    def spy(parent: Any = None, prefix: str = "decoy-") -> Any:
        manager = real(parent, prefix=prefix)

        class Wrap:
            def __enter__(self) -> Path:
                path = manager.__enter__()
                issued.append(path)
                return path

            def __exit__(self, *exc: Any) -> Any:
                return manager.__exit__(*exc)

        return Wrap()

    monkeypatch.setattr(fileaccess, "protected_scratch_dir", spy)
    monkeypatch.setattr(
        _scan_integrity,
        "_before_fetch",
        lambda conn: modes.append(stat.S_IMODE(os.stat(issued[0]).st_mode)),
    )
    _facts(_small(tmp_path), _rel(("p", ["id"]), ("c", ["id"])))
    assert len(issued) == 1 and modes == [0o700]
    assert issued[0].parent == Path(__import__("tempfile").gettempdir())
    assert not issued[0].exists()


def test_acl_failure_stops_before_any_key_read(tmp_path: Path, monkeypatch: Any) -> None:
    reads: list[int] = []

    def refuse(*a: Any, **k: Any) -> Any:
        raise fileaccess._protected_dir.ProtectedDirAclFailed

    monkeypatch.setattr(fileaccess, "protected_scratch_dir", refuse)
    monkeypatch.setattr(_scan_integrity, "_open_keys", lambda *a, **k: reads.append(1))
    spill = tmp_path / "spill"
    with pytest.raises(fileaccess._protected_dir.ProtectedDirAclFailed) as info:
        _facts(_small(tmp_path), _rel(("p", ["id"]), ("c", ["id"])), spill_dir=spill)
    assert reads == [] and not spill.exists()
    assert reason_from_exception(info.value).code == "scratch_acl_failed"


def test_cloud_relationship_is_skipped_without_a_read(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(
        _scan_integrity, "_open_keys", lambda *a, **k: pytest.fail("read a cloud table")
    )
    monkeypatch.setattr(
        fileaccess, "protected_scratch_dir", lambda *a, **k: pytest.fail("allocated scratch")
    )
    src = {
        "p": {"type": "s3", "format": "csv", "bucket": "b", "key": "k"},
        "c": _csv(tmp_path, "c", "id\n1\n"),
    }
    f = _facts(src, _rel(("p", ["id"]), ("c", ["id"])))
    assert f.status == "skipped_cloud" and f.parent_rows is None
    assert f.children[0] == ChildFacts("c", ("id",), None, None, None, None)


def test_facts_are_value_free(tmp_path: Path) -> None:
    src = {
        "p": _csv(tmp_path, "p", f"id\n{CANARY}-a\n{CANARY}-a\n"),
        "c": _csv(tmp_path, "c", f"id\n{CANARY}-z\n"),
    }
    scan = scan_sources(src, tables=[], relationships=[_rel(("p", ["id"]), ("c", ["id"]))])
    dumped = json.dumps(scan_set_to_dict(scan, detail=False)) + json.dumps(
        scan.relationships[0].to_dict()
    )
    assert CANARY not in dumped and CANARY not in repr(scan.relationships)
    assert (
        scan_set_to_dict(scan, detail=False)["relationships"][0]["children"][0]["orphan_rows"] == 1
    )


def test_integrity_false_reads_no_key_column(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(_scan_integrity, "_open_keys", lambda *a, **k: pytest.fail("read keys"))
    scan = scan_sources(
        _small(tmp_path), relationships=[_rel(("p", ["id"]), ("c", ["id"]))], integrity=False
    )
    assert scan.relationships == ()


@pytest.mark.parametrize(
    "rel",
    [
        {"parent": {"table": "p", "columns": ["id"]}},
        _rel(("nope", ["id"]), ("c", ["id"])),
        _rel(("p", ["id", "id"]), ("c", ["id", "id"])),
        _rel(("p", ["id"]), ("c", ["id", "x"])),
        _rel(("p", ["missing"]), ("c", ["id"])),
    ],
)
def test_bad_relationships_are_config_errors_without_values(tmp_path: Path, rel: Any) -> None:
    with pytest.raises(PipelineConfigError) as info:
        _facts(_small(tmp_path), rel)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_fixed_width_keys_are_read_whole_and_unbounded(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("001\n002\n")
    fixed = {
        "type": "file",
        "format": "fixed_width",
        "path": str(path),
        "layout": LAYOUT,
    }
    src = {"f": fixed, "c": _csv(tmp_path, "c", "id\n001\n009\n")}
    f = _facts(src, _rel(("f", ["id"]), ("c", ["id"])))
    assert f.bounded is False and f.exact is True
    assert f.parent_rows == 2
    assert f.children[0].orphan_rows == 1


def test_denied_policy_stops_every_chunk_not_just_the_first_open(tmp_path: Path) -> None:
    src = {"p": _csv(tmp_path, "p", "id\n1\n"), "c": _csv(tmp_path, "c", "id\n1\n")}
    real = fileaccess.current
    opens: list[str] = []

    class AllowOnce:
        """Allows the header probe, denies the chunked key read."""

        def __init__(self) -> None:
            self.inner = real()

        def open_read(self, request: Any) -> Any:
            opens.append(request.field_key)
            if len(opens) > 1:
                raise FileAccessDenied(request.field_key)
            return self.inner.open_read(request)

    with pytest.raises(FileAccessDenied):
        scan_sources(
            src,
            tables=[],
            relationships=[_rel(("p", ["id"]), ("c", ["id"]))],
            file_access=AllowOnce(),  # type: ignore[arg-type]
        )
    assert len(opens) >= 2


def _core_doc(tmp_path: Path) -> dict[str, Any]:
    src = {"p": _csv(tmp_path, "p", "id,n\n1,a\n"), "c": _csv(tmp_path, "c", "id,n\n2,a\n")}
    scan = scan_sources(src, tables=[], relationships=[_rel(("p", ["id"]), ("c", ["id"]))])
    return scan_set_to_dict(scan, detail=False)


def test_relationship_core_is_a_typed_versioned_read_model(tmp_path: Path) -> None:
    doc = _core_doc(tmp_path)
    ScanSetCore.model_validate(doc)
    ScanSetCoreRead.model_validate(doc)
    for model in (ScanSetCore, ScanSetCoreRead):
        bad_status = json.loads(json.dumps(doc))
        bad_status["relationships"][0]["status"] = "maybe"
        with pytest.raises(ValueError):
            model.model_validate(bad_status)
        for path in ("parent_rows", "children.0.orphan_rows"):
            bad_count = json.loads(json.dumps(doc))
            holder = bad_count["relationships"][0]
            if path.startswith("children"):
                holder = holder["children"][0]
            holder[path.split(".")[-1]] = True
            with pytest.raises(ValueError):
                model.model_validate(bad_count)
    extra = json.loads(json.dumps(doc))
    extra["relationships"][0]["children"][0]["later"] = 1
    ScanSetCoreRead.model_validate(extra)
    with pytest.raises(ValueError):
        ScanSetCore.model_validate(extra)


def test_every_relationship_status_round_trips_through_the_scan_set_core() -> None:
    # The persisted core must accept every status the integrity pass can emit, and no other.
    from typing import get_args

    from pydantic import ValidationError

    from decoy_engine.profiling._scan_integrity import Status
    from decoy_engine.profiling.scan_set import RelationshipFactsCore

    base = {
        "parent": "p",
        "parent_columns": ["id"],
        "children": [],
        "parent_rows": None,
        "parent_null_key_rows": None,
        "parent_duplicate_excess": None,
        "exact": True,
        "bounded": False,
        "warnings": [],
    }
    for status in get_args(Status):
        assert RelationshipFactsCore.model_validate({**base, "status": status}).status == status
    with pytest.raises(ValidationError):
        RelationshipFactsCore.model_validate({**base, "status": "not_a_status"})
