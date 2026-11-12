"""`distribution_snapshot_document` withholds value-sensitive columns' cells from a fit snapshot.

Most cells force every column sensitive so the numeric and datetime rules are exercised on
exact values the profiler's predicate would not necessarily flag.
"""

from __future__ import annotations

import copy
import io
import json
import signal
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.generation.statistical import load_spec, sample_column
from decoy_engine.profiling import _snapshot_values as sv
from decoy_engine.profiling import (
    distribution_snapshot_document,
    profile_columns,
    profile_document,
)
from decoy_engine.profiling._value_redaction import POLICY_RAW, POLICY_REDACTED, REDACTED
from decoy_engine.quality.snapshot import compute_distribution_snapshot, numeric_stats

_FIRST = [
    "Abel", "Brina", "Cosmo", "Delia", "Ewan", "Fiora", "Gideon", "Hester", "Ivo", "Juno",
    "Kester", "Lorna", "Milo", "Nerys", "Otto", "Perpetua", "Quincy", "Rhoda", "Silas", "Tamsin",
]  # fmt: skip
_N = 20


@pytest.fixture
def force_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sv, "value_sensitive_names", lambda fields: [f["name"] for f in fields])


def _balance_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {"balance": [31337.25, *[40000.5 + 1000 * i for i in range(_N - 2)], 98765.5]}
    )


def _qa_frame() -> pd.DataFrame:
    names = [
        "Marguerite Okonkwo", "Dmitri Vasquez", "Ingrid Halvorsen", "Tobias Nakamura",
        "Priya Castellanos", "Leopold Abernathy", "Sunniva Kowalczyk", "Emeka Lindqvist",
        "Rosalind Fairweather", "Anselm Dragomir",
    ]  # fmt: skip
    return pd.DataFrame(
        {
            "patient_id": [f"PT-{80210 + 37 * i}" for i in range(10)],
            "full_name": names,
            "ssn": [f"{412 + 7 * i:03d}-{18 + i:02d}-{6100 + 13 * i:04d}" for i in range(10)],
        }
    )


def _leaks(doc: object, canaries: list[str]) -> list[str]:
    text = json.dumps(doc)
    return [c for c in canaries if c in text]


def _col(doc: dict[str, Any], name: str) -> dict[str, Any]:
    out: dict[str, Any] = doc["columns"][name]
    return out


def _numeric(values: list[float], dtype: str = "float64") -> dict[str, Any]:
    df = pd.DataFrame({"v": pd.Series(values, dtype=dtype)})
    return _col(distribution_snapshot_document(df), "v")["stats"]


def _finite(values: list[float]) -> list[float]:
    return [v for v in values if np.isfinite(v)]


def _one_bin(stats: dict[str, Any], values: list[float]) -> None:
    cells = _finite(values)
    assert len(stats["bin_edges"]) == 2
    assert stats["bin_counts"] == [len(cells)]
    assert all(np.isfinite(e) for e in stats["bin_edges"])
    assert not set(stats["bin_edges"]) & set(cells)
    assert stats["mean"] is None
    assert stats["std"] is None
    assert stats["quantiles"] == {}


# AT-1


def test_the_qa_patients_frame_leaks_no_name_ssn_or_id() -> None:
    df = _qa_frame()
    doc = distribution_snapshot_document(df, source_label="patients.csv")
    canaries = [str(v) for col in df for v in df[col]]
    assert _leaks(doc, canaries) == []
    assert doc["value_policy"] == POLICY_REDACTED
    assert {"full_name", "ssn"} <= set(doc["redacted_columns"])
    for name in doc["redacted_columns"]:
        stats = _col(doc, name)["stats"]
        assert stats["top_values"] == []
        assert stats["other_count"] == 10


# AT-2


def test_a_numeric_canary_column_keeps_a_coarse_unit_range_only() -> None:
    df = _balance_frame()
    doc = distribution_snapshot_document(df)
    assert doc["redacted_columns"] == ["balance"]
    stats = _col(doc, "balance")["stats"]
    assert _leaks(doc, ["31337", "98765", "50155.5875"]) == []
    unit = Decimal(10) ** 5  # range 67428.25 is five digits, so the grid step is 1e5
    assert Decimal(repr(stats["min"])) % unit == 0
    assert Decimal(repr(stats["max"])) % unit == 0
    assert stats["min"] < 31337.25 < 98765.5 < stats["max"]
    assert len(stats["bin_edges"]) == len(stats["bin_counts"]) + 1
    assert sum(stats["bin_counts"]) == _N
    assert stats["mean"] is None
    assert stats["std"] is None
    assert stats["quantiles"] == {}
    assert (stats["min"], stats["max"]) == (-100000, 200000)


