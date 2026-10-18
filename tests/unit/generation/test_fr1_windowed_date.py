"""Phase F FR1: windowed_date on the unified generation core.

Covers admission (same-table, earlier, reproducible, supported-type anchor), resident == streamed
== legacy parity over irregular partitions, the CHANGED null/error contract on both the core and
the legacy whole-table path, and the strict anchor grammar. Parity is tested, not assumed from the
shared `apply_windowed_date`.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._reproducibility import reproducibility_exclusions
from decoy_engine.generation.synthesize import _generate_tables_from_config
from decoy_engine.transforms.windowed_date import (
    SUPPORTED_MAX,
    SUPPORTED_MIN,
    WindowedDateConfig,
    WindowedDateError,
    apply_windowed_date,
)
from tests.parity import _f0_fixtures as fx
from tests.parity._fr0_support import (
    fingerprints,
    partition_from_cuts,
    prepared_config,
    run_resident,
    run_streamed,
)

DOB: dict[str, Any] = {"name": "dob", "type": "faker", "faker_type": "date_of_birth"}


def _win(**kw: Any) -> dict[str, Any]:
    base = {"name": "ev", "type": "windowed_date", "anchor": "dob", "min_days": 1, "max_days": 30}
    return {**base, **kw}


def _cfg(rows: int, *cols: dict[str, Any]) -> dict[str, Any]:
    return fx.config([fx.table("w", rows, list(cols))])


def _anchor_values(values: list[Any]) -> dict[str, Any]:
    """A categorical anchor that cycles through `values` (None allowed via null_probability 0)."""
    return {"name": "dob", "type": "categorical", "categories": values}


def _ev_reason(cfg: dict[str, Any], *, compile_first: bool = True) -> str:
    """The windowed column's own decline reason (plan.reason() reports the FIRST decline)."""
    config = prepared_config(cfg) if compile_first else cfg
    adm = classify_plan(config)
    return next(c.reason for c in adm.columns if c.column == "ev")


# ---- admission ---------------------------------------------------------------------------------


def test_string_anchor_is_admitted_with_a_declared_string_schema() -> None:
    adm = classify_plan(prepared_config(_cfg(10, DOB, _win())))
    assert adm.eligible
    assert adm.schema("w").field("ev").type == pa.string()


def test_matrix_row_records_changed_semantics() -> None:
    from decoy_engine.generation._admission import ADMISSION_MATRIX

    ok, why = ADMISSION_MATRIX["windowed_date"]
    assert ok and "CHANGED" in why


@pytest.mark.parametrize(
    ("cols", "reason"),
    [
        ([_win(), DOB], "windowed_date_anchor_not_earlier"),
        ([{"name": "dob", "type": "sequence"}, _win()], None),
        (
            [{"name": "dob", "type": "categorical", "categories": [1, 2]}, _win()],
            "windowed_date_anchor_type_unsupported",
        ),
        (
            [{"name": "dob", "type": "formula", "formula": "'2024-01-01'"}, _win()],
            "windowed_date_anchor_ineligible",
        ),
        (
            [{"name": "dob", "type": "faker", "faker_type": "date_between"}, _win()],
            "windowed_date_anchor_not_reproducible",
        ),
        ([{**DOB, "determinism": "fresh"}, _win()], "windowed_date_anchor_not_reproducible"),
    ],
)
def test_anchor_declines(cols: list[dict[str, Any]], reason: str | None) -> None:
    cfg = _cfg(10, *cols)
    if reason is None:
        # A sequence anchor is a string column, so it is admitted; its values fail at run time.
        assert classify_plan(prepared_config(cfg)).eligible
    else:
        assert _ev_reason(cfg) == reason


def test_cross_table_anchor_declines() -> None:
    # The plan compiler already rejects a missing anchor, so classify the raw configuration: the
    # admission is a second, independent guard for a caller that bypasses compile.
    cfg = fx.config([fx.table("a", 5, [DOB]), fx.table("b", 5, [_win()])])
    assert _ev_reason(cfg, compile_first=False) == "windowed_date_anchor_not_in_table"


def test_reproducibility_closure_still_follows_the_anchor() -> None:
    cfg = prepared_config(
        _cfg(10, {"name": "dob", "type": "faker", "faker_type": "date_between"}, _win())
    )
    excl = reproducibility_exclusions(cfg)
    assert ("w", "dob") in excl and ("w", "ev") in excl


@pytest.mark.parametrize("arrow_type", [pa.date32(), pa.timestamp("us")])
def test_typed_anchor_types_are_supported_by_the_helper(arrow_type: pa.DataType) -> None:
    from decoy_engine.generation._admission import _anchor_type_supported

    assert _anchor_type_supported(arrow_type)
    assert not _anchor_type_supported(pa.timestamp("us", tz="UTC"))
    assert not _anchor_type_supported(pa.int64())


# ---- parity: resident == streamed == legacy ---------------------------------------------------

