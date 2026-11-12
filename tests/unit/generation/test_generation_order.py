"""GEN-CLI E1: `generation_order` is one deterministic walk shared by every route.

AT-25 (a reference to a non-generate table raises one typed error everywhere) and AT-28 (the
order, the returned key order and every table's bytes are equal under any PYTHONHASHSEED).
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from decoy_engine.generation import generation_order
from decoy_engine.generation._plan_entry import generate_tables_streamed
from decoy_engine.generation.synthesize import (
    generate_tables,
    generate_tables_from_generation_plan,
    generate_tables_streamed_from_generation_plan,
)
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile
from tests.unit._gen_cli_golden import FIXTURES, graph_config

REPO = Path(__file__).resolve().parents[3]
GOLDEN = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))
SEEDS = ("0", "1", "2", "3", "17", "4242")

_CHILD = """
import json, sys
from decoy_engine.generation import generation_order
from decoy_engine.generation._plan_entry import generate_tables_streamed
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile
from tests.unit._gen_cli_golden import digests, graph_config

config = graph_config()
plan = compile_plan(config, empty_profile(), decoy_engine_version="test")
resident = generate_tables(plan)
streamed = generate_tables_streamed(plan, sink=None).tables
print(json.dumps({
    "set_order": list({"p1", "p2"}),
    "order": [[name, list(parents)] for name, parents in generation_order(config)],
    "resident_keys": list(resident),
    "streamed_keys": list(streamed),
    "digests": digests(resident),
    "streamed_digests": digests(streamed),
}))
"""


def _run_child(seed: str) -> dict[str, Any]:
    env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(REPO)}
    done = subprocess.run(  # noqa: S603 -- static argv, no untrusted input
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        cwd=REPO,
        env=env,
        timeout=300,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def runs() -> dict[str, dict[str, Any]]:
    return {seed: _run_child(seed) for seed in SEEDS}


def test_the_seed_list_can_fail_a_set_based_order(runs: dict[str, dict[str, Any]]) -> None:
    """Non-vacuity: two of the seeds iterate {"p1", "p2"} in different orders."""
    assert len({tuple(r["set_order"]) for r in runs.values()}) > 1


def test_order_and_result_keys_are_equal_across_seeds_and_routes(
    runs: dict[str, dict[str, Any]],
) -> None:
    first = runs[SEEDS[0]]
    for run in runs.values():
        assert run["order"] == first["order"]
        assert run["resident_keys"] == run["streamed_keys"] == [n for n, _ in first["order"]]
    names = [name for name, _ in first["order"]]
    assert names.index("p1") < names.index("c") < names.index("d")
    assert names.index("p2") < names.index("c")
    assert dict(map(tuple, first["order"]))["c"] == ["p2", "p1"]
    assert dict(map(tuple, first["order"]))["d"] == ["c", "p1"]


def test_every_table_keeps_the_bytes_captured_on_the_base(
    runs: dict[str, dict[str, Any]],
) -> None:
    for run in runs.values():
        assert run["digests"] == GOLDEN["graph"]["tables"]
        assert run["streamed_digests"] == GOLDEN["graph"]["tables"]


def test_a_single_parent_chain_keeps_declaration_driven_order() -> None:
    config = graph_config()
    config["tables"] = [config["tables"][i] for i in (3, 2, 1, 0)]
    order = [name for name, _ in generation_order(config)]
    assert order.index("p1") < order.index("c") < order.index("d")
    assert order.index("p2") < order.index("c")


# AT-25

MESSAGE = "table 'orders' column 'customer_id': reference_table 'customers' is not a generate table"


def _mask_parent_config() -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 1},
        "tables": [
            {"name": "customers", "columns": [{"name": "id", "strategy": "redact"}]},
            {
                "name": "orders",
                "row_count": 3,
                "generate_columns": [
                    {
                        "name": "customer_id",
                        "type": "reference",
                        "reference_table": "customers",
                        "reference_column": "id",
                    }
                ],
            },
        ],
    }


def test_generation_order_raises_for_a_reference_to_a_non_generate_table() -> None:
    with pytest.raises(ValueError) as exc:
        generation_order(_mask_parent_config())
    assert str(exc.value) == MESSAGE


def test_every_entry_raises_the_same_error_for_a_forged_plan() -> None:
    config = graph_config()
    plan = compile_plan(config, empty_profile(), decoy_engine_version="test")
    assert plan.generation is not None
    forged_config = _mask_parent_config()
    forged_generation = dataclasses.replace(
        plan.generation, config_json=json.dumps(forged_config, sort_keys=True)
    )
    forged_plan = dataclasses.replace(plan, generation=forged_generation)
    for call in (
        lambda: generate_tables(forged_plan),
        lambda: generate_tables_streamed(forged_plan, sink=None),
        lambda: generate_tables_from_generation_plan(forged_generation),
        lambda: generate_tables_streamed_from_generation_plan(forged_generation, sink=None),
    ):
        with pytest.raises(ValueError) as exc:
            call()
        assert str(exc.value) == MESSAGE


# GEN-REL: a self-reference is a tree inside one table, not a dependency between tables


def _tree_tables() -> list[dict[str, Any]]:
    key = {"name": "id", "type": "sequence", "start": 1}
    return [
        {
            "name": "employees",
            "row_count": 12,
            "generate_columns": [
                key,
                {
                    "name": "manager_id",
                    "type": "reference",
                    "reference_table": "employees",
                    "reference_column": "id",
                },
                {
                    "name": "dept",
                    "type": "reference",
                    "reference_table": "depts",
                    "reference_column": "id",
                },
            ],
        },
        {"name": "depts", "row_count": 3, "generate_columns": [key]},
    ]


def test_a_self_reference_is_not_a_dependency_edge() -> None:
    config = {"version": 2, "global_settings": {"seed": 1}, "tables": _tree_tables()}
    assert generation_order(config) == (("depts", ()), ("employees", ("depts",)))


def test_a_table_that_only_references_itself_has_no_parents() -> None:
    tables = _tree_tables()[:1]
    del tables[0]["generate_columns"][2]
    config = {"version": 2, "global_settings": {"seed": 1}, "tables": tables}
    assert generation_order(config) == (("employees", ()),)
