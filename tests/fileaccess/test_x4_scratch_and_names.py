"""AT-X4: config-derived scratch names are opaque, and shipped-name resolvers check the
registry before any file-system call."""

from __future__ import annotations

import builtins
import os
import shutil
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import _runner as runner_mod
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.plan._types import TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from tests.parity.test_out_of_core_fk_parity import _plan, _seed_for

_REG = get_default_registry()
WEIRD = ["/abs", "../up", "a/b", ".", "..", "é" * 300]


# -- the native pool-quality spool ------------------------------------------------------------


def test_the_pool_spool_name_is_opaque_and_the_site_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.native import _pool_quality as module
    from tests.native.test_pool_quality import _measure, _pool

    seen: list[Path] = []
    real = module.pq.write_table

    def spy(table: Any, path: Any, *args: Any, **kwargs: Any) -> None:
        seen.append(Path(path))
        real(table, path, *args, **kwargs)

    monkeypatch.setattr(module.pq, "write_table", spy)
    work = tmp_path / "scratch"
    work.mkdir()
    for column in WEIRD:
        _measure(column, ["a", "b"], ["x", "y"], _pool(size=10, distinct_count=10), work)
    assert len(seen) == len(WEIRD), "the spool site did not execute for every column"
    for path in seen:
        assert path.parent == work
        assert path.name.startswith("pairs_") and path.name.endswith(".parquet")
        for column in WEIRD:
            assert column not in path.name
    assert list(work.iterdir()) == [], "the spool is removed"


# -- out-of-core scratch confinement ----------------------------------------------------------