_DATETIME_ANCHORS = ["2020-02-29", "1999-12-31T23:59:59", "2001-05-06 07:08:09.123456"]
PARITY_CFGS = {
    "plain": lambda: _cfg(60, DOB, _win(distribution="early", min_days=-20)),
    "null_mask_anchor": lambda: _cfg(
        60, {**DOB, "null_probability": 0.3}, _win(distribution="late")
    ),
    "null_mask_windowed": lambda: _cfg(60, DOB, _win(null_probability=0.25)),
    "two_windows": lambda: _cfg(60, DOB, _win(name="a"), _win(name="b", max_days=400)),
    "datetime_strings": lambda: _cfg(
        40,
        _anchor_values(_DATETIME_ANCHORS),
        _win(),
    ),
}


# A categorical anchor draws from the index kernel in the core (FR3) and from random.choices in the
# legacy path, so the two anchors differ by design. What must still agree is the windowed column: the
# core's value equals the legacy whole-table windowed generator applied to the CORE's anchor.
KERNEL_ANCHORED = {"datetime_strings"}


def _assert_windowed_matches_the_whole_table_helper(cfg: dict[str, Any], table: pa.Table) -> None:
    from decoy_engine.generation._grouped_windowed_generators import _windowed_date_generate
    from decoy_engine.plan._seed import _normalize_job_seed_int

    config = prepared_config(cfg)
    col = next(c for c in config["tables"][0]["generate_columns"] if c["name"] == "ev")
    anchors = table.column("dob").to_pylist()
    assert set(anchors) <= set(_DATETIME_ANCHORS)
    want = _windowed_date_generate(
        col, len(anchors), _normalize_job_seed_int(config), {"dob": anchors}
    )
    assert table.column("ev").to_pylist() == want


@pytest.mark.parametrize("name", PARITY_CFGS)
def test_resident_streamed_and_legacy_agree(name: str, tmp_path: Path) -> None:
    cfg = PARITY_CFGS[name]()
    with fx.frozen_faker_clock():
        whole = run_resident(cfg)
        cut = run_resident(cfg, partition_from_cuts({"w": [0, 0, 7, 7, 8, 31]}))
        singles = run_resident(cfg, partition_from_cuts({"w": list(range(1, 60))}))
        result, streamed = run_streamed(
            cfg, tmp_path / "out", partition_from_cuts({"w": [2, 9, 9, 40]})
        )
        legacy = _generate_tables_from_config(prepared_config(cfg), None, None)
    assert result.mode == "streamed"
    assert fingerprints(whole) == fingerprints(cut) == fingerprints(singles)
    assert fingerprints(whole) == fingerprints(streamed)
    cols = whole["w"].column_names
    if name in KERNEL_ANCHORED:
        _assert_windowed_matches_the_whole_table_helper(cfg, whole["w"])
    else:
        for col in cols:
            assert whole["w"].column(col).to_pylist() == legacy["w"].column(col).to_pylist(), col
    for tbl in (whole, cut, singles, streamed):
        assert tbl["w"].schema.field(cols[-1]).type == pa.string()


def test_pinned_oracle_digest() -> None:
    with fx.frozen_faker_clock():
        out = run_resident(PARITY_CFGS["plain"]())
    assert fingerprints(out)["w"]["table"] == PINNED_PLAIN


PINNED_PLAIN = "46ba575b6d64a0e1d833c140a9046a913fa6d7c24a42baffc84504df7eab2394"


# ---- null anchor: propagate, no draw ----------------------------------------------------------


def test_mixed_null_anchor_yields_null_and_leaves_other_rows_untouched() -> None:
    vals = ["2020-01-01", "2021-06-15", "2022-12-31"]
    cfg_full = _cfg(30, _anchor_values(vals), _win())
    cfg_null = _cfg(30, {**_anchor_values(vals), "null_probability": 0.4}, _win())
    full = run_resident(cfg_full)["w"]
    nulled = run_resident(cfg_null)["w"]
    dob = nulled.column("dob").to_pylist()
    ev = nulled.column("ev").to_pylist()
    assert any(v is None for v in dob) and any(v is not None for v in dob)
    for i, (d, e) in enumerate(zip(dob, ev, strict=True)):
        assert (e is None) == (d is None)
        if d is not None and full.column("dob")[i].as_py() == d:
            # Same anchor at the same row -> same offset: the null rows consumed no shared draw.
            assert e == full.column("ev")[i].as_py()


def test_all_null_anchor_column_gives_all_null_declared_string_output(tmp_path: Path) -> None:
    cfg = _cfg(12, {**DOB, "null_probability": 1.0}, _win())
    res = run_resident(cfg, partition_from_cuts({"w": [5]}))["w"]
    assert res.schema.field("ev").type == pa.string()
    assert res.column("ev").to_pylist() == [None] * 12
    _, streamed = run_streamed(cfg, tmp_path / "o", partition_from_cuts({"w": [3, 3]}))
    assert streamed["w"].schema.field("ev").type == pa.string()
    legacy = _generate_tables_from_config(prepared_config(cfg), None, None)
    assert legacy["w"].column("ev").to_pylist() == [None] * 12


