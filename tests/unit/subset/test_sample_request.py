"""`sample_request`: the typed request a sample builds, and the strict knobs it takes."""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine._execution_error import ExecutionError
from decoy_engine.config import PipelineConfig
from decoy_engine.plan import pipeline_config_hash
from decoy_engine.subset import (
    SampleSpec,
    SeedSpec,
    SubsetPreflightError,
    SubsetRequest,
    sample_refusal_edge,
    sample_request,
    scope_for,
    select_subset,
    subset_request_from_config,
)
from decoy_engine.subset._edges import build_subset_edges
from tests.unit.subset._sample_fixtures import config, orphans, parent_child
from tests.unit.subset.conftest import JOB_SEED_CONFIG

FK = ("p", "id", "c", "pid")
KNOB = reasons.runtime.INVALID_EXECUTION_KNOB


def _heads(req: SubsetRequest) -> list[str]:
    assert all(s.mode == "head" for s in req.seeds)
    return [s.table for s in req.seeds]


def _directions(req: SubsetRequest) -> dict[str, str]:
    return dict(req.policy.edge_directions)


def test_default_seeds_are_child_free_tables_with_upward_edges() -> None:
    cfg = config({"p": "mask", "c": "mask", "e": "mask"}, [FK])
    req = sample_request(cfg, rows=3)
    assert req.seeds == (
        SeedSpec(table="c", mode="head", count=3),
        SeedSpec(table="e", mode="head", count=3),
    )
    assert set(_directions(req).values()) == {"upward"}
    assert len(_directions(req)) == 1
    assert req.sample == SampleSpec(rows=3, roots=("c", "e"), display_rows=0)
    tables = {**parent_child("p", "c"), "e": parent_child("e", "x")["e"]}
    scope = scope_for(req, tables)
    assert scope.head == frozenset({"e"})
    assert scope.tables == frozenset({"p", "c"})
    assert scope.whole == frozenset()
    in_scope = replace(req, seeds=tuple(s for s in req.seeds if s.table in scope.tables))
    chosen = select_subset(in_scope, {n: tables[n] for n in scope.tables}, config=JOB_SEED_CONFIG)
    assert chosen.survivors["c"] == frozenset({0, 1, 2})
    assert orphans(tables, dict(chosen.survivors), [FK]) == 0


def test_pure_cycle_seeds_its_first_table() -> None:
    cfg = config({"a": "mask", "b": "mask"}, [("a", "id", "b", "aid"), ("b", "id", "a", "bid")])
    assert _heads(sample_request(cfg, rows=2)) == ["a"]
    flipped = config({"b": "mask", "a": "mask"}, [("a", "id", "b", "aid"), ("b", "id", "a", "bid")])
    assert _heads(sample_request(flipped, rows=2)) == ["b"]


def test_cycle_with_a_child_free_table_seeds_that_table() -> None:
    cfg = config(
        {"a": "mask", "b": "mask", "c": "mask"},
        [("a", "id", "b", "aid"), ("b", "id", "a", "bid"), ("b", "id", "c", "bid")],
    )
    req = sample_request(cfg, rows=2)
    assert _heads(req) == ["c"]
    assert set(_directions(req).values()) == {"upward"}
    assert len(_directions(req)) == 3


def test_root_is_both_way_and_other_components_use_defaults() -> None:
    cfg = config(
        {"p": "mask", "c": "mask", "p2": "mask", "c2": "mask"},
        [FK, ("p2", "id", "c2", "pid")],
    )
    req = sample_request(cfg, rows=2, root="p")
    assert _heads(req) == ["p", "c2"]
    ids = {e.edge_id: e.child_table for e in build_subset_edges(req.relationships)}
    by_child = {ids[k]: v for k, v in _directions(req).items()}
    assert by_child == {"c": "both", "c2": "upward"}
    tables = {**parent_child("p", "c"), **parent_child("p2", "c2")}
    scope = scope_for(req, tables)
    assert scope.head == frozenset() and scope.whole == frozenset()
    chosen = select_subset(req, tables, config=JOB_SEED_CONFIG)
    pairs = [FK, ("p2", "id", "c2", "pid")]
    assert orphans(tables, dict(chosen.survivors), pairs) == 0


def test_a_subset_block_keeps_its_component_and_defaults_the_rest() -> None:
    cfg = config(
        {"p": "mask", "c": "mask", "p2": "mask", "c2": "mask"},
        [FK, ("p2", "id", "c2", "pid")],
        seeds=[{"table": "p", "mode": "keys", "key_columns": ["id"], "keys": [["p1"]]}],
    )
    req = sample_request(cfg, rows=2)
    assert [(s.table, s.mode, s.cap) for s in req.seeds] == [
        ("p", "keys", 2),
        ("c2", "head", None),
    ]
    by_child = {
        e.child_table: _directions(req).get(e.edge_id)
        for e in build_subset_edges(req.relationships)
    }
    assert by_child == {"c": None, "c2": "upward"}
    tables = {**parent_child("p", "c"), **parent_child("p2", "c2")}
    scope = scope_for(req, tables)
    assert scope.whole == frozenset() and scope.head == frozenset()


