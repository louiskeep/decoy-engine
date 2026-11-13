"""Chained column-level `derived` entries count as created columns.

The executor orders a `derived` node after the nodes writing the columns it reads, so a chain
of column-level derives runs. The created-column contract must accept the same chains, and
still refuse a chain whose root reads a column nobody supplies, and a cycle.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import run_pipeline
from decoy_engine.config import PipelineConfig
from decoy_engine.execution._column_access import created_columns
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.reason_adapter import reason_from_exception
from tests.unit.execution.test_created_target_contract import (
    CREATING,
    KW,
    PASSTHROUGH,
    SRC,
    _config,
)

REG = get_default_registry()
SOURCE = ("a", "b")


def _derived(name: str, expression: str) -> dict[str, Any]:
    return {"name": name, "strategy": "derived", "provider_config": {"expression": expression}}


def _created(columns: list[dict[str, Any]], transforms: list[dict[str, Any]] | None = None):
    table: dict[str, Any] = {"name": "t", "columns": columns}
    if transforms:
        table["transforms"] = transforms
    return created_columns(table, SOURCE, REG)


def test_two_step_column_derive_chain_is_created() -> None:
    assert _created([_derived("x", "a + 1"), _derived("y", "x * 2")]) == {"x", "y"}


def test_three_step_chain_is_created_whatever_the_declaration_order() -> None:
    columns = [_derived("z", "y + 1"), _derived("y", "x + 1"), _derived("x", "a + 1")]
    assert _created(columns) == {"x", "y", "z"}


def test_chain_rooted_at_a_missing_source_column_is_not_created() -> None:
    columns = [_derived("x", "nope + 1"), _derived("y", "x * 2"), _derived("z", "y + 1")]
    assert _created(columns) == frozenset()


def test_mixed_transform_derive_and_column_derive_chain_is_created() -> None:
    transforms = [{"op": "derive", "column": "t1", "expression": "a + 1"}]
    columns = [_derived("x", "t1 + 1"), _derived("y", "x + b")]
    assert _created(columns, transforms) == {"t1", "x", "y"}


def test_derive_cycle_is_not_created() -> None:
    columns = [_derived("x", "y + 1"), _derived("y", "x + 1"), _derived("w", "a + 1")]
    assert _created(columns) == {"w"}


def test_self_reading_derive_is_not_created() -> None:
    assert _created([_derived("x", "x + 1")]) == frozenset()


# --- execution: admission and ordering share one read set --------------------------------------


def _run_columns(
    tmp_path: Any, columns: list[dict[str, Any]], mode: str = "full_frame", *, fk: bool = False
) -> pa.Table:
    config = _config(tmp_path, "derived", fk=fk)
    table = config["tables"][0]
    table["columns"] = [*table["columns"][: len(PASSTHROUGH)], *columns]
    config = PipelineConfig.model_validate(config).model_dump()
    sources = {"t": SRC, "p": SRC} if fk else {"t": SRC}
    extra = {"out_of_core_threshold_rows": 1} if mode == "out_of_core" else {}
    return run_pipeline(config, sources, execution_mode=mode, **extra, **KW).outputs["t"]


def test_derived_aggregate_over_a_created_derive_runs_after_it(tmp_path: Any) -> None:
    columns = [
        _derived("z_driver", "a + 1"),
        {
            "name": "aa_aggregate",
            "strategy": "derived_aggregate",
            "provider_config": {"op": "sum", "column": "z_driver"},
        },
    ]
    out = _run_columns(tmp_path, columns)
    assert out["z_driver"].to_pylist() == [2, 3, 4, 5]
    assert set(out["aa_aggregate"].to_pylist()) == {14}


def test_two_step_derive_chain_runs_end_to_end(tmp_path: Any) -> None:
    out = _run_columns(tmp_path, [_derived("zz_late", "mid * 2"), _derived("mid", "a + 1")])
    assert out["mid"].to_pylist() == [2, 3, 4, 5]
    assert out["zz_late"].to_pylist() == [4, 6, 8, 10]


@pytest.mark.parametrize("strategy", sorted(CREATING))
def test_every_creating_strategy_runs_after_the_created_column_it_reads(
    strategy: str, tmp_path: Any
) -> None:
    """The target sorts before its driver, so only an explicit ordering edge runs it last."""
    template, refs = CREATING[strategy][1], CREATING[strategy][2]
    config = {
        k: v.format("z_driver", *refs[1:]) if isinstance(v, str) else v for k, v in template.items()
    }
    columns = [
        _derived("z_driver", refs[0]),
        {"name": "aa_tgt", "strategy": strategy, "provider_config": config},
    ]
    if strategy == "derived":
        columns[1]["provider_config"] = {"expression": "z_driver + 1"}
    out = _run_columns(tmp_path, columns)
    assert out.num_rows == SRC.num_rows
    assert {"z_driver", "aa_tgt"} <= set(out.column_names)


def test_forced_out_of_core_refuses_a_derive_chain_with_the_coded_reason(tmp_path: Any) -> None:
    with pytest.raises(Exception) as caught:
        _run_columns(
            tmp_path, [_derived("x", "a + 1"), _derived("y", "x * 2")], "out_of_core", fk=True
        )
    assert reason_from_exception(caught.value).code == "out_of_core_ineligible"
    assert "out_of_core_dynamic_output_type_unsupported" in str(caught.value)


def test_ordering_keeps_value_reads_when_the_when_predicate_is_unreadable() -> None:
    from decoy_engine.execution._column_access import scalar_creating_reads

    entry = {
        "name": "aa_tgt",
        "strategy": "derived",
        "provider_config": {"expression": "z_driver + 1"},
        "when": "b'x'",  # a bytes literal: the predicate's names cannot be read
    }
    assert scalar_creating_reads(entry) == {"z_driver"}
    assert _created([entry]) == frozenset()  # admission still refuses the unreadable entry


def test_a_when_predicate_adds_no_ordering_edge() -> None:
    from decoy_engine.execution._column_access import scalar_creating_reads

    entry = {**_derived("aa_tgt", "z_driver + 1"), "when": "b_gate > 0"}
    assert scalar_creating_reads(entry) == {"z_driver"}


def test_a_when_predicate_naming_a_dependent_does_not_form_a_cycle(tmp_path: Any) -> None:
    # `a` is gated on `g` while `g` is derived from `a`. The gate reads `g` as it stands when
    # `a` runs (its source value), so ordering adds only g-after-a and the run succeeds.
    config = _config(tmp_path, "derived")
    config["tables"][0]["columns"] = [
        {"name": "d", "strategy": "passthrough"},
        {"name": "b", "strategy": "passthrough"},
        {**_derived("a", "a + 10"), "when": "g == 'x'"},
        _derived("g", "a"),
    ]
    config = PipelineConfig.model_validate(config).model_dump()
    out = run_pipeline(config, {"t": SRC}, **KW).outputs["t"]
    assert out.column("a").to_pylist() == [11, 12, 3, 4]
