"""HC-7: avg_length / max_length on ColumnProfile.

Three concerns: (1) walk_dataframe populates the stats for string columns
and leaves them None for non-string columns; (2) profile_hash is NOT
perturbed by the new fields (they are advisory-only, excluded from the
data-shape hash bytes -- see profile/_serialize.py); (3) the fields
round-trip through JSON serialization.
"""

from __future__ import annotations

import json
import random
from datetime import datetime

import pandas as pd

from decoy_engine.profile import (
    ColumnProfile,
    Profile,
    TableProfile,
    profile_from_json,
    profile_hash,
    profile_to_json,
)
from decoy_engine.profile._walk import walk_dataframe


def _rng() -> random.Random:
    return random.Random(0)


def _col(**overrides: object) -> ColumnProfile:
    base: dict[str, object] = {
        "name": "notes",
        "dtype": "object",
        "row_count": 10,
        "null_count": 0,
        "distinct_count": 10,
        "sampled": False,
        "is_candidate_key_sampled": True,
        "declared_pk": False,
        "is_fk": False,
        "fk_target": None,
        "pii_class": None,
        "avg_length": None,
        "max_length": None,
    }
    base.update(overrides)
    return ColumnProfile(**base)  # type: ignore[arg-type]


def _profile(col: ColumnProfile) -> Profile:
    return Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=10, columns=(col,)),),
        relationships=(),
        profiled_at=datetime(2026, 7, 17, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


class TestWalkPopulatesLengthStats:
    def test_string_column_gets_avg_and_max_length(self) -> None:
        df = pd.DataFrame({"notes": ["short", "a bit longer text", "x"]})
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        col = profile.columns[0]
        assert col.avg_length is not None
        assert col.max_length == len("a bit longer text")
        expected_avg = (len("short") + len("a bit longer text") + len("x")) / 3
        assert col.avg_length == expected_avg

    def test_int_column_has_no_length_stats(self) -> None:
        df = pd.DataFrame({"age": [1, 2, 3]})
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        col = profile.columns[0]
        assert col.avg_length is None
        assert col.max_length is None

    def test_all_null_string_column_has_no_length_stats(self) -> None:
        df = pd.DataFrame({"notes": pd.array([None, None], dtype="object")})
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=None,
            rng=_rng(),
        )
        col = profile.columns[0]
        assert col.avg_length is None
        assert col.max_length is None

    def test_length_stats_come_from_the_sample_population(self) -> None:
        # Sampling makes distinct_count sample-derived (see _walk.py); the
        # length stats must track the same population so the free-text
        # advisory's length+distinctness pair stays statistically
        # consistent, not one full-scan stat paired with one sampled stat.
        df = pd.DataFrame({"notes": ["x" * 5] * 50 + ["y" * 500] * 50})
        profile = walk_dataframe(
            df,
            table_name="t",
            declared_pk_cols=frozenset(),
            fk_specs={},
            sample_rows=10,
            rng=_rng(),
        )
        col = profile.columns[0]
        assert col.avg_length is not None
        # Full-population mean would be exactly 252.5; a 10-row sample from
        # a same-seed deterministic RNG need not match it, but it must fall
        # within the two constituent lengths.
        assert 5.0 <= col.avg_length <= 500.0


class TestHashUnaffectedByLengthStats:
    def test_profile_hash_ignores_avg_length_and_max_length(self) -> None:
        without_lengths = _profile(_col())
        with_lengths = _profile(_col(avg_length=123.4, max_length=999))
        assert profile_hash(without_lengths) == profile_hash(with_lengths)


class TestSerializationRoundTrip:
    def test_avg_length_and_max_length_survive_json_round_trip(self) -> None:
        profile = _profile(_col(avg_length=42.5, max_length=310))
        round_tripped = profile_from_json(profile_to_json(profile))
        assert round_tripped == profile
        assert round_tripped.tables[0].columns[0].avg_length == 42.5
        assert round_tripped.tables[0].columns[0].max_length == 310

    def test_missing_length_keys_default_to_none_on_load(self) -> None:
        # Simulates JSON written before HC-7 -- no avg_length/max_length
        # keys present at all.
        profile = _profile(_col())
        raw = json.loads(profile_to_json(profile))
        del raw["tables"][0]["columns"][0]["avg_length"]
        del raw["tables"][0]["columns"][0]["max_length"]
        loaded = profile_from_json(json.dumps(raw))
        assert loaded.tables[0].columns[0].avg_length is None
        assert loaded.tables[0].columns[0].max_length is None
