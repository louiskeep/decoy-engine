"""AT-30 and AT-33: the loader and post-transform baseline tee (plan 4.3).

A table the sequential route reads through a `source_loader`, or transforms as it loads, has no
resident copy the gate can read again. The route's own read is copied once, key and cast columns
only, into scratch Parquet, and the gate takes its baseline from that copy. All values are made up.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import fileaccess, run_pipeline
from decoy_engine.errors import PipelineValidationError
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.fileaccess import RootedFileAccess
from decoy_engine.profile import LazySource
from decoy_engine.verification.postmask import PostScanBlockedError
from tests._declared_schema import declare_rest, file_schemas
from tests.unit.execution._transform_testkit import ENGINE_VERSION, fk_config, fk_tables
from tests.unit.execution.post_scan.test_staged_gate import _cast_job, _keys_by_table

DUPLICATES = "post_scan.primary_key_duplicates_introduced"
FILTER_PARENTS = [{"op": "filter", "expression": "n >= 2"}]


class _Routes:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def on_event(self, event: Any) -> None:
        if event.kind == "route":
            self.seen.append(event.route)


def _tables() -> tuple[pa.Table, pa.Table]:
    """Twelve parents; twenty-four children, four of which name a parent that never existed."""
    parent, child = fk_tables()
    ids = [f"p{i % 12}" if i % 6 else f"gone{i}" for i in range(24)]
    return parent, child.set_column(1, "parent_id", pa.array(ids))


def _job(tmp_path: Path, **kw: Any) -> tuple[dict[str, Any], pa.Table, pa.Table]:
    parent, child = _tables()
    cfg = fk_config(tmp_path, parent, child, **kw)
    return declare_rest(cfg, {"parent": parent, "child": child}), parent, child


def _break_parents(cfg: dict[str, Any]) -> None:
    """Redact the parent key to one constant: every key the inputs held once is now a duplicate."""
    parent = next(t for t in cfg["tables"] if t["name"] == "parent")
    parent["columns"] = [
        {"name": "id", "strategy": "redact"},
        *(c for c in parent["columns"] if c["name"] != "id"),
    ]


def _resident(cfg: dict[str, Any], parent: pa.Table, child: pa.Table) -> tuple[Any, list[str]]:
    routes = _Routes()
    result = run_pipeline(
        cfg,
        {"parent": parent, "child": child},
        engine_version=ENGINE_VERSION,
        execution_mode="sequential",
        progress=routes,
    )
    return result, routes.seen


def _lazy(cfg: dict[str, Any]) -> dict[str, Any]:
    return {n: LazySource(Path(cfg["sources"][n]["path"])) for n in ("parent", "child")}


def _loaded(
    cfg: dict[str, Any], loads: list[str] | None = None, mode: str = "sequential"
) -> tuple[Any, list[str]]:
    """A run whose tables come only through a `source_loader`."""
    routes = _Routes()

    def loader(name: str) -> pa.Table:
        if loads is not None:
            loads.append(name)
        return pq.read_table(cfg["sources"][name]["path"])

    result = run_pipeline(
        cfg,
        {},
        engine_version=ENGINE_VERSION,
        execution_mode=mode,
        source_loader=loader,
        source_loader_schemas=file_schemas(cfg),
        progress=routes,
    )
    return result, routes.seen


def _keys(result: Any) -> list[dict[str, Any]]:
    report = result.quality_metrics["post_scan_integrity"]
    return sorted(report["keys"], key=lambda k: (k["table"], k["kind"]))


def _blocked(call: Callable[[], Any]) -> PostScanBlockedError:
    with pytest.raises(PostScanBlockedError) as caught:
        call()
    return caught.value


# ----- AT-30: source_loader on the sequential route --------------------------------------------


def test_at30_the_tee_baseline_equals_the_resident_baseline(tmp_path: Path) -> None:
    cfg, parent, child = _job(tmp_path)
    expected, _ = _resident(cfg, parent, child)
    teed, routes = _loaded(cfg)
    assert routes == ["sequential"]
    assert "post_scan_gate_deferred" not in teed.quality_metrics
    assert _keys(teed) == _keys(expected)
    (fk,) = (k for k in _keys(teed) if k["kind"] == "foreign")
    assert fk["baseline"] == "measured"
    assert fk["source"]["orphan_rows"] == 4  # the inherited orphans are in the baseline
    assert fk["output"]["orphan_rows"] == 4
    assert teed.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False


def test_at30_the_loader_is_called_once_per_table(tmp_path: Path) -> None:
    cfg, _, _ = _job(tmp_path)
    loads: list[str] = []
    result, _ = _loaded(cfg, loads)
    assert sorted(loads) == ["child", "parent"]
    assert result.quality_metrics["post_scan_integrity"]["keys"]


def test_at30_a_key_the_run_breaks_blocks_through_the_tee(tmp_path: Path) -> None:
    cfg, _, _ = _job(tmp_path)
    _break_parents(cfg)
    loads: list[str] = []
    exc = _blocked(lambda: _loaded(cfg, loads))
    assert exc.reason == DUPLICATES
    assert sorted(loads) == ["child", "parent"]


def test_at30_the_tee_copies_only_the_key_columns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, _, _ = _job(tmp_path)
    seen: list[tuple[str, list[str]]] = []
    real = _post_scan.check_keys

    def spy(rels: Any, sources: Any, outputs: Any, **kw: Any) -> Any:
        seen.extend((name, list(table.schema.names)) for name, table in sources.items())
        return real(rels, sources, outputs, **kw)

    monkeypatch.setattr(_post_scan, "check_keys", spy)
    _loaded(cfg)
    assert sorted(seen) == [("child", ["parent_id"]), ("parent", ["id"])]


def test_at30_a_route_the_tee_cannot_wrap_stays_report_only(tmp_path: Path) -> None:
    cfg, _, _ = _job(tmp_path)
    result, routes = _loaded(cfg, mode="full_frame")
    assert routes == ["full_frame"]
    assert result.quality_metrics["post_scan_gate_deferred"] == {
        "namespace": "post_scan",
        "code": "gate_deferred_route",
    }


def test_at30_a_loader_table_without_the_key_columns_never_reaches_a_route(
    tmp_path: Path,
) -> None:
    """Declared columns are checked against the loader's schemas before any route is chosen, so
    the tee never meets a table that lacks a key column."""
    cfg, _, _ = _job(tmp_path)
    schemas = file_schemas(cfg)
    schemas["child"] = pa.schema([f for f in schemas["child"] if f.name != "parent_id"])
    loads: list[str] = []
    with pytest.raises(PipelineValidationError, match="declared_column_missing"):
        run_pipeline(
            cfg,
            {},
            engine_version=ENGINE_VERSION,
            execution_mode="auto",
            source_loader=lambda name: loads.append(name) or pq.read_table(""),
            source_loader_schemas=schemas,
        )
    assert loads == []


# ----- AT-33: a transform-bearing parent -------------------------------------------------------


def test_at33_a_filtered_parent_has_the_same_baseline_on_resident_and_sequential(
    tmp_path: Path,
) -> None:
    cfg, parent, child = _job(tmp_path, parent_transforms=FILTER_PARENTS)
    expected, _ = _resident(cfg, parent, child)
    routes = _Routes()
    lazy = run_pipeline(
        cfg,
        _lazy(cfg),
        engine_version=ENGINE_VERSION,
        execution_mode="sequential",
        progress=routes,
    )
    assert routes.seen == ["sequential"]
    assert "post_scan_gate_deferred" not in lazy.quality_metrics
    assert _keys(lazy) == _keys(expected)
    report = lazy.quality_metrics["post_scan_integrity"]
    assert report["verdict"] == expected.quality_metrics["post_scan_integrity"]["verdict"]
    assert report["verdict"]["blocking"] is False
    (fk,) = (k for k in _keys(lazy) if k["kind"] == "foreign")
    # p0 and p1 left the parent table before the check: their children are in the baseline.
    assert fk["source"]["orphan_rows"] == fk["output"]["orphan_rows"] > 4


def test_at33_a_loaded_filtered_parent_is_baselined_after_its_transforms(tmp_path: Path) -> None:
    cfg, parent, child = _job(tmp_path, parent_transforms=FILTER_PARENTS)
    expected, _ = _resident(cfg, parent, child)
    loads: list[str] = []
    result, routes = _loaded(cfg, loads)
    assert routes == ["sequential"]
    assert sorted(loads) == ["child", "parent"]
    assert _keys(result) == _keys(expected)


def test_at33_a_key_the_run_breaks_still_blocks_after_a_transform(tmp_path: Path) -> None:
    cfg, _, _ = _job(tmp_path, parent_transforms=FILTER_PARENTS)
    _break_parents(cfg)
    exc = _blocked(
        lambda: run_pipeline(
            cfg, _lazy(cfg), engine_version=ENGINE_VERSION, execution_mode="sequential"
        )
    )
    assert exc.reason == DUPLICATES


# ----- the tee's scratch --------------------------------------------------------------------


@pytest.fixture
def teed_files(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, int]]:
    """Each tee file's directory and mode, taken right after it is written."""
    seen: list[tuple[Path, int]] = []
    real = _post_scan.PostScanGate._tee

    def spy(self: Any, name: str, table: pa.Table, columns: tuple[str, ...]) -> None:
        real(self, name, table, columns)
        path = self._teed[name].path
        seen.append((path, path.stat().st_mode & 0o777))

    monkeypatch.setattr(_post_scan.PostScanGate, "_tee", spy)
    return seen


