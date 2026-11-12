"""`plan_delivery`: which tables a run makes visible, from the table-level dependency graph."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from decoy_engine.config import PipelineConfig, PipelineConfigError
from decoy_engine.delivery import plan_delivery

FAIL: Any = "failed"  # any non-None value is a failure cause; the plan never reads its text


def _config(
    tables: list[str],
    edges: list[tuple[str, str]] | None = None,
    mode: str | None = None,
    generate: Mapping[str, list[str]] | None = None,
) -> dict[str, Any]:
    """A validated-shape config dict: edges are (parent, child), generate maps child to parents."""
    generate = generate or {}
    entries: list[dict[str, Any]] = []
    for name in tables:
        entry: dict[str, Any] = {"name": name}
        if name in generate:
            entry["generate_columns"] = [
                {"name": f"{p}_id", "type": "reference", "reference_table": p}
                for p in generate[name]
            ] or [{"name": "id", "type": "sequence"}]
        entries.append(entry)
    config: dict[str, Any] = {
        "tables": entries,
        "relationships": [
            {
                "parent": {"table": p, "columns": ["id"]},
                "children": [{"table": c, "columns": ["pid"]}],
                "orphan_policy": "fail",
            }
            for p, c in (edges or [])
        ],
    }
    if mode is not None:
        config["delivery"] = {"mode": mode}
    return config


def _states(decision: Any) -> dict[str, str]:
    return {name: t.state for name, t in decision.tables.items()}


def test_at1_all_ok_strict_delivers_everything() -> None:
    d = plan_delivery(_config(["a", "b", "c"]), {"a": None, "b": None, "c": None})
    assert _states(d) == {"a": "delivered", "b": "delivered", "c": "delivered"}
    assert d.deliver is True


@pytest.mark.parametrize("mode", [None, "best_effort"])
def test_at1b_an_empty_run_delivers(mode: str | None) -> None:
    d = plan_delivery(_config([], mode=mode), {})
    assert d.tables == {}
    assert d.deliver is True


def test_at2_strict_failure_withholds_the_good_tables() -> None:
    d = plan_delivery(_config(["a", "b", "c"]), {"a": None, "b": FAIL, "c": None})
    assert _states(d) == {"a": "withheld", "b": "failed", "c": "withheld"}
    assert d.deliver is False


def test_at3_best_effort_drops_the_transitive_dependents() -> None:
    config = _config(["p", "c", "g", "u"], [("p", "c"), ("c", "g")], "best_effort")
    d = plan_delivery(config, {"p": FAIL, "c": None, "g": None, "u": None})
    assert _states(d) == {"p": "failed", "c": "dropped", "g": "dropped", "u": "delivered"}
    assert d.tables["c"].dropped_because == "p"
    assert d.tables["g"].dropped_because == "p"
    assert d.tables["u"].dropped_because is None
    assert d.deliver is True


def test_at4_a_good_parent_is_delivered_when_the_other_parent_failed() -> None:
    config = _config(["p", "q", "c"], [("p", "c"), ("q", "c")], "best_effort")
    d = plan_delivery(config, {"p": FAIL, "q": None, "c": None})
    assert _states(d) == {"p": "failed", "q": "delivered", "c": "dropped"}
    assert d.tables["c"].dropped_because == "p"


def test_at5_self_edges_are_ignored() -> None:
    config = _config(["t"], [("t", "t")], "best_effort")
    d = plan_delivery(config, {"t": None})
    assert _states(d) == {"t": "delivered"}


@pytest.mark.timeout(2)
def test_at6_a_table_level_cycle_terminates_and_drops_the_component() -> None:
    config = _config(["A", "B", "U"], [("A", "B"), ("B", "A")], "best_effort")
    d = plan_delivery(config, {"A": FAIL, "B": None, "U": None})
    assert _states(d) == {"A": "failed", "B": "dropped", "U": "delivered"}
    assert d.tables["B"].dropped_because == "A"


def test_at7_generate_reference_parents_are_edges() -> None:
    config = _config(["p", "c"], mode="best_effort", generate={"p": [], "c": ["p"]})
    assert config["relationships"] == []
    d = plan_delivery(config, {"p": FAIL, "c": None})
    assert _states(d) == {"p": "failed", "c": "dropped"}
    assert d.tables["c"].dropped_because == "p"


@pytest.mark.parametrize("order", [("f1", "f2"), ("f2", "f1")])
def test_at8_dropped_because_is_the_smallest_failed_name(order: tuple[str, str]) -> None:
    config = _config(["f2", "f1", "d"], [("f2", "d"), ("f1", "d")], "best_effort")
    outcomes: dict[str, Any] = {name: FAIL for name in order}
    outcomes["d"] = None
    d = plan_delivery(config, outcomes)
    assert d.tables["d"].dropped_because == "f1"


def test_at9_the_walk_passes_through_a_table_without_a_file_target() -> None:
    config = _config(["p", "mid", "g"], [("p", "mid"), ("mid", "g")], "best_effort")
    d = plan_delivery(config, {"p": FAIL, "g": None})
    assert _states(d) == {"p": "failed", "g": "dropped"}
    assert "mid" not in d.tables
    assert d.tables["g"].dropped_because == "p"


def test_at2b_an_unknown_mode_raises_instead_of_degrading() -> None:
    with pytest.raises(PipelineConfigError):
        plan_delivery(_config(["a"], mode="bogus"), {"a": FAIL})


def test_plan_reads_the_dump_of_a_validated_pipeline_config() -> None:
    raw = {
        "version": 2,
        "global_settings": {"seed": 1, "post_validation": False},
        "sources": {
            n: {"type": "file", "format": "csv", "has_header": True, "path": f"./{n}.csv"}
            for n in ("p", "c")
        },
        "tables": [
            {
                "name": "p",
                "columns": [{"name": "id", "strategy": "preserve_format_id", "provider": "uuid"}],
            },
            {"name": "c", "columns": [{"name": "pid", "strategy": "from_parent"}]},
        ],
        "relationships": [
            {
                "parent": {"table": "p", "columns": ["id"]},
                "children": [{"table": "c", "columns": ["pid"]}],
                "orphan_policy": "fail",
            }
        ],
        "targets": {
            n: {"type": "file", "format": "csv", "path": f"./out/{n}.csv"} for n in ("p", "c")
        },
        "delivery": {"mode": "best_effort"},
    }
    config = PipelineConfig.model_validate(raw).model_dump(mode="python")
    d = plan_delivery(config, {"p": FAIL, "c": None})
    assert _states(d) == {"p": "failed", "c": "dropped"}
    assert d.tables["c"].dropped_because == "p"
