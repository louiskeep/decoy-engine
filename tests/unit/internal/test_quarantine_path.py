"""`run_pipeline(quarantine_path=...)`: the placed quarantine path, one cell per route.

The keyword replaces `quarantine.output_path` for the run. The plan, and so the config hash,
comes from the caller's config; the caller's dict is never touched; a non-string is refused
before any source is read.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import ExecutionError
from decoy_engine.execution import _pipeline_route_dispatch as dispatch
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.plan._compile import _hash_config
from tests._declared_schema import declare_rest

_AGE = ["10", "20", "30", "40", "50", "badX"]
_IDS = ["p0", "p1", "p2", "p3", "p4", "p5"]


def _write(tmp_path: Path, table: pa.Table, name: str) -> str:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def _single_config(tmp_path: Path, declared: str) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    table = pa.table({"cc": ["4111111111111111", "4532015112830367"]})
    src = _write(tmp_path, table, "t")
    config: dict[str, Any] = {
        "version": 2,
        "global_settings": {"job_name": "qpath-single", "seed": 42},
        "sources": {"t": {"type": "file", "path": src, "format": "parquet"}},
        "targets": {"t": {"type": "file", "path": src + ".out", "format": "parquet"}},
        "tables": [{"name": "t", "columns": [{"name": "cc", "strategy": "passthrough"}]}],
        "relationships": [],
        "validators": [{"name": "luhn", "columns": {"t": ["cc"]}}],
        "quarantine": {"enabled": True, "output_path": declared, "triggers": ["validation_fail"]},
    }
    return declare_rest(config), {"t": pq.read_table(src)}


def _fk_config(tmp_path: Path, declared: str) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table({"id": pa.array(_IDS), "age": pa.array(_AGE)})
    child = pa.table({"id": pa.array([f"c{i}" for i in range(6)]), "parent_id": pa.array(_IDS)})
    fk = {
        "name": "",
        "strategy": "faker",
        "provider": "person_email",
        "deterministic": True,
        "namespace": "parent_ns",
    }
    config: dict[str, Any] = {
        "version": 2,
        "global_settings": {"job_name": "qpath-fk", "seed": 42},
        "sources": {
            "parent": {
                "type": "file",
                "path": _write(tmp_path, parent, "parent"),
                "format": "parquet",
            },
            "child": {
                "type": "file",
                "path": _write(tmp_path, child, "child"),
                "format": "parquet",
            },
        },
        "targets": {
            "parent": {"type": "file", "path": str(tmp_path / "p.out"), "format": "parquet"},
            "child": {"type": "file", "path": str(tmp_path / "c.out"), "format": "parquet"},
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    {**fk, "name": "id"},
                    {"name": "age", "strategy": "bucketize", "provider_config": {"width": 10}},
                ],
            },
            {
                "name": "child",
                "columns": [{**fk, "name": "parent_id"}, {"name": "id", "strategy": "passthrough"}],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "parent_ns",
            }
        ],
        "quarantine": {"enabled": True, "output_path": declared, "triggers": ["format_error"]},
    }
    return declare_rest(config), {
        "parent": pq.read_table(config["sources"]["parent"]["path"]),
        "child": pq.read_table(config["sources"]["child"]["path"]),
    }


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_full_frame_route_writes_the_quarantine_file_at_the_given_path(tmp_path: Path) -> None:
    declared, placed = tmp_path / "declared.jsonl", tmp_path / "run" / "placed.jsonl"
    placed.parent.mkdir()
    config, sources = _single_config(tmp_path, str(declared))
    before = copy.deepcopy(config)
    result = run_pipeline(
        config,
        sources,
        engine_version="0.1.0",
        execution_mode="full_frame",
        quarantine_path=str(placed),
    )
    assert [r["cc"] for r in _records(placed)] == ["4532015112830367"]
    assert not declared.exists()
    assert result.quality_metrics["quarantine"]["output_path"] == str(placed)
    assert config == before


def test_sequential_route_writes_the_quarantine_file_at_the_given_path(tmp_path: Path) -> None:
    declared, placed = tmp_path / "declared.jsonl", tmp_path / "run" / "placed.jsonl"
    placed.parent.mkdir()
    config, sources = _fk_config(tmp_path, str(declared))
    before = copy.deepcopy(config)
    result = run_pipeline(
        config,
        sources,
        engine_version="0.1.0",
        execution_mode="sequential",
        quarantine_path=str(placed),
    )
    assert [r["age"] for r in _records(placed)] == ["badX"]
    assert not declared.exists()
    assert result.quality_metrics["quarantine"]["output_path"] == str(placed)
    assert config == before


def test_out_of_core_route_receives_the_given_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The out-of-core route has no row-removal channel, so it never writes quarantine rows; the
    cell pins that it is handed the placed path and the caller's dict is not the one it sees."""
    declared, placed = str(tmp_path / "declared.jsonl"), str(tmp_path / "placed.jsonl")
    config, sources = _fk_config(tmp_path, declared)
    # Strategies the out-of-core route admits: it has no row-error channel, and no faker parent.
    config["tables"][0]["columns"] = [
        {"name": "id", "strategy": "hash", "namespace": "parent_ns"},
        {"name": "age", "strategy": "passthrough"},
    ]
    config["tables"][1]["columns"] = [
        {"name": "parent_id", "strategy": "hash", "namespace": "parent_ns"}
    ]
    seen: list[dict[str, Any]] = []
    real = dispatch._route_exec.run_out_of_core_route

    def spy(**kwargs: Any) -> Any:
        seen.append(kwargs["config"])
        return real(**kwargs)

    monkeypatch.setattr(dispatch._route_exec, "run_out_of_core_route", spy)
    declare_rest(config, sources)
    before = copy.deepcopy(config)
    run_pipeline(
        config,
        sources,
        engine_version="0.1.0",
        execution_mode="out_of_core",
        quarantine_path=placed,
    )
    (route_config,) = seen
    assert route_config["quarantine"]["output_path"] == placed
    assert route_config is not config
    assert config == before


