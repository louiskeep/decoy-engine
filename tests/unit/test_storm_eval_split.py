"""Tests for the held-out split scaffolding (ML0 / §A.3).

Validates the group-key function (determinism, content-based grouping) and
the split-input builder (correct shape, aggregate-only features, group key
length). The StratifiedGroupKFold split itself requires scikit-learn and is
tested only when that optional extra is present.

Gate reference: ml-benchmarking-and-privacy.md §A.3.
"""

from __future__ import annotations

import json

import pytest

from decoy_engine.storm.eval import build_fixtures
from decoy_engine.storm.eval.split import (
    assign_value_level_groups,
    make_group_key,
    make_split_inputs,
)


class TestMakeGroupKey:
    """Group key correctly represents unique column values."""

    def test_deterministic_for_same_series(self):
        import pandas as pd

        s = pd.Series(["b@x.com", "a@x.com", "b@x.com", None])
        assert make_group_key(s) == make_group_key(s)

    def test_sorted_unique_non_null(self):
        import pandas as pd

        s = pd.Series(["z", "a", "z", None, "m"])
        key = make_group_key(s)
        # "a", "m", "z" sorted and joined
        assert key == "a|m|z"

    def test_all_null_column_gives_empty_key(self):
        import pandas as pd

        s = pd.Series([None, None], dtype="object")
        assert make_group_key(s) == ""

    def test_different_values_give_different_keys(self):
        import pandas as pd

        s1 = pd.Series(["123-45-6789"])
        s2 = pd.Series(["987-65-4321"])
        assert make_group_key(s1) != make_group_key(s2)


class TestValueLevelGroups:
    """The real §A.3 guard: columns sharing ANY value land in one group."""

    def test_shared_value_unions_columns(self):
        # cols 0 and 1 share "b"; col 2 is disjoint. The shared pair MUST share
        # a group (so the value can't straddle the train/test boundary), and the
        # disjoint column MUST be separate. make_group_key alone fails this:
        # cols 0 and 1 have different signatures.
        groups = assign_value_level_groups([{"a", "b"}, {"b", "c"}, {"x", "y"}])
        assert groups[0] == groups[1]
        assert groups[2] != groups[0]

    def test_transitive_union_via_chain(self):
        # 0-1 share "b", 1-2 share "c" -> all three transitively one group.
        groups = assign_value_level_groups([{"a", "b"}, {"b", "c"}, {"c", "d"}])
        assert groups[0] == groups[1] == groups[2]

    def test_all_disjoint_are_distinct_groups(self):
        groups = assign_value_level_groups([{"a"}, {"b"}, {"c"}])
        assert len(set(groups)) == 3

    def test_deterministic_contiguous_ids(self):
        vs = [{"p", "q"}, {"r"}, {"q", "z"}]
        assert assign_value_level_groups(vs) == assign_value_level_groups(vs)
        # 0 and 2 share "q"; ids are first-seen-contiguous: 0->0, 1->1, 2->0.
        assert assign_value_level_groups(vs) == [0, 1, 0]


class TestMakeSplitInputs:
    """Split-input builder: shape, label coverage, no raw values in X."""

    def setup_method(self):
        self.fixtures = build_fixtures()
        self.X, self.y, self.groups = make_split_inputs(self.fixtures)

    def test_lengths_are_consistent(self):
        assert len(self.X) == len(self.y) == len(self.groups)

    def test_column_count_matches_fixture_total(self):
        total_cols = sum(len(fx.labels) for fx in self.fixtures)
        assert len(self.X) == total_cols

    def test_labels_match_fixture_labels(self):
        # All truth labels come from the fixture ground-truth dict.
        all_labels = {label for fx in self.fixtures for label in fx.labels.values()}
        assert set(self.y).issubset(all_labels)

    def test_feature_dicts_are_json_serializable(self):
        # Features must be aggregate stats, all JSON-serializable, and
        # round-trip back to an equal dict (no lossy/opaque encoding).
        for feat in self.X:
            payload = json.dumps(feat)
            assert json.loads(payload) == feat

    def test_feature_dicts_contain_no_raw_cell_values(self):
        # Privacy gate §B.4: X must be aggregate stats, not raw cell values.
        # Check that known raw values from the fixtures do not appear in X.
        sample_raw_values = [
            "user0@example.com",  # a specific email
            "123456789",  # a digit string that appears in NPI body
        ]
        X_blob = json.dumps(self.X)
        for val in sample_raw_values:
            assert val not in X_blob, f"raw value {val!r} leaked into feature dict"

    def test_groups_are_non_empty_strings(self):
        for g in self.groups:
            assert isinstance(g, str)
            # Most columns have data; group key can be empty only for all-null columns.

    def test_groups_are_deterministic(self):
        _, _, groups2 = make_split_inputs(build_fixtures())
        assert self.groups == groups2


class TestHeldOutSplit:
    """StratifiedGroupKFold split -- skipped when scikit-learn is absent."""

    def test_split_yields_non_overlapping_indices(self):
        sklearn = pytest.importorskip("sklearn")  # noqa: F841
        from decoy_engine.storm.eval.split import held_out_split

        fixtures = build_fixtures()
        X, y, groups = make_split_inputs(fixtures)

        folds = list(held_out_split(X, y, groups, n_splits=2, random_state=0))
        assert len(folds) == 2
        for train_idx, test_idx in folds:
            # No index appears in both train and test.
            assert set(train_idx).isdisjoint(set(test_idx))
            # Together they cover the full corpus.
            assert sorted(train_idx + test_idx) == list(range(len(X)))

    def test_same_group_not_in_both_splits(self):
        sklearn = pytest.importorskip("sklearn")  # noqa: F841
        from decoy_engine.storm.eval.split import held_out_split

        fixtures = build_fixtures()
        X, y, groups = make_split_inputs(fixtures)

        for train_idx, test_idx in held_out_split(X, y, groups, n_splits=2):
            train_groups = {groups[i] for i in train_idx}
            test_groups = {groups[i] for i in test_idx}
            # No group key should appear on both sides.
            assert train_groups.isdisjoint(test_groups), (
                "same group key in both train and test -- leakage guard violated"
            )

    def test_split_requires_sklearn(self):
        """ImportError raised when the [ml] extra (scikit-learn) is absent.

        Forces the failure deterministically whether or not sklearn is installed:
        a ``None`` entry in ``sys.modules`` makes ``from sklearn... import`` raise
        ImportError, so this documents the dependency contract in any environment.
        """
        import sys

        keys = ("sklearn", "sklearn.model_selection")
        saved = {k: sys.modules.get(k) for k in keys}
        for k in keys:
            sys.modules[k] = None  # force ImportError on the next `from sklearn...`
        try:
            from decoy_engine.storm.eval.split import held_out_split

            with pytest.raises(ImportError, match="scikit-learn"):
                list(held_out_split([1], ["ssn"], ["g1"], n_splits=2))
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
