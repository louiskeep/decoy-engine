"""Fitted cell format across every generation route (AT-5, AT-6, AT-7, AT-12)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.generation.statistical import load_spec, sample_column, spec_to_dict
from decoy_engine.generation.window import classify_window, generate_rows
from tests.unit.generation._cf_routes_support import (
    ROWS,
    SEED,
    all_columns,
    config,
    digest,
    fitted,
    pinned,
    plan_of,
    route_frame,
    stat,
    whole,
    windows,
    write_snapshot,
)
from tests.unit.generation.statistical._cf_support import fit, none_rate

# Digests captured from engine main 5a7f14dc (before the sampler read `cell_format`) for a
# snapshot fitted WITHOUT the block, so any drift on the old path turns AT-7 red.
OLD_BYTES = "ef2f6b2b750ecd02cfa541ef775baca3d7b3fe5e6278afbc4f94606e4f251128"


def _without_numeric_shape(snap_file: str) -> str:
    """Drop the number-shape blocks a fit now adds, so the file is what an older engine wrote."""
    path = Path(snap_file)
    doc = json.loads(path.read_text())
    for col in doc["columns"].values():
        col["stats"].pop("quantile_grid", None)
        col["stats"].pop("exact_values", None)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return snap_file


def _old_outputs(tmp_path: Path) -> dict[str, str]:
    snap = _without_numeric_shape(fitted(tmp_path, cell_format=False))
    cfg = config(all_columns(snap, with_void=False))
    specs = [
        spec_to_dict(load_spec(c, snapshot=json.loads(Path(snap).read_text())))
        for c in cfg["tables"][0]["generate_columns"]
    ]
    return {
        "whole": digest(whole(cfg)),
        "pinned": digest(pinned(cfg)),
        "windows": digest(windows(cfg)),
        "specs": json.dumps(specs, sort_keys=True, default=str),
    }


def test_old_snapshots_keep_their_bytes_on_every_route(tmp_path: Path) -> None:
    got = _old_outputs(tmp_path)
    assert [got["whole"], got["pinned"], got["windows"]] == [OLD_BYTES] * 3
    assert "cell_format" not in got["specs"]


# AT-5 route matrix


def test_every_route_gives_equal_bytes_and_schema(tmp_path: Path) -> None:
    cfg = config(all_columns(fitted(tmp_path)))
    a, b, c = whole(cfg), windows(cfg), pinned(cfg)
    assert a.schema == b.schema == c.schema
    assert a.to_pylist() == b.to_pylist() == c.to_pylist()
    assert digest(a) == digest(b) == digest(c)


def test_each_kind_has_the_declared_arrow_type_and_fitted_nulls(tmp_path: Path) -> None:
    table = whole(config(all_columns(fitted(tmp_path))))
    types = {f.name: f.type for f in table.schema}
    assert types["price"] == pa.float64()
    assert types["tier"] == pa.string()
    assert types["seen"] == pa.timestamp("us")
    assert types["note"] == pa.string()
    assert types["qty"] == pa.int64()
    assert types["day"] == pa.date32()
    assert types["void"] == pa.null()
    for name in ("price", "tier", "seen", "note", "qty", "day"):
        assert table.column(name).null_count > 0, name
    assert table.column("void").null_count == ROWS
    assert all(type(v) is date for v in table.column("day").to_pylist() if v is not None)
    assert all(type(v) is int for v in table.column("qty").to_pylist() if v is not None)


def test_an_all_none_table_slice_and_window_keep_the_declared_type(tmp_path: Path) -> None:
    frame = route_frame()[["price"]].copy()
    frame.loc[frame.index[2:], "price"] = None  # two values in 1,000 rows
    file = write_snapshot(tmp_path / "sparse.json", fit(frame))
    cfg = config([stat(file, "price")], rows=4)
    table = whole(cfg)
    assert table.column("price").null_count == 4  # non-vacuity: every cell is None
    assert table.schema.field("price").type == pa.float64()
    window = generate_rows(plan_of(cfg), table="t", offset=0, count=4, seed=SEED).table
    assert window.schema == table.schema
    assert window.to_pylist() == table.to_pylist()


def test_a_zero_row_window_and_table_keep_the_declared_types(tmp_path: Path) -> None:
    cfg = config(all_columns(fitted(tmp_path)))
    full = whole(cfg)
    empty_window = generate_rows(plan_of(cfg), table="t", offset=0, count=0, seed=SEED).table
    assert empty_window.num_rows == 0 and empty_window.schema == full.schema
    zero = whole(config(all_columns(fitted(tmp_path)), rows=0))
    assert zero.num_rows == 0 and zero.schema == full.schema


def test_windowed_admission_stays_class_a_for_fitted_nulls_zero_and_empty(tmp_path: Path) -> None:
    snap = fitted(tmp_path)
    for extra in ({}, {"null_probability": 0}):
        verdict = classify_window(plan_of(config(all_columns(snap, **extra))), "t")
        assert verdict.is_class_a, (extra, verdict)


def test_positive_null_probability_is_class_b_but_resident_routes_agree(tmp_path: Path) -> None:
    cfg = config(all_columns(fitted(tmp_path), with_void=False, null_probability=0.1))
    assert not classify_window(plan_of(cfg), "t").is_class_a
    a, b = whole(cfg), pinned(cfg)
    assert a.schema == b.schema and a.to_pylist() == b.to_pylist()


# AT-12 explicit opt-out


def test_null_probability_zero_removes_fitted_nulls_and_positive_runs_the_post_pass(
    tmp_path: Path,
) -> None:
    snap = fitted(tmp_path)
    off = whole(config([stat(snap, "price", null_probability=0)], rows=2_000))
    assert off.column("price").null_count == 0  # the source had 10% nulls
    on = whole(config([stat(snap, "price", null_probability=0.1)], rows=2_000))
    assert abs(none_rate(on.column("price").to_pylist()) - 0.1) < 0.03
    spec = load_spec(
        stat(snap, "price", null_probability=0.1), snapshot=json.loads(Path(snap).read_text())
    )
    base = sample_column(spec, 2_000, col_seed=0)
    assert None not in base  # every None above came from the post-pass, not the sampler


# AT-6 interactions


def test_condition_on_a_parent_with_fitted_nulls_draws_the_marginal(tmp_path: Path) -> None:
    frame = route_frame()
    frame["grade"] = frame["tier"].map({"gold": "A", "silver": "B", "bronze": "C"})
    frame.loc[frame.index % 7 == 0, "grade"] = None
    snap = fit(frame, joint_columns=[("tier", "grade")])
    file = write_snapshot(tmp_path / "joint.json", snap)
    snapshot = json.loads(Path(file).read_text())
    child = load_spec(stat(file, "grade", condition_on="tier"), snapshot=snapshot)
    marginal = load_spec(stat(file, "grade"), snapshot=snapshot)
    parent = sample_column(load_spec(stat(file, "tier"), snapshot=snapshot), 600, col_seed=3)
    assert None in parent  # non-vacuity: the parent has fitted nulls
    cond = sample_column(child, 600, col_seed=5, parent_values=parent)
    free = sample_column(marginal, 600, col_seed=5)
    rows = [i for i, p in enumerate(parent) if p is None]
    assert [cond[i] for i in rows] == [free[i] for i in rows]
    table = whole(config([stat(file, "tier"), stat(file, "grade", condition_on="tier")]))
    assert table.num_rows == ROWS


def test_windowed_date_and_derived_follow_fitted_nulls(tmp_path: Path) -> None:
    snap = fitted(tmp_path)
    cols = [
        stat(snap, "day"),
        stat(snap, "price"),
        {"name": "due", "type": "windowed_date", "anchor": "day", "min_days": 1, "max_days": 30},
        {"name": "double", "type": "derived", "expression": "price * 2"},
    ]
    table = whole(config(cols, rows=400))
    rows = table.to_pylist()
    assert any(r["day"] is None for r in rows) and any(r["day"] is not None for r in rows)
    for r in rows:
        assert (r["due"] is None) == (r["day"] is None)
        assert (r["double"] is None) == (r["price"] is None)
        if r["day"] is not None:
            assert r["due"] is not None


def test_fidelity_gate_scores_a_day_column_as_datetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.generation import _fidelity_gate as gate

    seen: list[dict[str, Any]] = []
    real = gate.compute_fidelity

    def spy(source: Any, generated: Any) -> Any:
        report = real(source, generated)
        seen.append(report)
        return report

    monkeypatch.setattr(gate, "compute_fidelity", spy)
    whole(config([stat(fitted(tmp_path), "day")], rows=300))
    assert seen
    entry = next(c for c in seen[0]["marginal"]["columns"] if c["column"] == "day")
    assert entry["comparable"], entry


@pytest.mark.parametrize("cell_format", [False, True])
def test_dates_past_the_nanosecond_range_still_generate(tmp_path: Path, cell_format: bool) -> None:
    # A far-future sentinel (2500-12-31) fits at microsecond unit; the fidelity gate must not
    # narrow it to nanoseconds, which stop at 2262. Old snapshots (no block) generated on main.
    stamps = pd.Series(
        np.array(
            ["2020-01-01", "2021-06-01", "2500-12-31", "2022-02-02"] * 50, dtype="datetime64[us]"
        )
    )
    days = pd.Series([date(2020, 1, 1), date(2400, 6, 1), None, date(2021, 3, 3)] * 50)
    snap = fit(pd.DataFrame({"valid_to": stamps, "day": days}), cell_format=cell_format)
    snap_file = write_snapshot(tmp_path / "far.json", snap)
    table = whole(config([stat(snap_file, "valid_to"), stat(snap_file, "day")], rows=60))
    assert table.num_rows == 60