# AT-3


def _spec_for(doc: dict[str, Any], name: str) -> Any:
    return load_spec({"name": name, "type": "statistical"}, snapshot=doc)


def test_a_redacted_numeric_column_still_samples_inside_its_bounds() -> None:
    doc = distribution_snapshot_document(_balance_frame())
    stats = _col(doc, "balance")["stats"]
    drawn = sample_column(_spec_for(doc, "balance"), 1000, col_seed=11)
    assert len(drawn) == 1000
    assert all(stats["min"] <= v <= stats["max"] for v in drawn)


def test_a_redacted_integer_column_samples_ints(force_sensitive: None) -> None:
    df = pd.DataFrame({"id": np.arange(1001, 1011, dtype="int64")})
    doc = distribution_snapshot_document(df)
    drawn = sample_column(_spec_for(doc, "id"), 200, col_seed=3)
    assert all(type(v) is int for v in drawn)


@pytest.mark.parametrize(
    "values",
    [
        [1.79e308] * _N,
        [1.70e308 + i * 5e305 for i in range(_N)],
    ],
    ids=["constant-near-max", "close-range-near-max"],
)
def test_a_numeric_column_near_float_max_still_samples(
    force_sensitive: None, values: list[float]
) -> None:
    doc = distribution_snapshot_document(pd.DataFrame({"v": values}))
    stats = _col(doc, "v")["stats"]
    _one_bin(stats, values)
    drawn = sample_column(_spec_for(doc, "v"), 100, col_seed=5)
    assert all(stats["bin_edges"][0] <= v <= stats["bin_edges"][1] for v in drawn)


# AT-4 and AT-4b

_GRID_ROWS = [
    ((1001.0, 1010.0), (990, 1030)),
    ((7.0, 7.0), (-10, 20)),
    ((-7.0, -7.0), (-20, 10)),
    ((0.0, 0.0), (-10, 20)),
    ((18.0, 90.0), (-100, 200)),
    ((31337.25, 98765.5), (-100000, 200000)),
    ((4.23, 6.56), (-10, 20)),
    ((94.0, 99.8), (80, 110)),
    ((-50.0, -10.0), (-200, 100)),
    ((-5.0, 5.0), (-200, 200)),
    ((0.0, 9.0), (-10, 20)),
    ((3.0, 4.0), (-10, 20)),
    ((5.0, 15.0), (-100, 200)),
    ((1e-9, 5e-9), (Decimal("-1E-8"), Decimal("2E-8"))),
    ((1e15, 2e15), (Decimal("-1E+16"), Decimal("2E+16"))),
]


@pytest.mark.parametrize(("lohi", "expected"), _GRID_ROWS, ids=[str(r[0]) for r in _GRID_ROWS])
def test_the_grid_edges_table(lohi: tuple[float, float], expected: tuple[Any, Any]) -> None:
    lo, hi = lohi
    lower, upper = sv._grid_edges(lo, hi)
    assert (lower, upper) == (Decimal(expected[0]), Decimal(expected[1]))
    assert lower < Decimal(repr(lo))
    assert upper > Decimal(repr(hi))


def test_a_range_tiny_against_its_magnitude_stays_outside() -> None:
    lo, hi = 1e6 + 0.1, 1e6 + 0.1 + 1e-9
    lower, upper = sv._grid_edges(lo, hi)
    assert lower < Decimal(repr(lo))
    assert upper > Decimal(repr(hi))


@pytest.mark.parametrize(
    ("values", "edges"),
    [
        (list(np.linspace(4.23, 6.56, 42).round(2)), [-10, 20]),
        (list(np.linspace(94.0, 99.8, 39).round(1)), [80, 110]),
    ],
)
def test_a_float_rule_would_have_written_the_real_extremes(
    force_sensitive: None, values: list[float], edges: list[float]
) -> None:
    stats = _numeric(values)
    _one_bin(stats, values)
    assert stats["bin_edges"] == edges


