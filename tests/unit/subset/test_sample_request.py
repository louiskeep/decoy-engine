"""`sample_request`: the typed request a sample builds, and the strict knobs it takes."""

from __future__ import annotations

import ast
import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pyarrow as pa
import pytest
from pydantic import ValidationError

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
from decoy_engine.subset._inputs import ResidentInput
from tests.unit.subset._sample_fixtures import config, orphans, parent_child
from tests.unit.subset.conftest import JOB_SEED_CONFIG

FK = ("p", "id", "c", "pid")
SELF = ("employees", "id", "employees", "manager_id")
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
    req = sample_request(cfg, rows=2)
    assert _heads(req) == ["a"]
    cycle = {
        "a": pa.table({"id": ["a0", "a1", "a2"], "bid": ["b0", "b1", "b2"]}),
        "b": pa.table({"id": ["b0", "b1", "b2"], "aid": ["a0", "a1", "a2"]}),
    }
    chosen = select_subset(req, cycle, config=JOB_SEED_CONFIG)
    assert chosen.survivors["a"] and chosen.survivors["b"]
    flipped = config({"b": "mask", "a": "mask"}, [("a", "id", "b", "aid"), ("b", "id", "a", "bid")])
    assert _heads(sample_request(flipped, rows=2)) == ["b"]


def _staff(order: list[str]) -> dict[str, Any]:
    kinds = dict.fromkeys(order, "mask")
    return config(kinds, [("departments", "id", "employees", "dept_id"), SELF])


def _staff_tables() -> dict[str, Any]:
    ids = [f"e{i}" for i in range(8)]
    return {
        "departments": pa.table({"id": ["d0", "d1", "d2"]}),
        "employees": pa.table(
            {
                "id": ids,
                "dept_id": [f"d{i % 3}" for i in range(8)],
                "manager_id": [ids[max(i - 1, 0)] for i in range(8)],
            }
        ),
    }


@pytest.mark.parametrize("order", [["departments", "employees"], ["employees", "departments"]])
def test_a_self_referencing_table_is_child_free_and_seeds_the_sample(order: list[str]) -> None:
    req = sample_request(_staff(order), rows=3)
    assert _heads(req) == ["employees"]
    tables = _staff_tables()
    chosen = select_subset(req, tables, config=JOB_SEED_CONFIG)
    assert len(chosen.survivors["employees"]) >= 3
    pairs = [("departments", "id", "employees", "dept_id"), SELF]
    assert orphans(tables, dict(chosen.survivors), pairs) == 0


def test_a_relationship_naming_a_table_outside_the_mask_tables_is_coded() -> None:
    cfg = config({"p": "mask", "c": "mask"}, [FK, ("p", "id", "ghost", "pid")])
    with pytest.raises(SubsetPreflightError) as excinfo:
        sample_request(cfg, rows=2)
    assert excinfo.value.code == reasons.subset.SUBSET_UNKNOWN_TABLE
    assert "ghost" in str(excinfo.value)
    flipped = config({"p": "mask", "c": "mask"}, [FK, ("ghost", "id", "c", "gid")])
    with pytest.raises(SubsetPreflightError) as excinfo:
        sample_request(flipped, rows=2)
    assert excinfo.value.code == reasons.subset.SUBSET_UNKNOWN_TABLE


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
    with pytest.raises(ValidationError):
        PipelineConfig.model_validate(_pipeline(seed))


def test_a_sample_request_leaves_the_config_hash_alone() -> None:
    raw = _pipeline({"mode": "sample", "key_columns": ["id"], "count": 3})
    PipelineConfig.model_validate(raw)
    dump = copy.deepcopy(raw)
    before = pipeline_config_hash(dump)
    sample_request(dump, rows=3, use_config_subset=False)
    assert pipeline_config_hash(dump) == before == pipeline_config_hash(raw)


def _two_components(allow_dangling: bool) -> dict[str, Any]:
    """Component 1 is seeded by the block; both components configure every edge `downward`."""
    cfg = config(
        {"p": "mask", "c": "mask", "p2": "mask", "c2": "mask"},
        [FK, ("p2", "id", "c2", "pid")],
        seeds=[{"table": "p", "mode": "keys", "key_columns": ["id"], "keys": [["p1"]]}],
    )
    cfg["subset"]["edge_directions"] = {
        "p.id -> c.pid": "downward",
        "p2.id -> c2.pid": "downward",
    }
    cfg["subset"]["allow_dangling"] = allow_dangling
    return cfg


