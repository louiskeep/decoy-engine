"""Locks the Phase B corpus-scale invariants (ml-rebaseline-corpus-calibration).

build_extended_fixtures() was scaled from ~370 to ~2960 columns and
diversified (value formats + header styles); build_cryptic_fixtures() is a
new held-out cryptic-header benchmark slice (CH-3 ablation). This module
locks the invariants those changes must never violate:

  - total column count and per-label counts land near the target
    composition (module docstring of ``fixtures.py``, "Target composition")
  - the §A.3 leakage guard: every column of a given (non-cvv) label has a
    value set DISJOINT from every other column of that label, so
    ``assign_value_level_groups`` can never merge two same-label columns
    into one fold-collapsing group. ``cvv`` is the documented exemption.
  - build_extended_fixtures() is fully deterministic (same call twice ->
    byte-identical output)
  - build_cryptic_fixtures() returns a sane, broad, out-of-corpus slice

Gate reference: ml-benchmarking-and-privacy.md §A.3 / §B.2 / §B.3.
"""

from __future__ import annotations

import collections

import pytest

from decoy_engine.storm.eval.fixtures import (
    NO_DETECTOR,
    build_cryptic_fixtures,
    build_extended_fixtures,
)
from decoy_engine.storm.eval.split import assign_value_level_groups

pytestmark = pytest.mark.ml  # ml-gate membership (pytest -m ml)

# Target composition from fixtures.py's build_extended_fixtures docstring.
_TARGET_COUNTS: dict[str, int] = {
    "ssn": 260,
    "email": 260,
    "pan": 260,
    "iban": 260,
    "icd10": 240,
    "iso_date": 240,
    "npi": 240,
    "cvv": 120,
    "mrn": 300,
    "health_plan_id": 300,
    NO_DETECTOR: 480,
}
_TOTAL_MIN, _TOTAL_MAX = 2900, 3060
_PER_LABEL_TOLERANCE = 0.15  # ±15%


def _label_counts(fixtures: list) -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    for fx in fixtures:
        for label in fx.labels.values():
            counts[label] += 1
    return counts


def _label_column_value_sets(fixtures: list) -> dict[str, list[tuple[str, set[str]]]]:
    """label -> list of (fixture_name, {values in that column})."""
    out: dict[str, list[tuple[str, set[str]]]] = collections.defaultdict(list)
    for fx in fixtures:
        for col, label in fx.labels.items():
            values = {str(v) for v in fx.df[col].tolist()}
            out[label].append((fx.name, values))
    return out


class TestTotalAndPerLabelCounts:
    def test_total_column_count_in_range(self):
        fixtures = build_extended_fixtures()
        assert _TOTAL_MIN <= len(fixtures) <= _TOTAL_MAX, (
            f"expected {_TOTAL_MIN}-{_TOTAL_MAX} total columns, got {len(fixtures)}"
        )

    def test_per_label_counts_within_tolerance(self):
        fixtures = build_extended_fixtures()
        counts = _label_counts(fixtures)
        assert set(counts) == set(_TARGET_COUNTS), (
            f"label set mismatch: got {sorted(counts)}, expected {sorted(_TARGET_COUNTS)}"
        )
        for label, target in _TARGET_COUNTS.items():
            actual = counts[label]
            lo, hi = target * (1 - _PER_LABEL_TOLERANCE), target * (1 + _PER_LABEL_TOLERANCE)
            assert lo <= actual <= hi, f"{label}: expected within ±15% of {target}, got {actual}"


