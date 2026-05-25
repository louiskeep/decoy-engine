"""D5c part 3: hash strategy joint-preservation tests.

Pins the behavior the D5b shape-fidelity scorer should validate:

  - Same (column, joints) tuple in source -> same hash in output.
  - Joint frequency distribution preserved (joint-cell counts agree).
  - Different joint values for same column value -> different hashes
    (anti-dictionary-attack property; single-column observation no
    longer leaks the source).
  - Backwards compat: no joint columns -> behaves exactly like the
    pre-D5c apply() path.
  - Determinism: dict ordering of joint_columns does not affect
    output (sorted-by-name internally).
  - Null source value passes through as null.
  - Null joint values do not collide across different combinations.
  - End-to-end through the StrategyManager dispatcher path.
"""
from __future__ import annotations

import pandas as pd

from decoy_engine.transforms.apply_context import ApplyContext
from decoy_engine.transforms.hash import HashStrategy
from decoy_engine.transforms.registry import StrategyManager


def _hash(seed: int = 42) -> HashStrategy:
    return HashStrategy(seed=seed)


# ── backwards compatibility ────────────────────────────────────────────────


class TestNoJointColumnsFallsBackToApply:
    def test_no_ctx_delegates_to_single_column_apply(self) -> None:
        h = _hash()
        col = pd.Series(["alice", "bob", "carol"])
        out_single = h.apply(col, {"column": "name"})
        out_via_ctx = h.apply_with_context(col, {"column": "name"}, None)
        pd.testing.assert_series_equal(out_single, out_via_ctx)

    def test_empty_joint_columns_delegates_to_single_column(self) -> None:
        h = _hash()
        col = pd.Series(["alice", "bob"])
        out_single = h.apply(col, {"column": "name"})
        out_via_empty = h.apply_with_context(
            col, {"column": "name"}, ApplyContext.empty(),
        )
        pd.testing.assert_series_equal(out_single, out_via_empty)


# ── joint preservation core contract ───────────────────────────────────────


class TestJointPreservationCore:
    def test_same_joint_tuple_produces_same_hash(self) -> None:
        """Two rows with identical (col, joint) tuples must hash the same."""
        h = _hash()
        col = pd.Series(["94105", "94105", "10001"])
        zip_city = pd.Series(["SF", "SF", "NYC"])
        ctx = ApplyContext(joint_columns={"city": zip_city})
        out = h.apply_with_context(col, {"column": "zip"}, ctx)
        # Row 0 and 1 share (94105, SF) -> same hash.
        assert out.iloc[0] == out.iloc[1]
        # Row 2 has (10001, NYC) -> different hash.
        assert out.iloc[0] != out.iloc[2]

    def test_different_joint_value_changes_hash_for_same_column(self) -> None:
        """The anti-dictionary-attack property: same column value with
        different joint context yields a different hash."""
        h = _hash()
        # Same zip 94105, but in two different cities (hypothetical).
        col = pd.Series(["94105", "94105"])
        city = pd.Series(["SF", "LA"])
        ctx = ApplyContext(joint_columns={"city": city})
        out = h.apply_with_context(col, {"column": "zip"}, ctx)
        assert out.iloc[0] != out.iloc[1]

    def test_joint_frequency_distribution_preserved(self) -> None:
        """The D5b motivating shape property: the JOINT FREQUENCY
        on the source (zip, city) maps 1:1 onto (hashed_zip, city)
        in the output. Same number of distinct combinations, same
        per-combination counts."""
        h = _hash()
        col = pd.Series(
            ["A"] * 50 + ["B"] * 30 + ["A"] * 20,
        )  # 70 A, 30 B
        joint = pd.Series(
            ["x"] * 50 + ["x"] * 30 + ["y"] * 20,
        )  # 50 (A,x), 30 (B,x), 20 (A,y)
        ctx = ApplyContext(joint_columns={"j": joint})
        out = h.apply_with_context(col, {"column": "c"}, ctx)
        # Three distinct (col, joint) source tuples -> three distinct
        # output hashes, with the same per-tuple frequencies.
        out_with_j = pd.DataFrame({"out": out, "j": joint})
        counts = out_with_j.groupby(["out", "j"]).size().sort_values(ascending=False)
        assert list(counts.values) == [50, 30, 20]


# ── determinism + edge cases ───────────────────────────────────────────────


class TestDeterminismAndEdgeCases:
    def test_dict_ordering_of_joints_does_not_affect_output(self) -> None:
        """The dispatcher's dict ordering of joint_columns must not
        leak into the output bytes. apply_with_context sorts joint
        names internally so {a:..., b:...} and {b:..., a:...} are
        identical at the byte level."""
        h = _hash()
        col = pd.Series(["x", "y"])
        a = pd.Series(["1", "2"])
        b = pd.Series(["p", "q"])
        ctx_a_first = ApplyContext(joint_columns={"a": a, "b": b})
        ctx_b_first = ApplyContext(joint_columns={"b": b, "a": a})
        out_a = h.apply_with_context(col, {"column": "c"}, ctx_a_first)
        out_b = h.apply_with_context(col, {"column": "c"}, ctx_b_first)
        pd.testing.assert_series_equal(out_a, out_b)

    def test_null_in_source_column_passes_through(self) -> None:
        h = _hash()
        col = pd.Series(["a", None, "c"])
        joint = pd.Series(["x", "y", "z"])
        ctx = ApplyContext(joint_columns={"j": joint})
        out = h.apply_with_context(col, {"column": "c"}, ctx)
        assert pd.isna(out.iloc[1])
        # The non-null rows produced real hashes.
        assert isinstance(out.iloc[0], str)
        assert isinstance(out.iloc[2], str)

    def test_null_joint_values_do_not_silently_collide(self) -> None:
        """Test the separator-byte safety: 'X' joined with null must
        not equal '' joined with 'X' once the separator goes in. The
        SEP byte ensures (a='X', b='') != (a='', b='X')."""
        h = _hash()
        col = pd.Series(["v", "v"])  # same column value both rows
        # Row 0: joints = (X, null), Row 1: joints = (null, X)
        a = pd.Series(["X", None])
        b = pd.Series([None, "X"])
        ctx = ApplyContext(joint_columns={"a": a, "b": b})
        out = h.apply_with_context(col, {"column": "c"}, ctx)
        # The two rows must produce different hashes; if the separator
        # was missing or if nulls produced identical composites, these
        # would collide.
        assert out.iloc[0] != out.iloc[1]

    def test_truncate_still_works_with_joint_columns(self) -> None:
        h = _hash()
        col = pd.Series(["a", "b"])
        joint = pd.Series(["x", "y"])
        ctx = ApplyContext(joint_columns={"j": joint})
        out = h.apply_with_context(
            col, {"column": "c", "truncate": 8}, ctx,
        )
        assert len(out.iloc[0]) == 8
        assert len(out.iloc[1]) == 8