# ---- invalid anchors: WindowedDateError on the core AND the legacy path ------------------------

BAD_ANCHORS = [
    "now",
    "today",
    "yesterday",
    "2024-01-01T00:00:00Z",
    "2024-01-01T00:00:00+02:00",
    "2024-01-01 00:00:00 UTC",
    "Jan 1 2024",
    "01/02/2024",
    "2024-1-1",
    "20240101",
    "2024-01",
    "2024",
    "2024-02-30",
    "2024-13-01",
    "",
    " ",
    "NaT",
    "nan",
    "1650-01-01",
    "2300-01-01",
    "\u0662\u0660\u0662\u0664-\u0660\u0661-\u0660\u0661",  # non-ASCII digits
]


@pytest.mark.parametrize("bad", BAD_ANCHORS)
def test_bad_anchor_raises_on_core_and_legacy(bad: str) -> None:
    cfg = _cfg(6, _anchor_values(["2020-01-01", bad]), _win())
    # Force the bad value into the output: a 2-category column over 6 rows is not guaranteed to
    # contain it, so pin the anchor with a one-category column instead.
    cfg = _cfg(6, _anchor_values([bad]), _win())
    prepared = prepared_config(cfg)
    assert classify_plan(prepared).eligible
    with pytest.raises(WindowedDateError):
        run_resident(cfg)
    with pytest.raises(WindowedDateError):
        _generate_tables_from_config(prepared, None, None)


def test_error_message_does_not_echo_the_value() -> None:
    secret = "not-a-date-SECRET-9931"
    with pytest.raises(WindowedDateError) as exc:
        apply_windowed_date(
            WindowedDateConfig.from_dict({"anchor": "a", "max_days": 3}),
            pd.DataFrame({"a": [secret]}),
            b"\x00" * 8,
            "windowed_date/x",
        )
    assert secret not in str(exc.value)
    assert "row 0" in str(exc.value)


@pytest.mark.parametrize(
    ("anchor", "min_days", "max_days"),
    [
        ("2200-12-30", 5, 5),  # lands past SUPPORTED_MAX
        ("1700-01-02", -5, -5),  # lands before SUPPORTED_MIN
        ("2000-01-01", 10**12, 10**12),  # Timedelta overflow
        ("2000-01-01", -(10**12), -(10**12)),
    ],
)
def test_window_overflow_and_out_of_range_result_raise(
    anchor: str, min_days: int, max_days: int
) -> None:
    cfg = _cfg(4, _anchor_values([anchor]), _win(min_days=min_days, max_days=max_days))
    with pytest.raises(WindowedDateError):
        run_resident(cfg)
    with pytest.raises(WindowedDateError):
        _generate_tables_from_config(prepared_config(cfg), None, None)


def test_supported_range_boundaries_are_inclusive() -> None:
    cfg = WindowedDateConfig.from_dict({"anchor": "a", "min_days": 0, "max_days": 0})
    df = pd.DataFrame({"a": ["1700-01-01", "2200-12-31"]})
    assert apply_windowed_date(cfg, df, b"\x00" * 8, "n") == ["1700-01-01", "2200-12-31"]
    assert SUPPORTED_MIN.year == 1700 and SUPPORTED_MAX.year == 2200


# ---- typed anchors and null cells in the shared helper ----------------------------------------


@pytest.mark.parametrize(
    "value",
    [pd.Timestamp("2024-03-05"), dt.datetime(2024, 3, 5), dt.date(2024, 3, 5), "2024-03-05"],
)
def test_typed_and_string_anchors_give_the_same_date(value: Any) -> None:
    cfg = WindowedDateConfig.from_dict({"anchor": "a", "min_days": 2, "max_days": 2})
    out = apply_windowed_date(
        cfg, pd.DataFrame({"a": pd.Series([value], dtype=object)}), b"\x01" * 8, "n"
    )
    assert out == ["2024-03-07"]


@pytest.mark.parametrize("null", [None, float("nan"), pd.NaT, pd.NA])
def test_every_null_cell_form_propagates(null: Any) -> None:
    cfg = WindowedDateConfig.from_dict({"anchor": "a", "max_days": 9})
    out = apply_windowed_date(
        cfg, pd.DataFrame({"a": pd.Series(["2024-01-01", null], dtype=object)}), b"\x01" * 8, "n"
    )
    assert out[0] is not None and out[1] is None


@pytest.mark.parametrize(
    "bad",
    [pd.Timestamp("2024-01-01", tz="UTC"), 20240101, 1.5, b"2024-01-01", True],
)
def test_tz_aware_and_non_date_types_raise(bad: Any) -> None:
    cfg = WindowedDateConfig.from_dict({"anchor": "a", "max_days": 9})
    with pytest.raises(WindowedDateError):
        apply_windowed_date(
            cfg, pd.DataFrame({"a": pd.Series([bad], dtype=object)}), b"\x01" * 8, "n"
        )