def test_full_span_floats_use_the_finite_fallback_and_terminate() -> None:
    def hung(*_: object) -> None:
        raise TimeoutError("no domain within five seconds")

    cells = np.array([-1e308, 1e308])
    previous = signal.signal(signal.SIGALRM, hung)
    signal.alarm(5)
    try:
        domain = sv._coarse_domain(-1e308, 1e308, cells)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert domain == (-1.5, 1.5)
    assert sv._coarse_domain(-1e308, 1e308, cells) == domain


@pytest.mark.parametrize(
    "cells",
    [
        [1.79e308, -1.79e308],
        [1.5, 2.5, 1.79e308, -1.79e308],
        [-1.5, 1.5, -2.5, 2.5, 3.5, 1e308, -1e308],
        [1e308, -1e308, 0.5],
    ],
)
def test_the_fallback_domain_is_fixed_whatever_the_cells(cells: list[float]) -> None:
    arr = np.array(cells)
    assert sv._coarse_domain(float(arr.min()), float(arr.max()), arr) == (-1.5, 1.5)


@settings(max_examples=150, deadline=None)
@given(cells=st.lists(st.floats(allow_nan=False, allow_infinity=False), min_size=1, max_size=25))
def test_the_domain_is_finite_and_outside_the_cells_unless_it_is_the_fixed_fallback(
    cells: list[float],
) -> None:
    arr = np.array(cells)
    lo, hi = float(arr.min()), float(arr.max())
    lower, upper = sv._coarse_domain(lo, hi, arr)
    assert np.isfinite(lower) and np.isfinite(upper)
    assert lower < upper
    if (lower, upper) != (-1.5, 1.5):  # the ordinary path encloses the data
        assert lower < lo
        assert upper > hi
        assert lower not in cells
        assert upper not in cells


_DECIMALS = st.integers(0, 4).flatmap(
    lambda p: st.decimals(min_value=-(10**6), max_value=10**6, places=p, allow_nan=False)
)


@settings(max_examples=60, deadline=None)
@given(cells=st.lists(_DECIMALS.map(lambda d: float(str(d))), min_size=2, max_size=30))
def test_decimal_grid_data_never_meets_an_edge(cells: list[float]) -> None:
    df = pd.DataFrame({"v": cells})
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sv, "value_sensitive_names", lambda fields: [f["name"] for f in fields])
        stats = _col(distribution_snapshot_document(df), "v")["stats"]
    assert len(stats["bin_edges"]) == 2
    assert stats["bin_counts"] == [len(cells)]
    assert not set(stats["bin_edges"]) & set(cells)
    assert stats["bin_edges"][0] < min(cells)
    assert stats["bin_edges"][1] > max(cells)


def test_one_bin_for_ids_ages_and_constants(force_sensitive: None) -> None:
    ids = [float(i) for i in range(1001, 1011)]
    for df in (
        pd.DataFrame({"v": np.arange(1001, 1011, dtype="int64")}),
        pd.DataFrame({"v": [*ids, np.nan]}),
    ):
        stats = _col(distribution_snapshot_document(df), "v")["stats"]
        assert stats["bin_edges"] == [990, 1030]
        assert stats["bin_counts"] == [10]
    const = _numeric([7] * 20, dtype="int64")
    assert (const["bin_edges"], const["bin_counts"]) == ([-10, 20], [20])
    ages = [18 + (i * 7) % 73 for i in range(5000)]
    stats = _numeric(ages, dtype="int64")
    assert (stats["bin_edges"], stats["bin_counts"]) == ([-100, 200], [5000])


def test_a_column_with_no_finite_value_is_left_alone() -> None:
    series = pd.Series([float("inf"), float("-inf")])
    stats = numeric_stats(series, bins=10)
    before = copy.deepcopy(stats)
    sv._coarse_range(series, stats, ["min", "max", "bin_edges", "bin_counts"])
    assert stats == before
    assert stats["min"] is None


# AT-5