def test_a_generate_edge_is_dropped_and_listed() -> None:
    cfg = config(
        {"p": "mask", "c": "mask", "g": "generate"},
        [FK, ("p", "id", "g", "pid")],
        seeds=[{"table": "p", "mode": "keys", "key_columns": ["id"], "keys": [["p1"]]}],
    )
    req = sample_request(cfg, rows=2, use_config_subset=False)
    assert req.excluded_edges == subset_request_from_config(cfg).excluded_edges  # type: ignore[union-attr]
    assert len(req.excluded_edges) == 1 and "g.pid" in req.excluded_edges[0]
    assert {e.child_table for e in build_subset_edges(req.relationships)} == {"c"}
    tables = parent_child("p", "c")
    chosen = select_subset(req, tables, config=JOB_SEED_CONFIG)
    assert chosen.excluded_edges == req.excluded_edges


def test_refusal_edge_sees_a_generated_parent_before_the_drop() -> None:
    cfg = config({"g": "generate", "m": "mask"}, [("g", "id", "m", "gid")])
    assert sample_refusal_edge(cfg) == ("g", ("id",), "m", ("gid",))
    assert sample_refusal_edge(config({"p": "mask", "c": "mask"}, [FK])) is None
    with pytest.raises(ExecutionError) as excinfo:
        sample_request(cfg, rows=2)
    assert excinfo.value.code == KNOB
    assert "'g'" in str(excinfo.value) and "'m'" in str(excinfo.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rows": 0},
        {"rows": 1001},
        {"rows": 5, "display_rows": 21},
        {"rows": 5, "display_rows": -1},
        {"rows": 5, "root": "p"},
    ],
)
def test_out_of_range_knobs_and_root_with_a_block_are_refused(kwargs: dict[str, Any]) -> None:
    cfg = config(
        {"p": "mask", "c": "mask"},
        [FK],
        seeds=[{"table": "p", "mode": "keys", "key_columns": ["id"], "keys": [["p1"]]}],
    )
    with pytest.raises(ExecutionError) as excinfo:
        sample_request(cfg, **kwargs)
    assert excinfo.value.code == KNOB


def test_bounds_are_accepted() -> None:
    cfg = config({"p": "mask", "c": "mask"}, [FK])
    assert sample_request(cfg, rows=1).sample.rows == 1  # type: ignore[union-attr]
    assert sample_request(cfg, rows=1000, display_rows=20).sample.rows == 1000  # type: ignore[union-attr]


_BAD = [True, False, "5", 5.0, None]


@pytest.mark.parametrize("bad", _BAD)
def test_strict_int_for_rows_and_display_rows(bad: Any) -> None:
    cfg = config({"p": "mask", "c": "mask"}, [FK])
    for call in (
        lambda: SampleSpec(rows=bad, roots=("c",)),
        lambda: SampleSpec(rows=5, roots=("c",), display_rows=bad),
        lambda: sample_request(cfg, rows=bad),
        lambda: sample_request(cfg, rows=5, display_rows=bad),
    ):
        with pytest.raises(ExecutionError) as excinfo:
            call()
        assert excinfo.value.code == KNOB


@pytest.mark.parametrize("bad", [True, False, "5", 5.0, 0, -1])
def test_seed_cap_is_a_strict_positive_int_or_unset(bad: Any) -> None:
    with pytest.raises(ExecutionError) as excinfo:
        SeedSpec(table="t", mode="head", count=3, cap=bad)
    assert excinfo.value.code == KNOB
    assert SeedSpec(table="t", mode="head", count=3, cap=None).cap is None


@pytest.mark.parametrize("root", ["g", "nope"])
def test_generate_or_unknown_root_is_an_unknown_table(root: str) -> None:
    cfg = config({"p": "mask", "c": "mask", "g": "generate"}, [FK])
    with pytest.raises(SubsetPreflightError) as excinfo:
        sample_request(cfg, rows=2, root=root)
    assert excinfo.value.code == reasons.subset.SUBSET_UNKNOWN_TABLE


def _pipeline(extra: dict[str, Any]) -> dict[str, Any]:
    ref = {"type": "file", "format": "parquet"}
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "sources": {"a": {**ref, "path": "/in/a.parquet"}, "b": {**ref, "path": "/in/b.parquet"}},
        "tables": [
            {"name": "a", "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}]},
            {"name": "b", "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "a", "columns": ["id"]},
                "children": [{"table": "b", "columns": ["id"]}],
                "orphan_policy": "preserve",
                "namespace": "n",
            }
        ],
        "targets": {
            "a": {"type": "file", "format": "parquet", "path": "/out/a.parquet"},
            "b": {"type": "file", "format": "parquet", "path": "/out/b.parquet"},
        },
        "subset": {"seeds": [{"table": "a", **extra}]},
    }


@pytest.mark.parametrize(
    "seed",
    [
        {"mode": "head", "count": 3},
        {"mode": "sample", "key_columns": ["id"], "count": 3, "cap": 2},
    ],
)
def test_yaml_cannot_say_head_or_cap(seed: dict[str, Any]) -> None:
    with pytest.raises(Exception):
        PipelineConfig.model_validate(_pipeline(seed))


def test_a_sample_request_leaves_the_config_hash_alone() -> None:
    raw = _pipeline({"mode": "sample", "key_columns": ["id"], "count": 3})
    PipelineConfig.model_validate(raw)
    dump = copy.deepcopy(raw)
    before = pipeline_config_hash(dump)
    sample_request(dump, rows=3, use_config_subset=False)
    assert pipeline_config_hash(dump) == before == pipeline_config_hash(raw)
