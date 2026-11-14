"""AT-30 and AT-33: the loader and post-transform baseline tee (plan 4.3).

A table the sequential route reads through a `source_loader`, or transforms as it loads, has no
resident copy the gate can read again. The route's own read is copied once, key and cast columns
only, into scratch Parquet, and the gate takes its baseline from that copy. All values are made up.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine import run_pipeline
from decoy_engine.errors import PipelineValidationError
from decoy_engine.execution.pipeline import _post_scan
from decoy_engine.profile import LazySource
from decoy_engine.verification.postmask import PostScanBlockedError
from tests._declared_schema import declare_rest, file_schemas
from tests.unit.execution._transform_testkit import ENGINE_VERSION, fk_config, fk_tables

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
