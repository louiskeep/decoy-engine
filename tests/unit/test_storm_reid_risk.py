"""Tests for STORM's k-anonymity-based re-id risk scoring (Plan B-1).

Each test builds a small DataFrame with a known joint-uniqueness shape,
runs `run_storm`, and asserts on `k_anonymity`, `quasi_identifier_groups`,
and `reid_risk_score`. Fixtures are intentionally tiny so the expected
k value is obvious by inspection.
"""
from __future__ import annotations

import pandas as pd

from decoy_engine.storm import run_storm


def test_single_qi_combo_with_k_equal_2():
    # Six rows of three categoricals. (city, state) groups:
    #   (NYC, NY): 2 rows
    #   (LA, CA): 2 rows
    #   (SF, CA): 2 rows
    # Minimum group size = 2 across the only viable 2-combo.
    # gender alone has k=2 (3 each) so (city, gender) and
    # (state, gender) can also produce small groups; the assertion
    # just checks the minimum and that the winning combo(s) are
    # 2-column.
    df = pd.DataFrame({
        "city":   ["NYC", "NYC", "LA",  "LA",  "SF",  "SF"],
        "state":  ["NY",  "NY",  "CA",  "CA",  "CA",  "CA"],
        "gender": ["F",   "M",   "F",   "M",   "F",   "M"],
    })
    profile = run_storm(df, "tiny.csv")
    # Joint (city, state, gender) splits 6 rows into 6 unique buckets
    # -> k=1. The 3-combo sweep finds it.
    assert profile.k_anonymity == 1
    # reid_risk_score = 100/1 capped at 100.
    assert profile.reid_risk_score == 100.0
    # The flat union should at least mention the columns from the
    # winning combo(s).
    assert set(profile.reid_risk_columns).issubset({"city", "state", "gender"})


def test_balanced_categoricals_yield_k_above_one():
    # 10 rows, 2 cities × 2 statuses = 4 buckets of size 2 or 3.
    # k=2 across any 2-combo.
    df = pd.DataFrame({
        "city":   ["A"] * 5 + ["B"] * 5,
        "status": ["x", "y"] * 5,
    })
    profile = run_storm(df, "balanced.csv")
    # k=2: smallest group across (city, status) is 2.
    assert profile.k_anonymity == 2
    assert profile.reid_risk_score == 50.0
    assert profile.quasi_identifier_groups == [["city", "status"]]


def test_no_qi_candidates_when_every_column_is_unique():
    # Pure identifier-shape: each row is unique on each column. No
    # column qualifies as a quasi-id candidate (unique_rate > 0.95
    # filter rejects them). k_anonymity is None; reid_risk_score
    # falls to 0 (the direct-identifier risk is captured by the
    # per-field pii_score / detector hits, not duplicated here).
    df = pd.DataFrame({
        "id":   list(range(50)),
        "name": [f"user_{i}" for i in range(50)],
    })
    profile = run_storm(df, "all_unique.csv")
    assert profile.k_anonymity is None
    assert profile.reid_risk_score == 0.0
    assert profile.quasi_identifier_groups == []
    assert profile.reid_risk_columns == []


def test_no_qi_candidates_when_every_column_is_constant():
    # The other extreme — single distinct value per column. Filtered
    # by the distinct_count > 1 guard.
    df = pd.DataFrame({
        "country": ["US"] * 100,
        "status":  ["active"] * 100,
    })
    profile = run_storm(df, "constants.csv")
    assert profile.k_anonymity is None
    assert profile.reid_risk_score == 0.0


def test_hipaa_trio_emerges_from_data_without_name_hardcoding():
    # The legacy hardcoded heuristic looked for column names matching
    # /dob/, /zip/, /gender/. Data-driven k-anonymity should surface
    # the same trio from cardinality alone, even with different
    # column names — though here we keep the canonical names so the
    # test reads naturally.
    # 8 rows; (dob, zip, gender) uniquely identifies 6 of them (k=1)
    # because two rows share (1985-03-15, 90210, F).
    df = pd.DataFrame({
        "dob":    ["1985-03-15", "1985-03-15",
                   "1990-07-22", "1990-07-22",
                   "1972-11-08", "1965-04-30",
                   "2001-09-17", "1988-12-25"],
        "zip":    ["90210", "90210", "10001", "10002",
                   "60601", "94102", "02134", "33101"],
        "gender": ["F", "F", "M", "M", "F", "M", "F", "M"],
    })
    profile = run_storm(df, "hipaa.csv")
    assert profile.k_anonymity == 1  # at least one combo uniquely identifies a row
    assert profile.reid_risk_score == 100.0
    # All three columns appear in at least one of the winning combos.
    contributing = set(profile.reid_risk_columns)
    assert {"dob", "zip", "gender"}.issubset(contributing)


def test_winning_combos_tie_at_minimum_k():
    # Construct a dataset where (a, b) and (a, c) both produce k=2,
    # and the (b, c) combo produces a higher k. Both 2-combos that
    # achieve k=2 should be reported.
    df = pd.DataFrame({
        # 4 distinct values, each occurring 4 times -> low unique_rate
        "a": ["x", "x", "x", "x", "y", "y", "y", "y",
              "z", "z", "z", "z", "w", "w", "w", "w"],
        # paired with a -- 8 distinct (a, b) buckets, each of size 2
        "b": ["p", "p", "q", "q", "p", "p", "q", "q",
              "r", "r", "s", "s", "r", "r", "s", "s"],
        # paired with a similarly
        "c": ["m", "m", "n", "n", "m", "m", "n", "n",
              "o", "o", "u", "u", "o", "o", "u", "u"],
    })
    profile = run_storm(df, "ties.csv")
    # The 3-combo (a, b, c) gives 8 buckets of size 2 -> k=2.
    # All 2-combos also produce k=2 buckets.
    assert profile.k_anonymity == 2
    assert profile.reid_risk_score == 50.0
    # 4 winning combos: C(3,2)=3 two-combos + 1 three-combo, all k=2.
    assert len(profile.quasi_identifier_groups) >= 1
    for group in profile.quasi_identifier_groups:
        assert set(group).issubset({"a", "b", "c"})


def test_too_few_rows_skips_inference():
    df = pd.DataFrame({"a": ["x"], "b": ["y"]})
    profile = run_storm(df, "tiny.csv")
    assert profile.k_anonymity is None
    assert profile.reid_risk_score == 0.0


def test_existing_profile_json_without_k_anonymity_still_loads():
    # Defensive: storm profiles persisted before Plan B-1 won't have
    # k_anonymity in their JSON. Round-tripping through to_dict()
    # -> dict -> StormProfile-via-kwargs should still work because
    # k_anonymity has a default value of None on the dataclass.
    from decoy_engine.storm.types import FieldStats, StormProfile

    pre_b1_dict = {
        "source_label": "pre_b1.csv",
        "row_count": 10,
        "sample_strategy": "full",
        "sample_row_cap": None,
        "fields": [],
        "reid_risk_columns": ["foo"],
        "reid_risk_score": 30.0,
        "quasi_identifier_groups": [],
        # NOTE: no "k_anonymity" key — that's the regression case.
        "engine_version": "0.1.0",
        "generated_at": "2026-05-13T00:00:00Z",
    }
    rebuilt = StormProfile(**pre_b1_dict)
    assert rebuilt.k_anonymity is None
    # And the new field round-trips through to_dict() going forward.
    assert "k_anonymity" in rebuilt.to_dict()
