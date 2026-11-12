"""`plan_delivery`: which tables a run makes visible, from the table-level dependency graph."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from decoy_engine.config import PipelineConfig, PipelineConfigError
from decoy_engine.delivery import plan_delivery
from decoy_engine.reasons.decoy import (
    DELIVERY_DEPENDENT_DROPPED,
    DELIVERY_POINTER_MOVED,
    DELIVERY_ROLLED_BACK,
    DELIVERY_WITHHELD,
    PUBLICATION_FAILED,
)

# The plan accepts only failure-kind reason codes as a table's cause.
FAIL = PUBLICATION_FAILED
_NOT_A_FAILURE = "delivery outcome is not a failure reason code"


def _config(
    tables: list[str],
    edges: list[tuple[str, str]] | None = None,
    mode: str | None = None,
    generate: Mapping[str, list[str]] | None = None,
    remote: tuple[str, ...] = (),
    untargeted: tuple[str, ...] = (),
) -> dict[str, Any]:
    """A validated-shape config dict: edges are (parent, child), generate maps child to parents.

    Every table gets a file target, except those in `remote`, which get an s3 one, and those in
    `untargeted`, which get none.
    """
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
        "targets": {
            n: {"type": "s3" if n in remote else "file", "path": f"./out/{n}.csv"}
            for n in tables
            if n not in untargeted
        },
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


def test_at1c_an_outcome_for_a_table_outside_the_universe_raises() -> None:
    unknown = _config(["a"])
    with pytest.raises(PipelineConfigError):
        plan_delivery(unknown, {"a": None, "ghost": None})
    untargeted = _config(["a", "b"], untargeted=("b",))
    with pytest.raises(PipelineConfigError):
        plan_delivery(untargeted, {"a": None, "b": None})


@pytest.mark.parametrize("mode", [None, "best_effort"])
def test_atc18_cloud_target_tables_are_in_the_universe(mode: str | None) -> None:
    config = _config(["f", "s", "g"], [("s", "g")], mode, remote=("s", "g"))
    config["targets"]["g"]["type"] = "gcs"
    ok = plan_delivery(config, {"f": None, "s": None, "g": None})
    assert _states(ok) == {"f": "delivered", "s": "delivered", "g": "delivered"}
    d = plan_delivery(config, {"f": None, "s": FAIL, "g": None})
    if mode is None:
        assert _states(d) == {"f": "withheld", "s": "failed", "g": "withheld"}
    else:
        assert _states(d) == {"f": "delivered", "s": "failed", "g": "dropped"}
        assert d.tables["g"].dropped_because == "s"
    with pytest.raises(PipelineConfigError):
        plan_delivery(config, {"f": None, "s": None})


def test_at1d_a_file_target_table_without_an_outcome_raises() -> None:
    with pytest.raises(PipelineConfigError):
        plan_delivery(_config(["a", "b"]), {"a": None})


def test_at1f_a_target_that_names_no_table_expects_no_outcome() -> None:
    config = _config(["a", "b"])
    config["targets"]["orphan"] = {"type": "file", "path": "./out/orphan.csv"}
    d = plan_delivery(config, {"a": None, "b": None})
    assert _states(d) == {"a": "delivered", "b": "delivered"}
    with pytest.raises(PipelineConfigError):
        plan_delivery(config, {"a": None, "b": None, "orphan": None})


@pytest.mark.parametrize("end", ["parent", "child"])
def test_at1e_a_relationship_end_outside_tables_raises(end: str) -> None:
    edges = [("ghost", "a")] if end == "parent" else [("a", "ghost")]
    with pytest.raises(PipelineConfigError):
        plan_delivery(_config(["a"], edges, "best_effort"), {"a": None})


def test_at1e_a_generate_reference_parent_outside_tables_raises() -> None:
    config = _config(["a"], generate={"a": ["ghost"]})
    with pytest.raises(PipelineConfigError):
        plan_delivery(config, {"a": None})


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


def test_at9_the_walk_passes_through_a_table_without_a_target() -> None:
    config = _config(
        ["p", "mid", "g"], [("p", "mid"), ("mid", "g")], "best_effort", untargeted=("mid",)
    )
    d = plan_delivery(config, {"p": FAIL, "g": None})
    assert _states(d) == {"p": "failed", "g": "dropped"}
    assert "mid" not in d.tables
    assert d.tables["g"].dropped_because == "p"


def test_at2b_an_unknown_mode_raises_instead_of_degrading() -> None:
    with pytest.raises(PipelineConfigError) as info:
        plan_delivery(_config(["a"], mode="bogus"), {"a": FAIL})
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert "bogus" not in str(info.value)


@pytest.mark.parametrize("bad", [None, False, 0, "", []])
def test_at2c_a_present_but_falsy_delivery_block_raises(bad: Any) -> None:
    config = _config(["a"])
    config["delivery"] = bad
    with pytest.raises(PipelineConfigError):
        plan_delivery(config, {"a": FAIL})


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


@pytest.mark.parametrize("mode", [None, "best_effort"])
def test_at10_each_state_carries_its_code(mode: str | None) -> None:
    cfg = _config(["a", "b", "c", "d"], edges=[("a", "b")], mode=mode)
    d = plan_delivery(cfg, {"a": PUBLICATION_FAILED, "b": None, "c": None, "d": None})
    codes = {n: t.code for n, t in d.tables.items()}
    if mode is None:
        assert codes == {
            "a": PUBLICATION_FAILED,
            "b": DELIVERY_WITHHELD,
            "c": DELIVERY_WITHHELD,
            "d": DELIVERY_WITHHELD,
        }
    else:
        assert codes == {
            "a": PUBLICATION_FAILED,
            "b": DELIVERY_DEPENDENT_DROPPED,
            "c": None,
            "d": None,
        }
    # ReasonCode compares as its bare string, so the namespace is pinned separately.
    assert {n: getattr(c, "namespace", None) for n, c in codes.items() if c} == {
        n: "decoy" for n, c in codes.items() if c
    }


def test_at10b_an_all_ok_run_has_no_codes() -> None:
    d = plan_delivery(_config(["a", "b"]), {"a": None, "b": None})
    assert [t.code for t in d.tables.values()] == [None, None]


@pytest.mark.parametrize(
    "bad",
    [DELIVERY_WITHHELD, DELIVERY_DEPENDENT_DROPPED, DELIVERY_ROLLED_BACK, "failed", object()],
    ids=["withheld", "dependent_dropped", "warning", "string", "object"],
)
def test_at11_a_non_failure_outcome_is_refused_with_a_fixed_message(bad: Any) -> None:
    with pytest.raises(PipelineConfigError) as err:
        plan_delivery(_config(["a", "b"]), {"a": bad, "b": None})
    assert str(err.value) == _NOT_A_FAILURE


def test_at11b_a_real_failure_code_still_plans_as_failed() -> None:
    d = plan_delivery(_config(["a"]), {"a": DELIVERY_POINTER_MOVED})
    assert d.tables["a"].state == "failed"
    assert d.tables["a"].code is DELIVERY_POINTER_MOVED
