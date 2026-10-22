"""GEN-API Slice 0: the windowed `generate_rows` primitive and its Class-A admission gate."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.generation._plan_entry import generate_tables
from decoy_engine.generation._window_admission import (
    GenerationAdmissionError,
    GenerationWindowError,
    GenerationWindowTimeoutError,
    classify_window_table,
)
from decoy_engine.generation.statistical import sample_column, spec_from_dict
from decoy_engine.generation.window import classify_window, generate_rows
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile

SEED = 4242
ROWS = 300


def _source_df() -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(7)
    n = 2_000
    states = rng.choice(["CA", "NY", "TX", "WA"], size=n, p=[0.5, 0.3, 0.15, 0.05])
    tier = [{"CA": "gold", "NY": "silver"}.get(s, "bronze") for s in states]
    return pd.DataFrame(
        {
            "amount": rng.normal(100.0, 25.0, size=n).round(2),
            "state": states,
            "tier": tier,
            "joined": pd.to_datetime("2020-01-01")
            + pd.to_timedelta(rng.integers(0, 365 * 5, size=n), unit="D"),
            "note": ["x" * int(k) for k in rng.integers(3, 40, size=n)],
        }
    )


def _snapshot(tmp_path: Any) -> str:
    from decoy_engine.quality.snapshot import compute_distribution_snapshot

    snap = compute_distribution_snapshot(_source_df(), joint_columns=[("state", "tier")])
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _class_a_plan(tmp_path: Any) -> Any:
    snap = _snapshot(tmp_path)

    def stat(name: str, **extra: Any) -> dict[str, Any]:
        return {"name": name, "type": "statistical", "snapshot_file": snap, **extra}

    cols = [
        {"name": "id", "type": "sequence", "start": 100, "step": 3, "prefix": "R", "pad_length": 8},
        stat("state", allow_real_categories=True),
        # The BR-1 trap guard: conditioned on a parent that is also in the window.
        stat("tier", allow_real_categories=True, condition_on="state"),
        stat("amount"),
        stat("joined"),
        stat("note"),
        {"name": "who", "type": "faker", "faker_type": "first_name"},
        {
            "name": "seen",
            "type": "windowed_date",
            "anchor": "joined",
            "min_days": 1,
            "max_days": 30,
        },
    ]
    cfg = {
        "global_settings": {"seed": SEED},
        "tables": [{"name": "t", "row_count": ROWS, "generate_columns": cols}],
    }
    return compile_plan(cfg, empty_profile(), decoy_engine_version="test")


def _full(plan: Any) -> pa.Table:
    return generate_tables(plan)["t"]


# ---- parity + determinism ---------------------------------------------------------------------


def test_window_equals_slice_of_full_run_with_conditional_categorical(tmp_path: Any) -> None:
    plan = _class_a_plan(tmp_path)
    full = _full(plan)
    win = generate_rows(plan, table="t", offset=30, count=100, seed=SEED)
    assert win.rows_emitted == 100 and not win.capped
    assert win.table.num_rows == 100
    assert win.table.equals(full.slice(30, 100))
    # The conditional column is genuinely conditioned, so the trap is live: CA rows are gold.
    states, tiers = win.table.column("state").to_pylist(), win.table.column("tier").to_pylist()
    ca = [t for s, t in zip(states, tiers, strict=True) if s == "CA"]
    assert ca and ca.count("gold") / len(ca) > 0.8


@pytest.mark.parametrize("offset", [0, 1, 77, 250])
def test_parity_at_several_offsets(tmp_path: Any, offset: int) -> None:
    plan = _class_a_plan(tmp_path)
    full = _full(plan)
    count = min(50, ROWS - offset)
    win = generate_rows(plan, table="t", offset=offset, count=count, seed=SEED)
    assert win.table.equals(full.slice(offset, count))


def test_chunk_size_does_not_change_values(tmp_path: Any) -> None:
    plan = _class_a_plan(tmp_path)
    a = generate_rows(plan, table="t", offset=11, count=120, seed=SEED, chunk_rows=7)
    b = generate_rows(plan, table="t", offset=11, count=120, seed=SEED, chunk_rows=4096)
    assert a.table.equals(b.table)


def test_deterministic_across_calls(tmp_path: Any) -> None:
    plan = _class_a_plan(tmp_path)
    a = generate_rows(plan, table="t", offset=30, count=100, seed=SEED)
    b = generate_rows(plan, table="t", offset=30, count=100, seed=SEED)
    assert a.table.to_pylist() == b.table.to_pylist()
    other = generate_rows(plan, table="t", offset=30, count=100, seed=SEED + 1)
    assert other.table.to_pylist() != a.table.to_pylist()


def test_parity_on_the_streamed_core_route() -> None:
    cols = [
        {"name": "id", "type": "sequence", "start": 1},
        {"name": "dob", "type": "faker", "faker_type": "date_of_birth"},
        {"name": "ev", "type": "windowed_date", "anchor": "dob", "min_days": 1, "max_days": 30},
        {"name": "city", "type": "faker", "faker_type": "city"},
    ]
    cfg = {
        "global_settings": {"seed": 9},
        "tables": [{"name": "t", "row_count": 200, "generate_columns": cols}],
    }
    plan = compile_plan(cfg, empty_profile(), decoy_engine_version="test")
    full = generate_tables(plan)["t"]
    win = generate_rows(plan, table="t", offset=30, count=100, seed=9)
    assert win.table.to_pylist() == full.slice(30, 100).to_pylist()


def test_sample_column_start_is_the_absolute_seed_offset(tmp_path: Any) -> None:
    plan = _class_a_plan(tmp_path)
    specs = {
        (s.table_name, s.column_name): spec_from_dict(_thaw(s.spec))
        for s in plan.generation.statistical_specs
    }
    spec = specs[("t", "amount")]
    whole = sample_column(spec, 60, col_seed=5)
    assert sample_column(spec, 20, col_seed=5, start=40) == whole[40:]


def _thaw(frozen: Any) -> Any:
    from decoy_engine.plan._types import unfreeze_json

    return unfreeze_json(frozen)


def test_conditional_sample_uses_absolute_seed_and_local_parent(tmp_path: Any) -> None:
    """Two-index contract: seed by `start + i`, parent by the local `i`."""
    plan = _class_a_plan(tmp_path)
    spec = next(
        spec_from_dict(_thaw(s.spec))
        for s in plan.generation.statistical_specs
        if s.column_name == "tier"
    )
    parents = ["CA", "NY", "TX", "WA"] * 15
    whole = sample_column(spec, 60, col_seed=1, parent_values=parents)
    window = sample_column(spec, 20, col_seed=1, parent_values=parents[40:], start=40)
    assert window == whole[40:]


def test_budget_caps_at_a_whole_row_and_reports_bytes(tmp_path: Any) -> None:
    plan = _class_a_plan(tmp_path)
    full = generate_rows(plan, table="t", offset=0, count=50, seed=SEED)
    per_row = full.bytes_emitted // 50
    capped = generate_rows(
        plan, table="t", offset=0, count=50, seed=SEED, budget_bytes=per_row * 10, chunk_rows=8
    )
    assert capped.capped and 0 < capped.rows_emitted < 50
    assert capped.bytes_emitted <= per_row * 10
    assert capped.table.equals(full.table.slice(0, capped.rows_emitted))
    none_fit = generate_rows(plan, table="t", offset=0, count=5, seed=SEED, budget_bytes=1)
    assert none_fit.rows_emitted == 0 and none_fit.capped


def test_deadline_raises_typed_timeout(tmp_path: Any) -> None:
    plan = _class_a_plan(tmp_path)
    with pytest.raises(GenerationWindowTimeoutError) as exc:
        generate_rows(plan, table="t", offset=0, count=10, seed=SEED, deadline_s=-1.0)
    assert exc.value.code == "generation_window_timeout"


@pytest.mark.parametrize(("offset", "count"), [(-1, 5), (0, -1), (True, 5), (1.5, 5), (2**63, 1)])
def test_bad_window_arguments(tmp_path: Any, offset: Any, count: Any) -> None:
    plan = _class_a_plan(tmp_path)
    with pytest.raises(GenerationWindowError) as exc:
        generate_rows(plan, table="t", offset=offset, count=count, seed=SEED)
    assert exc.value.code == "generation_window_invalid"


# ---- admission --------------------------------------------------------------------------------


def _spec(kind: str, stats: dict[str, Any], **kw: Any) -> Any:
    base: dict[str, Any] = {
        "column": "c",
        "source_column": "c",
        "kind": kind,
        "dtype": "float64",
        "stats": stats,
        "other_mode": "redistribute",
        "condition_on": None,
        "joint": None,
        "parent_first": True,
    }
    return spec_from_dict({**base, **kw})


def _verdict(col: dict[str, Any], spec: Any = None, **kw: Any) -> Any:
    col = {"name": "c", **col}
    cfg = {"tables": [{"name": "t", "row_count": 10, "generate_columns": [col]}]}
    specs = {("t", "c"): spec} if spec is not None else {}
    return classify_window_table(cfg, "t", statistical_specs=specs, **kw)


def _numeric(edges: list[Any], counts: list[Any]) -> Any:
    return _spec("numeric", {"bin_edges": edges, "bin_counts": counts})


def _stat_col() -> dict[str, Any]:
    return {"type": "statistical"}


def test_statistical_numeric_is_class_a() -> None:
    assert _verdict(_stat_col(), _numeric([0.0, 1.0, 2.0], [1, 1])).is_class_a


@pytest.mark.parametrize(
    ("col", "spec", "code", "prop"),
    [
        ({"type": "formula", "formula": "1 + 1"}, None, "generation_unbounded_column", "type"),
        (_stat_col(), _numeric([-1e308, 1e308], [1]), "generation_unbounded_column", "bin_edges"),
        (
            _stat_col(),
            _numeric([0, float("nan"), 2], [1, 1]),
            "generation_unbounded_column",
            "bin_edges",
        ),
        (_stat_col(), _numeric([0, float("inf")], [1]), "generation_unbounded_column", "bin_edges"),
        (_stat_col(), _numeric([0, 2, 1], [1, 1]), "generation_unbounded_column", "bin_edges"),
        (_stat_col(), _numeric([0.0], []), "statistical_stats_degenerate", "bin_edges"),
        (_stat_col(), _numeric([0, 1, 2], [1]), "statistical_stats_degenerate", "bin_edges"),
        (_stat_col(), _numeric([0, 1, 2], [0, 0]), "statistical_stats_degenerate", "bin_counts"),
        (
            _stat_col(),
            _spec("freetext", {"length_bin_edges": [0, float("inf")], "length_bin_counts": [1]}),
            "generation_unbounded_column",
            "length_bin_edges",
        ),
        (
            _stat_col(),
            _spec("freetext", {"length_bin_edges": [0, 10**6], "length_bin_counts": [1]}),
            "generation_unbounded_column",
            "length_bin_edges",
        ),
        (
            _stat_col(),
            _spec(
                "datetime",
                {
                    "year_bins": [{"year": 9999, "count": 5}],
                    "min": "2000-01-01",
                    "max": "2001-01-01",
                },
            ),
            "generation_unbounded_column",
            "year_bins",
        ),
        (
            _stat_col(),
            _spec(
                "datetime",
                {
                    "year_bins": [{"year": 2000, "count": 5}],
                    "min": "2000-01-01T00:00:00+00:00",
                    "max": "2001-01-01",
                },
            ),
            "generation_unbounded_column",
            "min",
        ),
        (
            _stat_col(),
            _spec(
                "datetime",
                {
                    "year_bins": [{"year": 2000, "count": 5}],
                    "min": "2000-01-01",
                    "max": "2001-01-01T00:00:00Z",
                },
            ),
            "generation_unbounded_column",
            "max",
        ),
        (
            _stat_col(),
            _spec("datetime", {"year_bins": [], "min": "2000-01-01", "max": "2001-01-01"}),
            "statistical_stats_degenerate",
            "year_bins",
        ),
        (
            _stat_col(),
            _spec("categorical", {"top_values": []}),
            "statistical_stats_degenerate",
            "top_values",
        ),
        (
            _stat_col(),
            _spec("categorical", {"top_values": [{"value": "x" * 50, "count": 3}]}, dtype="object"),
            "generation_unbounded_column",
            "top_values",
        ),
    ],
)
def test_refusals_are_coded_admission_errors(
    col: dict[str, Any], spec: Any, code: str, prop: str
) -> None:
    with pytest.raises(GenerationAdmissionError) as exc:
        _verdict(col, spec, max_cell_chars=32)
    assert exc.value.code == code and exc.value.prop == prop
    assert exc.value.column == "c" and exc.value.table == "t"


def test_sampled_year_check_ignores_zero_weight_bins() -> None:
    spec = _spec(
        "datetime",
        {
            "year_bins": [{"year": 2000, "count": 5}, {"year": 9999, "count": 0}],
            "min": "2000-01-01",
            "max": "2001-01-01",
        },
    )
    assert _verdict(_stat_col(), spec).is_class_a


def test_refusal_wins_over_class_b() -> None:
    cfg = {
        "tables": [
            {
                "name": "t",
                "row_count": 10,
                "generate_columns": [
                    {"name": "a", "type": "categorical", "categories": ["x"]},
                    {"name": "b", "type": "formula", "formula": "1"},
                ],
            }
        ]
    }
    with pytest.raises(GenerationAdmissionError) as exc:
        classify_window_table(cfg, "t", statistical_specs={})
    assert exc.value.column == "b"


def test_clock_faker_is_refused_not_reproducible() -> None:
    with pytest.raises(GenerationAdmissionError) as exc:
        _verdict({"type": "faker", "faker_type": "date_time_this_year"})
    assert exc.value.code == "generation_not_reproducible"
    assert _verdict({"type": "faker", "faker_type": "date_of_birth"}).is_class_a


@pytest.mark.parametrize(
    ("col", "rows", "reason"),
    [
        ({"type": "categorical", "categories": ["a", "b"]}, 10, "family_class_b:categorical"),
        ({"type": "faker", "faker_type": "first_name"}, 60_000, "pooled_faker"),
        ({"type": "sequence", "null_probability": 0.2}, 10, "null_injection"),
        (
            {"type": "reference", "reference_table": "p", "reference_column": "id"},
            10,
            "family_class_b:reference",
        ),
        ({"type": "derived", "expression": "1"}, 10, "family_class_b:derived"),
    ],
)
def test_non_class_a_families_classify_as_class_b(
    col: dict[str, Any], rows: int, reason: str
) -> None:
    cfg = {"tables": [{"name": "t", "row_count": rows, "generate_columns": [{"name": "c", **col}]}]}
    verdict = classify_window_table(cfg, "t", statistical_specs={})
    assert verdict.klass == "B" and verdict.class_b_reasons == (("c", reason),)


def test_generate_rows_refuses_class_b_table() -> None:
    cols = [{"name": "c", "type": "categorical", "categories": ["a", "b"]}]
    cfg = {
        "global_settings": {"seed": 1},
        "tables": [{"name": "t", "row_count": 10, "generate_columns": cols}],
    }
    plan = compile_plan(cfg, empty_profile(), decoy_engine_version="test")
    assert classify_window(plan, "t").klass == "B"
    with pytest.raises(GenerationAdmissionError) as exc:
        generate_rows(plan, table="t", offset=0, count=5, seed=1)
    assert exc.value.code == "generation_not_class_a"


def test_unknown_table() -> None:
    with pytest.raises(GenerationWindowError) as exc:
        classify_window_table({"tables": []}, "nope", statistical_specs={})
    assert exc.value.code == "generation_table_unknown"


def test_sequence_cell_width_is_capped() -> None:
    with pytest.raises(GenerationAdmissionError):
        _verdict({"type": "sequence", "pad_length": 10_000}, max_cell_chars=100)


def test_overflowing_span_still_refused_when_dataclass_replaced() -> None:
    spec = replace(
        _numeric([0.0, 1.0], [1]), stats={"bin_edges": [-1e308, 1e308], "bin_counts": [1]}
    )
    with pytest.raises(GenerationAdmissionError):
        _verdict(_stat_col(), spec)