class TestLeakageGuard:
    """§A.3: no value string may appear in two columns of the same label
    (except cvv, whose small value space is a documented exemption)."""

    def test_no_shared_values_within_label_except_cvv(self):
        fixtures = build_extended_fixtures()
        by_label = _label_column_value_sets(fixtures)
        for label, cols in by_label.items():
            if label == "cvv":
                continue
            owner: dict[str, str] = {}
            offenders: list[tuple[str, str, str]] = []
            for fixture_name, values in cols:
                for v in values:
                    prior = owner.get(v)
                    if prior is not None and prior != fixture_name:
                        offenders.append((label, prior, fixture_name))
                    else:
                        owner[v] = fixture_name
            assert not offenders, (
                f"{label}: value-level leakage between columns (sample: {offenders[:5]})"
            )

    def test_assign_value_level_groups_yields_one_group_per_column_high_card(self):
        """Equivalent framing via the real split-time union-find: for every
        high-cardinality label, each column lands in its OWN group (no
        merges)."""
        fixtures = build_extended_fixtures()
        by_label = _label_column_value_sets(fixtures)
        for label, cols in by_label.items():
            if label == "cvv":
                continue
            value_sets = [values for _, values in cols]
            groups = assign_value_level_groups(value_sets)
            assert len(set(groups)) == len(cols), (
                f"{label}: {len(cols)} columns collapsed into {len(set(groups))} groups"
            )

    def test_cvv_leakage_is_bounded_not_catastrophic(self):
        """cvv is exempt from strict disjointness, but shouldn't collapse
        wholesale -- most of its ~120 columns should still land in their
        own union-find group given the realistic 3-4 digit value space."""
        fixtures = build_extended_fixtures()
        by_label = _label_column_value_sets(fixtures)
        cvv_cols = by_label["cvv"]
        value_sets = [values for _, values in cvv_cols]
        groups = assign_value_level_groups(value_sets)
        distinct = len(set(groups))
        assert distinct >= len(cvv_cols) * 0.5, (
            f"cvv: only {distinct}/{len(cvv_cols)} columns kept distinct groups"
        )


class TestDeterminism:
    def test_build_extended_fixtures_is_deterministic(self):
        fx1 = build_extended_fixtures()
        fx2 = build_extended_fixtures()
        assert len(fx1) == len(fx2)
        for a, b in zip(fx1, fx2, strict=True):
            assert a.name == b.name
            assert a.labels == b.labels
            assert a.df.equals(b.df)

    def test_build_cryptic_fixtures_is_deterministic(self):
        fx1 = build_cryptic_fixtures()
        fx2 = build_cryptic_fixtures()
        assert len(fx1) == len(fx2)
        for a, b in zip(fx1, fx2, strict=True):
            assert a.name == b.name
            assert a.labels == b.labels
            assert a.df.equals(b.df)


class TestCrypticFixtures:
    def test_column_count_in_expected_range(self):
        fixtures = build_cryptic_fixtures()
        assert 200 <= len(fixtures) <= 240, f"expected 200-240 columns, got {len(fixtures)}"

    def test_all_columns_have_non_empty_values(self):
        fixtures = build_cryptic_fixtures()
        for fx in fixtures:
            for col in fx.labels:
                series = fx.df[col]
                assert len(series) > 0
                assert series.notna().all()
                assert all(str(v).strip() != "" for v in series.tolist())

    def test_covers_at_least_seven_distinct_labels(self):
        fixtures = build_cryptic_fixtures()
        labels = {label for fx in fixtures for label in fx.labels.values()}
        assert len(labels) >= 7, f"expected >=7 distinct labels, got {labels}"

    def test_headers_are_cryptic_not_canonical(self):
        # Sanity check: none of the canonical clear-header strings should
        # leak into the cryptic benchmark slice.
        canonical = {
            "ssn",
            "email",
            "pan",
            "iban",
            "icd10",
            "npi",
            "mrn",
            "health_plan_id",
            "cvv",
            "service_date",
        }
        fixtures = build_cryptic_fixtures()
        headers = {col for fx in fixtures for col in fx.labels}
        assert not (headers & canonical), (
            f"canonical headers leaked into cryptic slice: {headers & canonical}"
        )

    def test_values_do_not_overlap_training_corpus(self):
        train = build_extended_fixtures()
        cryptic = build_cryptic_fixtures()
        train_by_label = _label_column_value_sets(train)
        cryptic_by_label = _label_column_value_sets(cryptic)

        train_value_union: dict[str, set[str]] = {
            label: set().union(*(vals for _, vals in cols)) if cols else set()
            for label, cols in train_by_label.items()
        }

        for label, cols in cryptic_by_label.items():
            if label == "cvv":
                continue  # documented exemption: cvv value space is shared
            cryptic_union: set[str] = set().union(*(vals for _, vals in cols))
            overlap = cryptic_union & train_value_union.get(label, set())
            assert not overlap, (
                f"{label}: cryptic slice overlaps training corpus ({list(overlap)[:5]})"
            )
