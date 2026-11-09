"""Columns with identical settings draw independent streams.

A column's draw derives from the seed (or key and label), its strategy
fingerprint, and its (table, column) identity. These tests pin that two
columns with the same settings never repeat each other, that output stays
deterministic, and that adding a column or growing row_count leaves other
columns unchanged.
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pytest

from decoy_engine.context import make_key_resolver
from decoy_engine.generation._plan_entry import generate_tables
from decoy_engine.generators.derivation import GenDeriveContext
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile

SEED = 20260
ROWS = 400
_MASTER = bytes(range(32))


def _plan(cols: list[dict[str, Any]], *, rows: int = ROWS, table: str = "t", seed: int = SEED):
    cfg = {
        "global_settings": {"seed": seed},
        "tables": [{"name": table, "row_count": rows, "generate_columns": cols}],
    }
    return compile_plan(cfg, empty_profile(), decoy_engine_version="test")


def _gen(cols, *, key: str | None = None, **kw) -> dict[str, list[Any]]:
    derive_key = make_key_resolver(_MASTER, key) if key else None
    tbl = generate_tables(_plan(cols, **kw), derive_key=derive_key)[kw.get("table", "t")]
    return {n: tbl.column(n).to_pylist() for n in tbl.column_names}


def _pair(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": "a", **spec}, {"name": "b", **spec}]


_SPECS = {
    "faker": {"type": "faker", "faker_type": "first_name"},
    "date_between": {
        "type": "faker",
        "faker_type": "date_between",
        "faker_kwargs": {"start_date": "-5y", "end_date": "today"},
    },
    "categorical": {"type": "categorical", "categories": list("abcdefgh")},
    "random_int": {
        "type": "faker",
        "faker_type": "random_int",
        "faker_kwargs": {"min": 0, "max": 10**6},
    },
}


@pytest.mark.parametrize("name", sorted(_SPECS))
@pytest.mark.parametrize("key", [None, "label-1"])
def test_identical_settings_differ_row_wise(name: str, key: str | None) -> None:
    out = _gen(_pair(_SPECS[name]), key=key)
    same = sum(x == y for x, y in zip(out["a"], out["b"], strict=True))
    assert same < ROWS * 0.5, f"{name}: {same}/{ROWS} rows identical"


@pytest.mark.parametrize("name", sorted(_SPECS))
@pytest.mark.parametrize("key", [None, "label-1"])
def test_same_inputs_are_byte_identical(name: str, key: str | None) -> None:
    assert _gen(_pair(_SPECS[name]), key=key) == _gen(_pair(_SPECS[name]), key=key)


def test_seed_and_label_still_change_output() -> None:
    cols = _pair(_SPECS["categorical"])
    assert _gen(cols, seed=1) != _gen(cols, seed=2)
    assert _gen(cols, key="l1") != _gen(cols, key="l2")


def test_same_column_name_in_two_tables_differs() -> None:
    cfg = {
        "global_settings": {"seed": SEED},
        "tables": [
            {"name": t, "row_count": ROWS, "generate_columns": [{"name": "x", **_SPECS["faker"]}]}
            for t in ("t1", "t2")
        ],
    }
    out = generate_tables(compile_plan(cfg, empty_profile(), decoy_engine_version="test"))
    assert out["t1"].column("x").to_pylist() != out["t2"].column("x").to_pylist()


def test_sequence_columns_are_deterministic() -> None:
    cols = _pair({"type": "sequence", "start": 1, "step": 2})
    out = _gen(cols)
    assert out["a"] == out["b"] == [str(i) for i in range(1, 2 * ROWS, 2)]


def test_statistical_columns_from_independent_sources_are_uncorrelated(tmp_path) -> None:
    from decoy_engine.quality.snapshot import compute_distribution_snapshot

    rng = np.random.default_rng(7)
    df = pd.DataFrame(
        {"a": rng.normal(100, 25, 3000).round(2), "b": rng.normal(100, 25, 3000).round(2)}
    )
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(compute_distribution_snapshot(df)), encoding="utf-8")
    cols = _pair({"type": "statistical", "snapshot_file": str(path), "source_column": "a"})
    out = _gen(cols, rows=2000)
    corr = float(np.corrcoef(out["a"], out["b"])[0, 1])
    assert abs(corr) < 0.2, corr


def test_adding_a_column_or_growing_rows_keeps_existing_values() -> None:
    tail = [*_pair(_SPECS["faker"]), {"name": "c", **_SPECS["categorical"]}]
    base = _gen(tail)
    more_cols = _gen([{"name": "z", **_SPECS["date_between"]}, *tail])
    grown = _gen(tail, rows=ROWS * 2)
    for n in ("a", "b", "c"):
        assert more_cols[n] == base[n]
        assert grown[n][:ROWS] == base[n]


def test_identity_separator_cannot_collide() -> None:
    from decoy_engine.generators.derivation import with_table

    cfg = {"type": "categorical", "categories": ["x", "y"]}
    a = GenDeriveContext.for_column(None, with_table({**cfg, "name": "b,c"}, "a"), 5)
    b = GenDeriveContext.for_column(None, with_table({**cfg, "name": "c"}, "a,b"), 5)
    assert a.base_int("py") != b.base_int("py")


def _two_tables(col: dict[str, Any]) -> dict[str, list[Any]]:
    """The same table twice under different names. The anchor columns are constant, so only the
    derived column's own stream can make the two outputs differ."""
    base = [
        {"name": "g", "type": "categorical", "categories": ["a"]},
        {"name": "d", "type": "categorical", "categories": ["2020-01-01"]},
        {"name": "o", "type": "sequence", "start": 1},
    ]
    cfg = {
        "global_settings": {"seed": SEED},
        "tables": [
            {"name": t, "row_count": 200, "generate_columns": [*base, col]} for t in ("t1", "t2")
        ],
    }
    out = generate_tables(compile_plan(cfg, empty_profile(), decoy_engine_version="test"))
    return {t: tbl.column("x").to_pylist() for t, tbl in out.items()}


@pytest.mark.parametrize(
    "col",
    [
        {
            "name": "x",
            "type": "grouped_series",
            "group_by": "g",
            "order_by": "o",
            "generator": "monotone_walk",
            "max_step": 50,
        },
        {"name": "x", "type": "windowed_date", "anchor": "d", "min_days": 1, "max_days": 3000},
        {"name": "x", "type": "group_key", "group_by": "g"},
    ],
    ids=lambda c: c["type"],
)
def test_derived_kinds_with_same_name_in_two_tables_differ(col: dict[str, Any]) -> None:
    out = _two_tables(col)
    assert out["t1"] != out["t2"]
    assert out == _two_tables(col)


def test_context_without_a_table_raises() -> None:
    cfg = {"name": "x", "type": "categorical", "categories": ["a", "b"]}
    with pytest.raises(ValueError, match="no table"):
        GenDeriveContext.for_column(None, cfg, 5)
    with pytest.raises(ValueError, match="no table"):
        GenDeriveContext.for_column(make_key_resolver(_MASTER, "l"), cfg, 5)