def _jobs(parent: str, child1: str, child2: str, k1: str, k2: str, f1: str, f2: str):  # type: ignore[no-untyped-def]
    seed = _seed_for("hash", "ns_x")
    plan = _plan(
        (
            (
                parent,
                TableSeed(per_column=((k1, seed), (k2, _seed_for("hash", "ns_y"))), per_group=()),
            ),
            (child1, TableSeed(per_column=((f1, seed),), per_group=())),
            (child2, TableSeed(per_column=((f2, _seed_for("hash", "ns_y")),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table=parent,
                parent_columns=(k1,),
                child_table=child1,
                child_columns=(f1,),
                namespace="ns_x",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
            RelationshipEdge(
                parent_table=parent,
                parent_columns=(k2,),
                child_table=child2,
                child_columns=(f2,),
                namespace="ns_y",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    sources = {
        parent: pa.table({k1: [f"a{i}" for i in range(6)], k2: [f"b{i}" for i in range(6)]}),
        child1: pa.table({f1: ["a0", "a1", "a1", "a5", None, "a9"]}),
        child2: pa.table({f2: ["b3", "b3", "b0", None, "b9"]}),
    }
    return plan, sources, graph


PLAIN = ("parent", "child1", "child2", "k1", "k2", "f1", "f2")


def _weird_names(base: str) -> tuple[str, ...]:
    return (base, f"{base}1", f"{base}2", f"{base}k1", f"{base}k2", f"{base}f1", f"{base}f2")


class _Probe:
    """Records every directory or file the job creates through the Python calls the scratch
    code uses, so a path outside the scratch root can be named."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths: list[Path] = []
        record = self.paths.append
        real_mkdir, real_makedirs, real_os_mkdir = Path.mkdir, os.makedirs, os.mkdir
        real_write, real_writer = pq.write_table, pq.ParquetWriter

        def mkdir(path: Path, *a: Any, **k: Any) -> None:
            record(Path(path))
            return real_mkdir(path, *a, **k)

        def makedirs(name: Any, *a: Any, **k: Any) -> None:
            record(Path(name))
            return real_makedirs(name, *a, **k)

        def os_mkdir(path: Any, *a: Any, **k: Any) -> None:
            record(Path(path))
            return real_os_mkdir(path, *a, **k)

        def write_table(table: Any, where: Any, *a: Any, **k: Any) -> None:
            record(Path(where))
            return real_write(table, where, *a, **k)

        class Writer(real_writer):  # type: ignore[misc, valid-type]
            def __init__(self, where: Any, *a: Any, **k: Any) -> None:
                record(Path(where))
                super().__init__(where, *a, **k)

        monkeypatch.setattr(Path, "mkdir", mkdir)
        monkeypatch.setattr(os, "makedirs", makedirs)
        monkeypatch.setattr(os, "mkdir", os_mkdir)
        monkeypatch.setattr(pq, "write_table", write_table)
        monkeypatch.setattr(pq, "ParquetWriter", Writer)


def _listing(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


@pytest.mark.parametrize("base", WEIRD)
def test_scratch_stays_inside_its_root_and_the_output_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    driver = {"resident": 0, "reorder": 0}
    for attr, key in (("_stream_table", "resident"), ("stream_table", "reorder")):
        real = getattr(runner_mod, attr)

        def counted(*a: Any, _real: Any = real, _key: str = key, **k: Any) -> Any:
            driver[_key] += 1
            return _real(*a, **k)

        monkeypatch.setattr(runner_mod, attr, counted)

    plan, sources, graph = _jobs(*PLAIN)
    plain_out = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=tmp_path / "plain-work"
    ).outputs

    outer = tmp_path / "outer"
    outer.mkdir()
    work = outer / "work"
    before = _listing(tmp_path)
    probe = _Probe(monkeypatch)
    weird = _weird_names(base)
    plan2, sources2, graph2 = _jobs(*weird)
    result = run_fk_out_of_core(
        plan2, sources2, registry=_REG, relationship_graph=graph2, temp_dir=work
    )
    assert driver["resident"] >= 3 and driver["reorder"] == 0
    # Nothing was created beside the scratch root, and every probed path is beneath it.
    assert {p for p in _listing(tmp_path) - before if not p.startswith("outer/work")} == set()
    for path in probe.paths:
        resolved = Path(os.path.abspath(path))
        assert str(resolved).startswith(str(work)), f"{path} is outside the scratch root"
    # No scratch name is derived from a logical name.
    for entry in _listing(work):
        for name in weird:
            assert name not in entry, entry
    # Same output as the plain-names job once the logical names are mapped.
    for plain_name, weird_name in zip(PLAIN[:3], weird[:3], strict=True):
        assert list(plain_out[plain_name].to_pydict().values()) == list(
            result.outputs[weird_name].to_pydict().values()
        )


@pytest.mark.parametrize("base", ["../up", "/abs", "a/b", ".."])
def test_the_reorder_driver_names_nothing_from_a_column_or_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base: str
) -> None:
    calls = {"reorder": 0}
    real = runner_mod.stream_table

    def counted(*a: Any, **k: Any) -> Any:
        calls["reorder"] += 1
        return real(*a, **k)

    monkeypatch.setattr(runner_mod, "stream_table", counted)
    names = ("parent", "child1", "child2", *_weird_names(base)[3:])
    plan, sources, graph = _jobs(*names)
    src = tmp_path / "src"
    src.mkdir()
    lazy = {}
    for table, data in sources.items():
        pq.write_table(data, src / f"{table}.parquet")
        lazy[table] = LazySource(src / f"{table}.parquet")
    outer = tmp_path / "outer"
    outer.mkdir()
    work = outer / "work"
    before = _listing(tmp_path)
    probe = _Probe(monkeypatch)
    run_fk_out_of_core(
        plan,
        lazy,
        registry=_REG,
        relationship_graph=graph,
        sink=ParquetTransactionalSink(tmp_path / "out"),
        temp_dir=work,
        budget_bytes=1 << 30,
        temp_disk_budget_bytes=200 << 30,
        out_of_core_reorder_threshold_rows=0,
    )
    assert calls["reorder"] >= 1, "the reorder driver did not run"
    created = {p for p in _listing(tmp_path) - before if not p.startswith(("outer/work", "out"))}
    assert created == set()
    for path in probe.paths:
        resolved = Path(os.path.abspath(path))
        assert str(resolved).startswith((str(work), str(tmp_path / "out"))), path
    for entry in _listing(work):
        for name in names[3:]:
            assert name not in entry, entry


def test_relations_sharing_one_parent_do_not_collide(tmp_path: Path) -> None:
    plan, sources, graph = _jobs(*PLAIN)
    work = tmp_path / "work"
    result = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, temp_dir=work
    )
    # Both children resolve against their own relation of the shared parent.
    assert result.outputs["child1"].num_rows == 6 and result.outputs["child2"].num_rows == 5


def test_the_isolated_source_scratch_names_are_opaque(tmp_path: Path) -> None:
    from decoy_engine.execution._isolated_run import _write_payload

    work = tmp_path / "work"
    work.mkdir()
    staging = tmp_path / "staging"
    _write_payload(
        work_root=work,
        config={},
        sources={"../x": pa.table({"a": [1]}), "/abs": pa.table({"a": [2]})},
        run_pipeline_kwargs={},
        mem_cap_bytes=None,
        rlimit_kind="data",
        staging_output_dir=staging,
    )
    files = sorted(p.name for p in (work / "sources").iterdir())
    assert all(name.startswith("s") and name.endswith(".parquet") for name in files)
    assert not (tmp_path / "x.parquet").exists()


# -- shipped-name resolution ------------------------------------------------------------------


class _FsCalls:
    """Records any file-system call made inside the block."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        for module, name in (
            (os, "stat"),
            (os, "lstat"),
            (os, "open"),
            (os, "scandir"),
            (os, "listdir"),
            (builtins, "open"),
            (shutil, "copyfile"),
            (pq, "read_table"),
            (pq, "ParquetFile"),
        ):
            real = getattr(module, name)

            def wrapper(*a: Any, _real: Any = real, _name: str = name, **k: Any) -> Any:
                self.calls.append(f"{_name}{a[:1]}")
                return _real(*a, **k)

            monkeypatch.setattr(module, name, wrapper)


def _bad_names(tmp_path: Path) -> list[str]:
    real = tmp_path / "real_corpus"
    pq.write_table(pa.table({"code": ["a", "b"]}), str(real) + ".parquet")
    return [str(real), "../x", "a/b", "icd10.parquet", "icd10/", "ICD10", ""]


def test_shipped_corpus_resolvers_reject_unknown_names_without_a_file_system_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.transforms import _codeset_loader as loader
    from decoy_engine.transforms.code_set import CodeSetConfig, resolve_corpus_record

    loader.load_corpus("icd10")  # warm the cache: the warm path must be membership-first too
    names = _bad_names(tmp_path)
    resolvers = {
        "load_corpus": lambda n: loader.load_corpus(n),
        "load_corpus_provenance": lambda n: loader.load_corpus_provenance(n),
        "_get_corpus_record": lambda n: loader._get_corpus_record(n, None, is_shipped=True),
        "resolve_corpus_record": lambda n: resolve_corpus_record(CodeSetConfig(code_set=n)),
    }
    probe = _FsCalls(monkeypatch)
    for label, resolve in resolvers.items():
        for name in names:
            with pytest.raises(PlanCompileError) as info:
                resolve(name)
            assert info.value.code == "code_set_corpus_not_found", (label, name)
            assert name not in str(info.value) or name == "", (label, name)
            assert str(tmp_path) not in str(info.value)
    assert probe.calls == [], probe.calls


def test_the_unknown_corpus_message_is_fixed() -> None:
    from decoy_engine.transforms._codeset_loader import load_corpus

    messages = set()
    for name in ("nope", "../nope", "other"):
        with pytest.raises(PlanCompileError) as info:
            load_corpus(name)
        messages.add(str(info.value))
    assert len(messages) == 1


def test_shipped_names_still_load() -> None:
    from decoy_engine.transforms._codeset_loader import load_corpus, load_corpus_provenance

    assert load_corpus("icd10")
    assert load_corpus_provenance("icd10") is not None


def test_reference_tables_check_membership_before_any_file_system_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.reference_tables import load_table

    names = _bad_names(tmp_path)
    probe = _FsCalls(monkeypatch)
    for name in names:
        with pytest.raises(FileNotFoundError):
            load_table(name)
    assert probe.calls == [], probe.calls