@pytest.mark.parametrize("relative", [False, True])
def test_absolute_and_relative_paths_are_passed_through_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    given = "rel.jsonl" if relative else str(tmp_path / "abs.jsonl")
    config, sources = _single_config(tmp_path, "declared.jsonl")
    result = run_pipeline(config, sources, engine_version="0.1.0", quarantine_path=given)
    assert result.quality_metrics["quarantine"]["output_path"] == given
    assert (tmp_path / ("rel.jsonl" if relative else "abs.jsonl")).is_file()
    assert not (tmp_path / "declared.jsonl").exists()


def test_the_plan_hash_comes_from_the_callers_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    declared, placed = str(tmp_path / "declared.jsonl"), str(tmp_path / "placed.jsonl")
    config, sources = _single_config(tmp_path, declared)
    captured: list[Any] = []
    real = dispatch.execute_route

    def spy(ctx: Any, decision: Any, refusals: Any) -> Any:
        captured.append(ctx)
        return real(ctx, decision, refusals)

    monkeypatch.setattr(dispatch, "execute_route", spy)
    run_pipeline(config, sources, engine_version="0.1.0", quarantine_path=placed)
    run_pipeline(config, sources, engine_version="0.1.0", quarantine_path=placed)
    first, second = captured
    assert first.plan.pipeline_config_hash == second.plan.pipeline_config_hash
    assert first.plan.pipeline_config_hash == _hash_config(config)
    assert first.config["quarantine"]["output_path"] == placed
    assert _hash_config(first.config) != _hash_config(config)


@pytest.mark.parametrize("bad", [True, False, 3, b"q.jsonl", ["q.jsonl"], {"p": "q"}, "", "  "])
def test_a_non_string_is_refused_before_any_source_is_read(tmp_path: Path, bad: Any) -> None:
    config, _ = _single_config(tmp_path, "declared.jsonl")

    class Unread(dict):  # type: ignore[type-arg]
        def __iter__(self) -> Any:
            raise AssertionError("a source was touched")

        def __len__(self) -> int:
            raise AssertionError("a source was touched")

        def __bool__(self) -> bool:
            raise AssertionError("a source was touched")

    with pytest.raises(ExecutionError) as exc:
        run_pipeline(config, Unread(), engine_version="0.1.0", quarantine_path=bad)  # type: ignore[arg-type]
    assert exc.value.code == "invalid_execution_knob"
    assert "q.jsonl" not in str(exc.value)


def test_without_a_quarantine_block_the_config_is_left_alone(tmp_path: Path) -> None:
    config, sources = _single_config(tmp_path, "declared.jsonl")
    config.pop("quarantine")
    config.pop("validators")
    before = copy.deepcopy(config)
    run_pipeline(config, sources, engine_version="0.1.0", quarantine_path=str(tmp_path / "q.jsonl"))
    assert config == before


@pytest.mark.parametrize("route", ["full_frame", "sequential"])
def test_a_path_with_surrounding_spaces_is_used_verbatim_on_both_routes(
    tmp_path: Path, route: str
) -> None:
    folder = tmp_path / "run"
    folder.mkdir()
    spaced = str(folder / " spaced.jsonl ")
    make = _single_config if route == "full_frame" else _fk_config
    config, sources = make(tmp_path, "declared.jsonl")
    result = run_pipeline(
        config, sources, engine_version="0.1.0", execution_mode=route, quarantine_path=spaced
    )
    assert result.quality_metrics["quarantine"]["output_path"] == spaced
    assert sorted(p.name for p in folder.iterdir()) == [" spaced.jsonl "]