# ── dispatcher integration ─────────────────────────────────────────────────


class TestDispatcherJointWiring:
    def test_apply_masking_rules_threads_joint_with_directive(self) -> None:
        """End-to-end: rule with joint_with directive -> hash strategy
        receives the joint columns via ctx -> joint frequency preserved."""
        mgr = StrategyManager(seed=42)
        df = pd.DataFrame({
            "zip": ["94105", "94105", "10001"],
            "city": ["SF", "SF", "NYC"],
        })
        rules = [{"column": "zip", "type": "hash", "joint_with": ["city"]}]
        out_df = mgr.apply_masking_rules(df, rules)
        # First two rows share (94105, SF) -> identical hashed zip;
        # third row's (10001, NYC) differs.
        assert out_df["zip"].iloc[0] == out_df["zip"].iloc[1]
        assert out_df["zip"].iloc[0] != out_df["zip"].iloc[2]
        # Joint column itself is unchanged (only the main column was masked).
        pd.testing.assert_series_equal(out_df["city"], df["city"])

    def test_apply_masking_rules_without_joint_with_unchanged(self) -> None:
        """Rules without joint_with behave exactly like pre-D5c output
        (regression guard for the back-compat path)."""
        mgr = StrategyManager(seed=42)
        df = pd.DataFrame({"x": ["alice", "bob", "alice"]})
        rules_pre_d5c = [{"column": "x", "type": "hash"}]
        out = mgr.apply_masking_rules(df, rules_pre_d5c)
        # Row 0 and 2 share value 'alice' -> identical hash; row 1
        # differs. This is the same property hash always had; we are
        # just confirming D5c routing didn't break it.
        assert out["x"].iloc[0] == out["x"].iloc[2]
        assert out["x"].iloc[0] != out["x"].iloc[1]

    def test_joint_uses_source_frame_not_in_progress_result(self) -> None:
        """If `zip` is masked AFTER `city`, and `zip` joint_with=['city'],
        the hash must use the ORIGINAL city (not the masked city), so
        the joint preservation still holds even when the joint column
        is itself a masked column earlier in the rule list."""
        mgr = StrategyManager(seed=42)
        df = pd.DataFrame({
            "city": ["SF", "SF", "NYC"],
            "zip":  ["94105", "94105", "10001"],
        })
        rules = [
            {"column": "city", "type": "hash"},
            {"column": "zip", "type": "hash", "joint_with": ["city"]},
        ]
        out_df = mgr.apply_masking_rules(df, rules)
        # First two rows share source (94105, SF) -> identical hashed
        # zip in output, regardless of how city was masked first.
        assert out_df["zip"].iloc[0] == out_df["zip"].iloc[1]
        assert out_df["zip"].iloc[0] != out_df["zip"].iloc[2]


# ── shape-fidelity scorer regression ───────────────────────────────────────


class TestShapeFidelityOnJointPreservedOutput:
    """Capstone: the D5b shape scorer should report ~1.0 on the
    joint (zip, city) for hash-with-joint-preservation, where it
    would have reported lower for plain hash (because plain hash
    breaks the joint by hashing zip independently of city)."""

    def test_joint_hash_keeps_joint_shape_score_perfect(self) -> None:
        from decoy_engine.quality.shape_fidelity import compute_shape_fidelity
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        # Build a realistic distribution: 3 cities x 3 zips, weighted.
        rows = []
        for city, zip_, count in [
            ("SF", "94105", 50), ("SF", "94110", 30),
            ("NYC", "10001", 40), ("NYC", "10002", 20),
            ("LA", "90001", 25), ("LA", "90002", 15),
        ]:
            rows += [{"city": city, "zip": zip_}] * count
        src = pd.DataFrame(rows)

        mgr = StrategyManager(seed=42)
        rules = [
            {"column": "city", "type": "hash"},
            {"column": "zip", "type": "hash", "joint_with": ["city"]},
        ]
        out = mgr.apply_masking_rules(src, rules)

        src_snap = compute_distribution_snapshot(
            src, joint_columns=[("city", "zip")],
        )
        out_snap = compute_distribution_snapshot(
            out, joint_columns=[("city", "zip")],
        )
        shape = compute_shape_fidelity(src_snap, out_snap)

        # The whole point: the joint shape score is ~1.0 because the
        # masked (hashed_city, hashed_zip) joint frequencies match the
        # source (city, zip) joint frequencies cell-for-cell.
        joints = shape["pairwise"]["joints"]
        assert len(joints) >= 1
        joint_entry = joints[0]
        assert joint_entry["comparable"] is True
        assert joint_entry["shape_similarity"] == 1.0
