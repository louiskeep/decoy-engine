"""`test_fk_unit_all_or_none`: the sink binds every table of a unit plus its descriptor with one
rename, so a destination holds all of a unit's tables or none of them (plan 4.3, 7.2; class 5).

The FK shapes of plan 4.3 (single, composite, self-referencing, a parent/child/grandchild
chain, and children staged in a later batch than their parent) at the sink, and the parent and
child job on the sequential and out-of-core routes. Each is committed, refused (the target
already holds another result) or SIGKILLed mid-stage in a `spawn` child. FK closure inside a
complete unit is the producer's (F06); F22 claims only all or none.

Characterization: green before c1b for the commit and SIGKILL cells, since the plain rename
was already one rename for the unit; the refused cell changes from a raw `OSError` to a
refusal with the prior unit untouched. Paired with the mutation "one rename per table".
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.publication import PublicationRefused
from tests.unit.execution.publication._sink_support import (
    DESCRIPTOR,
    POLICIES,
    sink_factory,
    stages,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX durable publish")

KILL_MARKER = "killed-mid-stage"


def _shape(name: str, seed: str) -> list[tuple[str, list[pa.Table]]]:
    """Tables in FK-topological order; each entry is written as one or more batches."""
    tag = pa.array([f"{seed}-{i}" for i in range(4)], pa.string())
    if name == "single":
        return [
            ("parent", [pa.table({"id": [1, 2, 3, 4], "tag": tag})]),
            ("child", [pa.table({"cid": [1, 2, 3, 4], "pid": [1, 1, 2, 4], "tag": tag})]),
        ]
    if name == "composite":
        return [
            ("parent", [pa.table({"a": [1, 1, 2, 2], "b": [1, 2, 1, 2], "tag": tag})]),
            ("child", [pa.table({"pa": [1, 2, 2, 1], "pb": [2, 1, 2, 1], "tag": tag})]),
        ]
    if name == "self_referencing":
        return [("employees", [pa.table({"id": [1, 2, 3, 4], "manager": [None, 1, 1, 2]})])]
    if name == "chain":
        return [
            ("parent", [pa.table({"id": [1, 2, 3, 4], "tag": tag})]),
            ("child", [pa.table({"id": [10, 11, 12, 13], "pid": [1, 2, 3, 4]})]),
            ("grandchild", [pa.table({"id": [7, 8, 9, 6], "cid": [10, 10, 11, 13], "tag": tag})]),
        ]
    child = pa.table({"cid": [1, 2, 3, 4], "pid": [4, 3, 2, 1], "tag": tag})
    return [  # later_batch: the child arrives in two batches after the parent
        ("parent", [pa.table({"id": [1, 2, 3, 4], "tag": tag})]),
        ("child", [child.slice(0, 2), child.slice(2, 2)]),
    ]


SHAPES = ["single", "composite", "self_referencing", "chain", "later_batch"]


def _write(sink: Any, name: str, parts: list[pa.Table]) -> None:
    if len(parts) == 1:
        sink.write(name, parts[0])
        return

    def batches() -> Iterator[pa.RecordBatch]:
        for part in parts:
            yield from part.to_batches()

    sink.write_batches(name, batches(), schema=parts[0].schema)


def _listing(leaf: Path) -> list[str]:
    return sorted(os.listdir(leaf))


def _expected(shape: list[tuple[str, list[pa.Table]]]) -> list[str]:
    return sorted([DESCRIPTOR, *(f"{name}.parquet" for name, _ in shape)])


def _kill_child(spec_path: str) -> None:
    """Stage every table of the shape, SIGKILLed while staging the last one."""
    spec = json.loads(Path(spec_path).read_text("utf-8"))
    base = Path(spec["base"])
    shape = _shape(spec["shape"], "A")
    last = shape[-1][0]
    with sink_factory(spec["policy"], base) as make:
        sink = make()
        real = sink._writer._stored

        def stored(table: str, txn: Any, component: str) -> None:
            if table == last:
                (base.parent / KILL_MARKER).write_text("x", encoding="utf-8")
                os.kill(os.getpid(), signal.SIGKILL)
            real(table, txn, component)

        sink._writer._stored = stored
        for name, parts in shape:
            _write(sink, name, parts)
        sink.commit()


def _spawn(target: Any, spec: dict[str, Any], tmp: Path) -> int:
    path = tmp / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    proc = multiprocessing.get_context("spawn").Process(target=target, args=(str(path),))
    proc.start()
    proc.join(120)
    if proc.is_alive():  # pragma: no cover - a hung child is a failure, not a pass
        proc.kill()
        proc.join()
        raise AssertionError("child did not finish")
    return proc.exitcode if proc.exitcode is not None else -1


@pytest.mark.parametrize("shape_name", SHAPES)
@pytest.mark.parametrize("policy", POLICIES)
def test_fk_unit_all_or_none_commit(policy: str, shape_name: str, tmp_path: Path) -> None:
    shape = _shape(shape_name, "A")
    with sink_factory(policy, tmp_path) as make:
        sink = make()
        for name, parts in shape:
            _write(sink, name, parts)
        sink.commit()
    leaf = tmp_path / "out"
    assert _listing(leaf) == _expected(shape)
    for name, parts in shape:
        assert pq.read_table(leaf / f"{name}.parquet").equals(pa.concat_tables(parts)), name


@pytest.mark.parametrize("shape_name", SHAPES)
@pytest.mark.parametrize("policy", POLICIES)
def test_fk_unit_all_or_none_refused(policy: str, shape_name: str, tmp_path: Path) -> None:
    """The target holds another result: none of this unit's tables reach it."""
    leaf = tmp_path / "out"
    with sink_factory(policy, tmp_path) as make:
        prior = make()
        prior.write("prior", pa.table({"x": [1]}))
        prior.commit()
        before = {p: (leaf / p).read_bytes() for p in _listing(leaf)}
        sink = make()
        shape = _shape(shape_name, "A")
        for name, parts in shape:
            _write(sink, name, parts)
        with pytest.raises(PublicationRefused) as info:
            sink.commit()
    assert info.value.code == "publication_target_occupied"
    assert {p: (leaf / p).read_bytes() for p in _listing(leaf)} == before
    assert all(f"{name}.parquet" not in before for name, _ in shape)  # non-vacuity
    assert stages(tmp_path) == []