@pytest.mark.parametrize("allow_dangling", [False, True])
def test_an_unseeded_component_is_upward_whatever_the_config_says(allow_dangling: bool) -> None:
    req = sample_request(_two_components(allow_dangling), rows=2)
    assert _directions(req) == {"p.id -> c.pid": "downward", "p2.id -> c2.pid": "upward"}
    tables = {**parent_child("p", "c"), **parent_child("p2", "c2")}
    chosen = select_subset(req, tables, config=JOB_SEED_CONFIG)
    assert chosen.survivors["c2"]
    assert orphans(tables, dict(chosen.survivors), [("p2", "id", "c2", "pid")]) == 0


def _outcome(cfg: dict[str, Any], **kwargs: Any) -> tuple[str, str, str]:
    """The refusal a call raises as (class, code, message); a call that returns is a bug."""
    try:
        sample_request(cfg, rows=2, **kwargs)
    except Exception as exc:  # the test reads whichever refusal comes back
        return type(exc).__name__, getattr(exc, "code", ""), str(exc)
    return "returned", "", ""


_GEN_TO_MASK = ("g", "id", "m", "gid")
_UNKNOWN_TABLE_CELLS = [
    pytest.param(
        {"g": "generate", "m": "mask"}, [_GEN_TO_MASK], {"root": "g"}, "g", id="generate-root"
    ),
    pytest.param(
        {"p": "mask", "g": "generate"},
        [("g", "id", "ghost", "gid")],
        {},
        "ghost",
        id="unknown-child-of-generated-parent",
    ),
    pytest.param(
        {"p": "mask", "g": "generate"},
        [("ghost", "id", "g", "gid")],
        {},
        "ghost",
        id="unknown-parent-of-generated-child",
    ),
    pytest.param(
        {"p": "mask", "c": "mask"},
        [("ghost", "id", "c", "gid")],
        {},
        "ghost",
        id="unknown-parent-of-mask-child",
    ),
    pytest.param(
        {"p": "mask", "c": "mask"},
        [("p", "id", "ghost", "pid")],
        {},
        "ghost",
        id="unknown-child-of-mask-parent",
    ),
]


@pytest.mark.parametrize(("kinds", "edges", "kwargs", "named"), _UNKNOWN_TABLE_CELLS)
def test_tables_are_checked_before_any_structural_decision(
    kinds: dict[str, str],
    edges: list[tuple[str, str, str, str]],
    kwargs: dict[str, Any],
    named: str,
) -> None:
    cls, code, message = _outcome(config(kinds, edges), **kwargs)
    assert (cls, code) == ("SubsetPreflightError", reasons.subset.SUBSET_UNKNOWN_TABLE)
    assert repr(named) in message


def test_the_refusal_edge_does_not_count_an_unknown_child_as_masked() -> None:
    assert sample_refusal_edge(config({"g": "generate"}, [("g", "id", "ghost", "gid")])) is None


def _walks(path: str) -> tuple[bool, bool]:
    tree = ast.parse((Path(__file__).parents[3] / path).read_text())
    loops = any(isinstance(n, ast.While) for n in ast.walk(tree))
    imports = any(
        isinstance(n, ast.ImportFrom) and any(a.name == "reach" for a in n.names)
        for n in ast.walk(tree)
    )
    return loops, imports


@pytest.mark.parametrize(
    "path", ["src/decoy_engine/subset/_sample.py", "src/decoy_engine/subset/_scope.py"]
)
def test_reach_is_the_only_walk(path: str) -> None:
    assert _walks(path) == (False, True)


def test_an_upward_walk_from_a_child_of_a_cycle_keeps_both_cycle_tables() -> None:
    pairs = [("a", "id", "b", "aid"), ("b", "id", "a", "bid"), ("b", "id", "c", "bid")]
    cfg = config({"a": "mask", "b": "mask", "c": "mask"}, pairs)
    tables = {
        "a": pa.table({"id": ["a0", "a1", "a2"], "bid": ["b0", "b1", "b2"]}),
        "b": pa.table({"id": ["b0", "b1", "b2"], "aid": ["a0", "a1", "a2"]}),
        "c": pa.table({"id": [f"c{i}" for i in range(6)], "bid": [f"b{i % 3}" for i in range(6)]}),
    }
    chosen = select_subset(sample_request(cfg, rows=2), tables, config=JOB_SEED_CONFIG)
    assert chosen.survivors["c"] == frozenset({0, 1})
    assert chosen.survivors["b"] >= frozenset({0, 1})
    assert chosen.survivors["a"] >= frozenset({0, 1})
    assert orphans(tables, dict(chosen.survivors), pairs) == 0


class _CountingInput(ResidentInput):
    def __init__(self, name: str, table: pa.Table, log: list[str]) -> None:
        super().__init__(name, table)
        self._log = log

    def key_frame(self, columns: Any) -> pl.DataFrame:
        self._log.append(self.name)
        return super().key_frame(columns)