def test_datetime_bounds_are_the_years_around_the_data(force_sensitive: None) -> None:
    exact = [
        "1991-01-01T00:00:00",
        "1991-12-31T23:59:59",
        *[f"1991-0{m}-15T08:30:00" for m in range(2, 9)],
    ]
    df = pd.DataFrame({"admitted": pd.to_datetime(exact)})
    raw = compute_distribution_snapshot(df)
    doc = distribution_snapshot_document(df)
    stats = _col(doc, "admitted")["stats"]
    assert stats["min"] == "1990-01-01T00:00:00"
    assert stats["max"] == "1992-12-31T23:59:59"
    assert stats["year_bins"] == _col(raw, "admitted")["stats"]["year_bins"]
    assert _leaks(doc, ["1991-01-01T00", "1991-12-31T23", "08:30:00"]) == []
    drawn = sample_column(_spec_for(doc, "admitted"), 200, col_seed=9)
    assert {d.year for d in drawn} == {1991}


def _calendar_edge_frame(stamps: list[str]) -> pd.DataFrame:
    return pd.DataFrame({"admitted": pd.Series(np.array(stamps, dtype="datetime64[s]"))})


def test_datetime_bounds_at_the_first_calendar_year_stay_parseable(force_sensitive: None) -> None:
    df = _calendar_edge_frame(["0001-03-04T05:06:07", "0001-09-10T11:12:13", "0002-06-01T00:00:00"])
    doc = distribution_snapshot_document(df)
    stats = _col(doc, "admitted")["stats"]
    assert stats["min"] == "0001-01-01T00:00:00"
    assert stats["max"] == "0003-12-31T23:59:59"
    drawn = sample_column(_spec_for(doc, "admitted"), 100, col_seed=5)
    assert {d.year for d in drawn} <= {1, 2}


def test_datetime_bounds_at_the_last_calendar_year_stay_parseable(force_sensitive: None) -> None:
    df = _calendar_edge_frame(["9998-03-04T05:06:07", "9999-09-10T11:12:13"])
    doc = distribution_snapshot_document(df)
    stats = _col(doc, "admitted")["stats"]
    assert stats["min"] == "9997-01-01T00:00:00"
    assert stats["max"] == "9999-12-31T23:59:59"
    spec = _spec_for(doc, "admitted")
    assert datetime.fromisoformat(str(spec.stats["max"])).year == 9999


# AT-6


def test_categorical_values_become_aggregate_mass(force_sensitive: None) -> None:
    labels = ["[redacted]", "[redacted:1]", *[f"label-{i}" for i in range(38)]]
    df = pd.DataFrame({"code": labels, "ssn": [labels[i % 25] for i in range(40)]})
    raw = compute_distribution_snapshot(df, high_cardinality_columns=["code"])
    doc = distribution_snapshot_document(df, high_cardinality_columns=["code"])
    assert "[redacted" not in json.dumps(doc)
    for name in ("code", "ssn"):
        stats = _col(doc, name)["stats"]
        before = _col(raw, name)["stats"]
        assert stats["top_values"] == []
        assert stats["other_count"] == _col(doc, name)["non_null_count"] == 40
        assert stats["other_count"] == before["other_count"] + sum(
            t["count"] for t in before["top_values"]
        )
    assert _col(doc, "code")["stats"]["high_cardinality"] is True
    assert "high_cardinality" not in _col(doc, "ssn")["stats"]


# AT-7 and AT-7b


def _joint_frame(emails: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "email": emails,
            "status": [("open", "closed", "held")[i % 3] for i in range(40)],
            "tier": [("gold", "basic")[i % 2] for i in range(40)],
            "zemail": emails,
        }
    )


_EMAILS = [f"user{i}@example.com" for i in range(40)]
# Same length profile as _EMAILS: lengths are kept, so only the labels may differ.
_OTHER_EMAILS = [f"mail{(i * 7) % 40}@example.com" for i in range(40)]


def test_a_joint_with_a_sensitive_side_has_no_cells() -> None:
    df = _joint_frame(_EMAILS)
    raw = compute_distribution_snapshot(df, joint_columns=[("email", "status")])
    doc = distribution_snapshot_document(df, joint_columns=[("email", "status")])
    (joint,) = doc["joints"]
    (rawjoint,) = raw["joints"]
    assert joint["cells"] == []
    assert joint["other_count"] == rawjoint["other_count"] + sum(
        c["count"] for c in rawjoint["cells"]
    )
    assert joint["columns"] == rawjoint["columns"]
    assert joint["cell_count"] == rawjoint["cell_count"]


def test_a_real_redacted_cell_is_not_a_sentinel(force_sensitive: None) -> None:
    df = pd.DataFrame({"a": ["[redacted]", "x", "y"] * 4, "b": ["p", "q", "r"] * 4})
    (joint,) = distribution_snapshot_document(df, joint_columns=[("a", "b")])["joints"]
    assert joint["cells"] == []
    assert joint["other_count"] == 12