@pytest.mark.parametrize("shape_name", SHAPES)
@pytest.mark.parametrize("policy", POLICIES)
def test_fk_unit_all_or_none_killed_mid_stage(policy: str, shape_name: str, tmp_path: Path) -> None:
    base = tmp_path / "root"
    base.mkdir()
    spec = {"base": str(base), "policy": policy, "shape": shape_name}
    assert _spawn(_kill_child, spec, tmp_path) == -signal.SIGKILL
    assert (tmp_path / KILL_MARKER).exists()  # non-vacuity: killed while staging
    assert not (base / "out").exists()


# -- routes ---------------------------------------------------------------------------------


def _route_job(tmp: Path, mode: str, sink: Any) -> Any:
    import tests.acceptance.test_f06_route_parity as f06
    from decoy_engine.keyprovider import SecretKeyProvider
    from tests.acceptance._f06_support import run

    return run(
        f06._fk_cfg(tmp, endpoints=False),
        f06._fk_sources(),
        execution_mode=mode,
        sink=sink,
        key_provider=SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
    )


def _route_kill_child(spec_path: str) -> None:
    """The route job, SIGKILLed when the sink stages its second table."""
    from decoy_engine.execution._transactional_sink import StagedParquetWriter

    spec = json.loads(Path(spec_path).read_text("utf-8"))
    base = Path(spec["base"])
    real = StagedParquetWriter._stored
    seen: list[str] = []

    def stored(self: Any, table: str, txn: Any, component: str) -> None:
        seen.append(table)
        if len(seen) == 2:
            (base.parent / KILL_MARKER).write_text("x", encoding="utf-8")
            os.kill(os.getpid(), signal.SIGKILL)
        real(self, table, txn, component)

    StagedParquetWriter._stored = stored  # type: ignore[method-assign]
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink

    _route_job(base, spec["mode"], ParquetTransactionalSink(base / "out"))


ROUTE_MODES = ["sequential", "out_of_core"]


@pytest.mark.parametrize("mode", ROUTE_MODES)
def test_fk_unit_all_or_none_route_commit_and_refused(mode: str, tmp_path: Path) -> None:
    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink

    leaf = tmp_path / "out"
    _route_job(tmp_path, mode, ParquetTransactionalSink(leaf))
    assert _listing(leaf) == sorted([DESCRIPTOR, "child.parquet", "parent.parquet"])
    other = tmp_path / "other"
    other.mkdir()
    (other / "prior.txt").write_text("occupant", encoding="utf-8")
    with pytest.raises(PublicationRefused):
        _route_job(tmp_path, mode, ParquetTransactionalSink(other))
    assert _listing(other) == ["prior.txt"]
    assert stages(tmp_path) == []


@pytest.mark.parametrize("mode", ROUTE_MODES)
def test_fk_unit_all_or_none_route_killed_mid_stage(mode: str, tmp_path: Path) -> None:
    base = tmp_path / "root"
    base.mkdir()
    assert _spawn(_route_kill_child, {"base": str(base), "mode": mode}, tmp_path) == (
        -signal.SIGKILL
    )
    assert (tmp_path / KILL_MARKER).exists()  # non-vacuity: killed staging the second table
    assert not (base / "out").exists()