def test_the_tee_files_are_private_and_removed_after_a_pass(
    tmp_path: Path, teed_files: list[tuple[Path, int]]
) -> None:
    cfg, _, _ = _job(tmp_path)
    _loaded(cfg)
    assert len(teed_files) == 2
    assert {mode for _, mode in teed_files} == {0o600}
    assert not any(path.parent.exists() for path, _ in teed_files)


def test_the_tee_directory_is_removed_after_a_block(
    tmp_path: Path, teed_files: list[tuple[Path, int]]
) -> None:
    cfg, _, _ = _job(tmp_path)
    _break_parents(cfg)
    _blocked(lambda: _loaded(cfg))
    assert teed_files
    assert not any(path.parent.exists() for path, _ in teed_files)


def test_a_loader_run_completes_under_a_strict_file_policy(tmp_path: Path) -> None:
    """The tee writes into the engine's own scratch directory, which every policy allows."""
    cfg, parent, child = _job(tmp_path)
    tables = {"parent": parent, "child": child}
    schemas = {name: table.schema for name, table in tables.items()}
    # A file descriptor would be a read the strict policy denies, so the tables are declared
    # as caller sources; the route still reads them through the loader.
    cfg.pop("sources")
    cfg.pop("targets")
    routes = _Routes()
    with fileaccess.scope(RootedFileAccess([], allow_portable_fallback=True)):
        result = run_pipeline(
            cfg,
            tables,
            engine_version=ENGINE_VERSION,
            execution_mode="sequential",
            source_loader=lambda name: tables[name],
            source_loader_schemas=schemas,
            progress=routes,
        )
    assert routes.seen == ["sequential"]
    assert "post_scan_gate_deferred" not in result.quality_metrics
    assert result.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False