def test_a_joint_of_two_plain_columns_is_unchanged() -> None:
    df = _joint_frame(_EMAILS)
    pair = [("status", "tier")]
    raw = compute_distribution_snapshot(df, joint_columns=pair)
    doc = distribution_snapshot_document(df, joint_columns=pair)
    assert doc["joints"] == raw["joints"]
    assert doc["joints"][0]["cells"]


@pytest.mark.parametrize("pair", [("email", "status"), ("status", "zemail")])
def test_joint_output_does_not_follow_the_real_sensitive_labels(pair: tuple[str, str]) -> None:
    left, right = _joint_frame(_EMAILS), _joint_frame(_OTHER_EMAILS)
    raw = compute_distribution_snapshot(left, joint_columns=[pair])
    assert raw["joints"][0]["other_count"] == 15  # 40 tied count-1 cells, top-K cut at 25
    first = distribution_snapshot_document(left, joint_columns=[pair])
    second = distribution_snapshot_document(right, joint_columns=[pair])
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["joints"][0]["cells"] == []
    assert first["joints"][0]["other_count"] == 40


# AT-8 and AT-9


def test_plain_columns_equal_the_raw_snapshot() -> None:
    df = _joint_frame(_EMAILS)
    df["amount"] = [1, 2, 3, 4, 5] * 8
    raw = compute_distribution_snapshot(df, cell_format=True)
    doc = distribution_snapshot_document(df)
    for name in ("status", "tier", "amount"):
        assert name not in doc["redacted_columns"]
        assert doc["columns"][name] == raw["columns"][name]
    assert {"email", "zemail"} <= set(doc["redacted_columns"])


def test_include_values_is_the_raw_snapshot_plus_the_two_keys() -> None:
    df = _joint_frame(_EMAILS)
    pairs = [("email", "status")]
    raw = compute_distribution_snapshot(df, joint_columns=pairs, cell_format=True)
    before = copy.deepcopy(raw)
    frame_before = df.copy()
    doc = distribution_snapshot_document(df, joint_columns=pairs, include_values=True)
    names = distribution_snapshot_document(df, joint_columns=pairs)["redacted_columns"]
    assert doc == {**raw, "value_policy": POLICY_RAW, "redacted_columns": names}
    assert raw == before
    pd.testing.assert_frame_equal(df, frame_before)


_NON_FINITE = [
    ("mixed", [1.5, np.inf, -np.inf, np.nan, 2.5, 7.25, 3.0, 9.5]),
    ("all-inf", [np.inf, -np.inf, np.inf, np.inf]),
]


@pytest.mark.parametrize("include_values", [False, True])
@pytest.mark.parametrize(("label", "values"), _NON_FINITE, ids=[n for n, _ in _NON_FINITE])
def test_inf_cells_do_not_break_the_document(
    force_sensitive: None, label: str, values: list[float], include_values: bool
) -> None:
    doc = distribution_snapshot_document(
        pd.DataFrame({"v": values, "w": range(len(values))}), include_values=include_values
    )
    assert "v" in doc["columns"]
    if not include_values and label == "mixed":
        stats = _col(doc, "v")["stats"]
        assert all(np.isfinite(e) for e in stats["bin_edges"])
        assert stats["bin_counts"] == [sum(np.isfinite(values))]


def test_the_csv_repro_with_inf_produces_a_document() -> None:
    df = pd.read_csv(io.StringIO("x,y\n1.5,a\ninf,b\n2.5,c\n"))
    assert "x" in distribution_snapshot_document(df)["columns"]


@pytest.mark.parametrize("include_values", [False, True])
@pytest.mark.parametrize("values", [[1e308, 1.0, 2.0], [1.79e308, 1.7e308, 1.5]])
def test_a_range_wider_than_the_profile_chart_can_bin_produces_a_document(
    values: list[float], include_values: bool
) -> None:
    doc = distribution_snapshot_document(
        pd.DataFrame({"v": values, "w": range(len(values))}), include_values=include_values
    )
    assert "v" in doc["columns"]


def test_include_values_must_be_a_bool() -> None:
    with pytest.raises(TypeError, match="include_values"):
        distribution_snapshot_document(_qa_frame(), include_values=1)  # type: ignore[arg-type]