def test_each_table_is_read_for_keys_exactly_once() -> None:
    tables = parent_child("p", "c")
    log: list[str] = []
    inputs = {n: _CountingInput(n, t, log) for n, t in tables.items()}
    req = sample_request(config({"p": "mask", "c": "mask"}, [FK]), rows=3, root="p")
    select_subset(req, inputs, config=JOB_SEED_CONFIG)
    assert sorted(log) == ["c", "p"]


def test_ignoring_the_subset_block_gives_default_seeds_and_only_upward_edges() -> None:
    cfg = config(
        {"p": "mask", "c": "mask"},
        [FK],
        seeds=[{"table": "p", "mode": "keys", "key_columns": ["id"], "keys": [["p1"]]}],
    )
    cfg["subset"]["edge_directions"] = {"p.id -> c.pid": "downward"}
    req = sample_request(cfg, rows=3, use_config_subset=False)
    assert req.seeds == (SeedSpec(table="c", mode="head", count=3),)
    assert _directions(req) == {"p.id -> c.pid": "upward"}


def _composite() -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    cfg = config({"p": "mask", "c": "mask"}, [FK])
    rel = cfg["relationships"][0]
    rel["parent"]["columns"] = ["a", "b"]
    rel["children"][0]["columns"] = ["ca", "cb"]
    keys = [(f"a{i}", f"b{i}") for i in range(4)]
    tables = {
        "p": pa.table({"a": [k[0] for k in keys], "b": [k[1] for k in keys]}),
        "c": pa.table(
            {"ca": [keys[i % 4][0] for i in range(9)], "cb": [keys[i % 4][1] for i in range(9)]}
        ),
    }
    return cfg, tables, [("p", "a", "c", "ca"), ("p", "b", "c", "cb")]


def _two_children() -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    cfg = config({"p": "mask", "c1": "mask", "c2": "mask"}, [("p", "id", "c1", "pid")])
    cfg["relationships"][0]["children"].append({"table": "c2", "columns": ["pid"]})
    tables = {**parent_child("p", "c1"), "c2": parent_child("p", "c2", children=10)["c2"]}
    return cfg, tables, [("p", "id", "c1", "pid"), ("p", "id", "c2", "pid")]


def _diamond() -> tuple[dict[str, Any], dict[str, Any], list[Any]]:
    pairs = [
        ("a", "id", "b", "aid"),
        ("a", "id", "c", "aid"),
        ("b", "id", "d", "bid"),
        ("c", "id", "d", "cid"),
    ]
    cfg = config(dict.fromkeys("abcd", "mask"), pairs)
    tables = {
        "a": pa.table({"id": [f"a{i}" for i in range(4)]}),
        "b": pa.table({"id": [f"b{i}" for i in range(4)], "aid": [f"a{i}" for i in range(4)]}),
        "c": pa.table({"id": [f"c{i}" for i in range(4)], "aid": [f"a{3 - i}" for i in range(4)]}),
        "d": pa.table(
            {
                "id": [f"d{i}" for i in range(8)],
                "bid": [f"b{i % 4}" for i in range(8)],
                "cid": [f"c{(i + 1) % 4}" for i in range(8)],
            }
        ),
    }
    return cfg, tables, pairs


@pytest.mark.parametrize("shape", [_composite, _two_children, _diamond])
def test_the_default_sample_has_no_orphans_for_each_edge_shape(shape: Any) -> None:
    cfg, tables, pairs = shape()
    chosen = select_subset(sample_request(cfg, rows=2), tables, config=JOB_SEED_CONFIG)
    assert any(chosen.survivors[p] for p in {pair[0] for pair in pairs})
    assert orphans(tables, dict(chosen.survivors), pairs) == 0


@pytest.mark.parametrize(
    ("configured", "carried"), [(500, 500), (5_000_000, 1_000_000), (None, 1_000_000)]
)
def test_the_budget_is_the_config_value_capped_at_the_ceiling(
    configured: int | None, carried: int
) -> None:
    budget = {"max_table_seed_multiple": 3.0}
    if configured is not None:
        budget["max_total_rows"] = configured
    cfg = config(
        {"p": "mask", "c": "mask"},
        [FK],
        seeds=[{"table": "p", "mode": "keys", "key_columns": ["id"], "keys": [["p1"]]}],
        budget=budget,
    )
    policy = sample_request(cfg, rows=2).policy
    assert policy.budget.max_total_rows == carried
    assert policy.budget.max_table_seed_multiple == 3.0


def test_roots_come_out_sorted_whatever_the_config_order() -> None:
    req = sample_request(config({"z": "mask", "m": "mask", "a": "mask"}, []), rows=2)
    assert _heads(req) == ["z", "m", "a"]
    assert req.sample is not None and req.sample.roots == ("a", "m", "z")