# ----- one decision on what the route read ----------------------------------------------------


def test_a_loader_that_disagrees_with_sources_is_baselined_as_the_route_read_it(
    tmp_path: Path,
) -> None:
    """The route reads the loader, not `sources`; the baseline must be the loader's table."""
    cfg, parent, child = _job(tmp_path)
    dup_ids = parent.column("id").to_pylist()
    dup_ids[1] = dup_ids[0]
    served = parent.set_column(0, "id", pa.array(dup_ids))

    def loader(name: str) -> pa.Table:
        return served if name == "parent" else child

    routes = _Routes()
    result = run_pipeline(
        cfg,
        {"parent": parent, "child": child},
        engine_version=ENGINE_VERSION,
        execution_mode="sequential",
        source_loader=loader,
        source_loader_schemas={"parent": served.schema, "child": child.schema},
        progress=routes,
    )
    assert routes.seen == ["sequential"]
    (pk,) = (k for k in _keys(result) if k["kind"] == "primary")
    assert pk["source"]["duplicate_excess"] == 1  # the loader's copy, not the caller's
    assert result.quality_metrics["post_scan_integrity"]["verdict"]["blocking"] is False


# ----- a declared cast on a teed key ------------------------------------------------------------


def test_a_cast_key_column_is_teed_raw_and_cast_on_the_re_read(tmp_path: Path) -> None:
    from tests.acceptance._f06_support import run

    cfg, data = _cast_job(tmp_path)
    resident = run(cfg, data, execution_mode="full_frame")
    teed = run(
        cfg,
        {},
        execution_mode="sequential",
        source_loader=lambda name: data[name],
        source_loader_schemas={n: t.schema for n, t in data.items()},
    )
    assert "post_scan_gate_deferred" not in teed.quality_metrics
    want, got = _keys_by_table(resident), _keys_by_table(teed)
    for table in ("parent", "child"):
        assert got[table]["baseline"] == want[table]["baseline"] == "measured"
        assert got[table]["source"] == want[table]["source"]
    assert got["parent"]["source"]["duplicate_excess"] == 1  # "01" and "1" are one key


# ----- where the tee cannot run, and where the run fails -------------------------------------


def test_a_host_without_the_scratch_flags_keeps_the_deferral(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scratch writer needs POSIX descriptor flags; without them the run is deferred, as it
    was before the tee, and is not refused."""
    for flag in ("O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY"):
        monkeypatch.delattr(os, flag, raising=False)
    assert _post_scan.tee_supported() is False
    cfg, _, _ = _job(tmp_path)
    result, routes = _loaded(cfg)
    assert routes == ["sequential"]
    assert result.quality_metrics["post_scan_gate_deferred"] == {
        "namespace": "post_scan",
        "code": "gate_deferred_route",
    }
    assert "post_scan_integrity" not in result.quality_metrics


def test_the_tee_directory_is_removed_when_the_loader_fails_mid_route(
    tmp_path: Path, teed_files: list[tuple[Path, int]]
) -> None:
    cfg, _, _ = _job(tmp_path)
    seen: list[str] = []

    def loader(name: str) -> pa.Table:
        seen.append(name)
        if len(seen) == 2:
            raise RuntimeError("loader failed")
        return pq.read_table(cfg["sources"][name]["path"])

    with pytest.raises(RuntimeError, match="loader failed"):
        run_pipeline(
            cfg,
            {},
            engine_version=ENGINE_VERSION,
            execution_mode="sequential",
            source_loader=loader,
            source_loader_schemas=file_schemas(cfg),
        )
    assert len(teed_files) == 1  # the first table was teed before the second read failed
    assert not teed_files[0][0].parent.exists()