# AT-11b


def test_an_unknown_key_in_a_sensitive_entry_becomes_null(monkeypatch: pytest.MonkeyPatch) -> None:
    real = sv.compute_distribution_snapshot

    def spiked(df: pd.DataFrame, **kw: Any) -> dict[str, Any]:
        snap = real(df, **kw)
        for col in snap["columns"].values():
            col["extra"] = REDACTED
            col["stats"]["surprise"] = "Canary-Cell"
        return snap

    monkeypatch.setattr(sv, "compute_distribution_snapshot", spiked)
    df = _joint_frame(_EMAILS)
    doc = distribution_snapshot_document(df)
    sensitive = _col(doc, "email")
    assert sensitive["extra"] is None
    assert sensitive["stats"]["surprise"] is None
    plain = _col(doc, "status")
    assert plain["extra"] == REDACTED
    assert plain["stats"]["surprise"] == "Canary-Cell"


# AT-13 and AT-14


def test_the_document_is_deterministic() -> None:
    df = _joint_frame(_EMAILS)
    pairs = [("email", "status")]
    a = distribution_snapshot_document(df, joint_columns=pairs)
    b = distribution_snapshot_document(df, joint_columns=pairs)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_the_marked_columns_equal_the_scan_documents() -> None:
    for df in (_qa_frame(), _joint_frame(_EMAILS), _balance_frame()):
        scan = profile_document(profile_columns(df, "x.csv"))
        assert distribution_snapshot_document(df)["redacted_columns"] == scan["redacted_columns"]


def test_the_profile_row_cap_matches_a_scan_of_the_head() -> None:
    df = pd.DataFrame(
        {
            "grp": ["x", "y", "z", *(["a", "b"] * 18), "a"],
            "full_name": [f"{f} Quixwort" for f in _FIRST] * 2,
            "n": range(40),
        }
    )
    head = profile_document(profile_columns(df.head(3), "x.csv"))["redacted_columns"]
    full = profile_document(profile_columns(df, "x.csv"))["redacted_columns"]
    assert head != full
    capped = distribution_snapshot_document(df, source_label="x.csv", profile_row_cap=3)
    assert capped["redacted_columns"] == head


@pytest.mark.parametrize(
    ("cap", "error"),
    [(-3, ValueError), (0, ValueError), (True, TypeError), (2.5, TypeError)],
    ids=["negative", "zero", "bool", "float"],
)
def test_the_profile_row_cap_must_be_a_positive_int(cap: Any, error: type[Exception]) -> None:
    with pytest.raises(error, match="profile_row_cap"):
        distribution_snapshot_document(_qa_frame(), profile_row_cap=cap)


# AT-9 (QA-FK-FANOUT)


def _fanout_frame() -> pd.DataFrame:
    """Ten patients with 1..4 child rows each, so the degree histogram has four entries."""
    degrees = [1, 1, 2, 2, 2, 3, 3, 4, 4, 4]
    return pd.DataFrame(
        {
            "patient_id": [f"PT-{80210 + 37 * i}" for i, d in enumerate(degrees) for _ in range(d)],
        }
    )


def test_the_fanout_block_survives_redaction_on_a_sensitive_column_as_counts_only() -> None:
    df = _fanout_frame()
    raw = compute_distribution_snapshot(df, fanout_columns=["patient_id"])
    doc = distribution_snapshot_document(df, fanout_columns=["patient_id"])
    assert "patient_id" in doc["redacted_columns"]
    block = _col(doc, "patient_id")["fanout"]
    assert block == _col(raw, "patient_id")["fanout"]
    assert block == {"rows": 26, "keys": 10, "degrees": [[1, 2], [2, 3], [3, 2], [4, 3]]}
    assert all(type(n) is int for n in (block["rows"], block["keys"]))
    assert all(type(n) is int for pair in block["degrees"] for n in pair)
    assert _leaks(doc, [str(v) for v in df["patient_id"]]) == []


def test_a_redacted_snapshot_with_fanout_still_decodes() -> None:
    from decoy_engine.generation.statistical import fanout_spec_from_snapshot

    doc = distribution_snapshot_document(_fanout_frame(), fanout_columns=["patient_id"])
    spec = fanout_spec_from_snapshot(doc, "patient_id", digest="0" * 64)
    assert (spec.rows, spec.keys) == (26, 10)
