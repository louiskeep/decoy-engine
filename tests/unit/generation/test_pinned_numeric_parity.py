"""Every route types a number from the full config, relationships included.

A sequence related to a text categorical must stay text on the whole-table, window, admission and
pinned routes alike; a related reference child keeps it numeric on all four. The pinned plan keeps
no relationships, so it carries the compile-time decision instead.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._plan_entry import generate_tables
from decoy_engine.generation.pinned import (
    PinnedPlan,
    compile_pinned_plan,
    execute_pinned,
    fingerprint,
)
from decoy_engine.generation.window import generate_rows
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile

SEED = 77
REF = datetime(2026, 10, 22, tzinfo=timezone.utc)
SEQ = {"name": "id", "type": "sequence", "start": 1, "step": 1}
CAT = {"name": "pid", "type": "categorical", "categories": ["1", "2", "3"]}
REFCOL = {"name": "pid", "type": "reference", "reference_table": "people", "reference_column": "id"}


def _cfg(child_col: dict[str, Any]) -> dict[str, Any]:
    return {
        "global_settings": {"seed": SEED},
        "tables": [
            {"name": "people", "row_count": 3, "generate_columns": [SEQ]},
            {"name": "accounts", "row_count": 3, "generate_columns": [child_col]},
        ],
        "relationships": [
            {
                "parent": {"table": "people", "columns": ["id"]},
                "children": [{"table": "accounts", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }


def _routes(cfg: dict[str, Any]) -> dict[str, pa.Table]:
    plan = compile_plan(copy.deepcopy(cfg), empty_profile(), decoy_engine_version="test")
    return {
        "whole": generate_tables(plan)["people"],
        "window": generate_rows(plan, table="people", offset=0, count=3, seed=SEED).table,
        "pinned": execute_pinned(compile_pinned_plan(copy.deepcopy(cfg), REF), SEED)["people"],
    }


@pytest.mark.parametrize(
    ("child", "want_type"), [(CAT, pa.string()), (REFCOL, pa.int64())], ids=["text", "numeric"]
)
def test_four_routes_agree_on_dtype_and_values(child: dict[str, Any], want_type: Any) -> None:
    cfg = _cfg(child)
    out = _routes(cfg)
    admitted = next(
        c
        for c in classify_plan(copy.deepcopy(cfg)).columns
        if (c.table, c.column) == ("people", "id")
    )
    assert admitted.arrow_type == want_type
    for name, tbl in out.items():
        assert tbl.schema.field("id").type == want_type, name
    values = {name: tbl["id"].to_pylist() for name, tbl in out.items()}
    assert values["whole"] == values["window"] == values["pinned"]
    assert values["whole"] == ([1, 2, 3] if want_type == pa.int64() else ["1", "2", "3"])


def test_plan_without_numeric_columns_keeps_its_old_bytes() -> None:
    cfg = {
        "global_settings": {"seed": SEED},
        "tables": [{"name": "t", "row_count": 3, "generate_columns": [CAT]}],
    }
    plan = compile_pinned_plan(cfg, REF)
    assert plan.numeric_types == ()
    assert "numeric_types" not in plan.to_dict()
    # The same payload without the field, hashed by the pre-field recipe, gives the same digest.
    assert fingerprint(plan) == fingerprint(PinnedPlan.from_dict(plan.to_dict()))


def test_numeric_plan_round_trips_and_moves_the_fingerprint() -> None:
    plan = compile_pinned_plan(_cfg(REFCOL), REF)
    assert plan.numeric_types == (("people", "id", "int64"),)
    again = PinnedPlan.from_dict(json.loads(json.dumps(plan.to_dict())))
    assert again == plan and fingerprint(again) == fingerprint(plan)
    stripped = PinnedPlan.from_dict(
        {k: v for k, v in plan.to_dict().items() if k != "numeric_types"}
    )
    assert fingerprint(stripped) != fingerprint(plan)


def test_old_v1_plan_dict_without_the_field_still_loads_and_runs_as_text() -> None:
    plan = compile_pinned_plan(_cfg(REFCOL), REF)
    old = {k: v for k, v in plan.to_dict().items() if k != "numeric_types"}
    out = execute_pinned(PinnedPlan.from_dict(old), SEED)
    assert out["people"].schema.field("id").type == pa.string()


def test_unknown_numeric_type_name_is_a_malformed_plan() -> None:
    from decoy_engine._reference_date import PinnedModeError

    data = compile_pinned_plan(_cfg(REFCOL), REF).to_dict()
    data["numeric_types"] = [["people", "id", "decimal"]]
    with pytest.raises(PinnedModeError):
        PinnedPlan.from_dict(data)
