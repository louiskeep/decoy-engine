"""SP-08 joint_mask strategy tests (TDD: tests land first).

Tests cover:
  A.1 - Mask mode: keyed HMAC picks a row; output is a real reference row.
  A.2 - Gen mode: seeded random sampling; deterministic with fixed seed.
  A.3 - Output validity: every output tuple exists in the reference table.
  A.4 - Config-time validation: missing/wrong fields raise PlanCompileError.
  A.5 - SP-06 keyed-access caveat inherited + documented.

Methodology: HMAC-SHA256-keyed row selection via ReferenceTable.keyed_row
(RFC 2104; same primitive as date_shift + fpe). Gen mode uses numpy.default_rng
seeded from the job seed for determinism without a key.
"""

from __future__ import annotations

import pandas as pd
import pytest

from decoy_engine.plan._errors import PlanCompileError
from decoy_engine.transforms.joint_mask import (
    JointMaskConfig,
    apply_joint_mask,
    validate_joint_mask_config,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

# Stable test key; 32 bytes. Not a real secret.
_JOB_SEED = b"\xca\xfe" * 16

# Columns in us_zip5_city_state that joint_mask will write.
_ZIP_COLS = ["zip", "city", "state"]


def _make_df(n: int = 5) -> pd.DataFrame:
    """DataFrame with placeholder values in the target columns + a key column."""
    return pd.DataFrame(
        {
            "patient_id": [f"P{i:04d}" for i in range(n)],
            "zip": ["00000"] * n,
            "city": ["placeholder"] * n,
            "state": ["XX"] * n,
        }
    )


def _make_config(**overrides) -> dict:
    base = {
        "columns": _ZIP_COLS,
        "reference": "us_zip5_city_state",
        "key_by": "patient_id",
    }
    base.update(overrides)
    return base


# ── A.1: Mask mode - keyed HMAC selection ─────────────────────────────────────


class TestMaskMode:
    def test_output_is_a_real_reference_row(self):
        """Every output (zip, city, state) tuple must be a real row from the table."""
        from decoy_engine.reference_tables import load_table

        df = _make_df(10)
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)

        tbl = load_table("us_zip5_city_state")
        valid_tuples = {
            (tbl.row(i)["zip"], tbl.row(i)["city"], tbl.row(i)["state"])
            for i in range(tbl.row_count)
        }
        for _, row in result.iterrows():
            out_tuple = (row["zip"], row["city"], row["state"])
            assert out_tuple in valid_tuples, (
                f"Output tuple {out_tuple!r} is not a real row in us_zip5_city_state. "
                "joint_mask must only produce real reference-table rows."
            )

    def test_same_key_same_output(self):
        """Identical key_by values must produce identical output tuples."""
        df = pd.DataFrame(
            {
                "patient_id": ["SAME_KEY", "SAME_KEY", "OTHER_KEY"],
                "zip": ["00000"] * 3,
                "city": ["x"] * 3,
                "state": ["X"] * 3,
            }
        )
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)

        row0 = (result.iloc[0]["zip"], result.iloc[0]["city"], result.iloc[0]["state"])
        row1 = (result.iloc[1]["zip"], result.iloc[1]["city"], result.iloc[1]["state"])
        assert row0 == row1, "Same key_by value must produce the same output tuple."

    def test_different_keys_may_differ(self):
        """Different key_by values should (statistically) produce different tuples.

        With 50 rows the probability of all landing on the same row is
        (1/50)^49 ~ 10^-83. A collision here means the keying is broken.
        """
        keys = [f"patient_{i}" for i in range(10)]
        df = pd.DataFrame(
            {
                "patient_id": keys,
                "zip": ["00000"] * 10,
                "city": ["x"] * 10,
                "state": ["X"] * 10,
            }
        )
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        tuples = list(
            zip(
                result["zip"].tolist(),
                result["city"].tolist(),
                result["state"].tolist(),
                strict=True,
            )
        )
        assert len(set(tuples)) > 1, "Different keys should map to different output rows."

    def test_mask_mode_does_not_mutate_input(self):
        """apply_joint_mask must not mutate the original DataFrame."""
        df = _make_df(3)
        original_zip = df["zip"].tolist()
        config = JointMaskConfig.from_dict(_make_config())
        _ = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        assert df["zip"].tolist() == original_zip, "apply_joint_mask must not mutate df."

    def test_tuple_consistency_boulder_co(self):
        """Masked output for a Boulder-area key must be a real (zip, city, state) row.

        The spec requirement: masked output tuple is internally consistent
        (city is in state, ZIP is in city) because every row IS a real reference row.
        This test verifies that property holds for a specific scenario.
        """
        from decoy_engine.reference_tables import load_table

        df = pd.DataFrame(
            {
                "patient_id": ["boulder_patient"],
                "zip": ["80301"],
                "city": ["Boulder"],
                "state": ["CO"],
            }
        )
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)

        tbl = load_table("us_zip5_city_state")
        valid_tuples = {
            (tbl.row(i)["zip"], tbl.row(i)["city"], tbl.row(i)["state"])
            for i in range(tbl.row_count)
        }
        out = (result.iloc[0]["zip"], result.iloc[0]["city"], result.iloc[0]["state"])
        assert out in valid_tuples, (
            f"Masked output {out!r} must be a real reference row (Boulder -> consistent tuple)."
        )


# ── A.2: Gen mode - seeded random sampling ────────────────────────────────────


class TestGenMode:
    def test_gen_mode_determinism_with_seed(self):
        """Same seed + same input size produces identical output."""
        df1 = _make_df(5)
        df2 = _make_df(5)
        config = JointMaskConfig.from_dict(_make_config())
        seed = b"\x01" * 32
        r1 = apply_joint_mask(df1, config, mode="gen", job_seed=seed)
        r2 = apply_joint_mask(df2, config, mode="gen", job_seed=seed)
        assert r1["zip"].tolist() == r2["zip"].tolist(), "Gen mode must be seed-deterministic."
        assert r1["city"].tolist() == r2["city"].tolist()
        assert r1["state"].tolist() == r2["state"].tolist()

    def test_gen_mode_different_seeds_differ(self):
        """Different seeds should produce different outputs."""
        df1 = _make_df(20)
        df2 = _make_df(20)
        r1 = apply_joint_mask(
            df1,
            JointMaskConfig.from_dict(_make_config()),
            mode="gen",
            job_seed=b"\x01" * 32,
        )
        r2 = apply_joint_mask(
            df2,
            JointMaskConfig.from_dict(_make_config()),
            mode="gen",
            job_seed=b"\x02" * 32,
        )
        assert r1["zip"].tolist() != r2["zip"].tolist(), "Different seeds must differ."

    def test_gen_mode_output_is_a_real_reference_row(self):
        """Every gen-mode output tuple must be a real reference-table row."""
        from decoy_engine.reference_tables import load_table

        df = _make_df(20)
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="gen", job_seed=_JOB_SEED)

        tbl = load_table("us_zip5_city_state")
        valid_tuples = {
            (tbl.row(i)["zip"], tbl.row(i)["city"], tbl.row(i)["state"])
            for i in range(tbl.row_count)
        }
        for _, row in result.iterrows():
            out = (row["zip"], row["city"], row["state"])
            assert out in valid_tuples, f"Gen-mode output {out!r} not in reference table."


# ── A.3: Output validity across modes ─────────────────────────────────────────


class TestOutputValidity:
    def test_output_columns_written(self):
        """Target columns must be written; other columns must be unchanged."""
        df = _make_df(4)
        original_ids = df["patient_id"].tolist()
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        assert result["patient_id"].tolist() == original_ids, "Non-target columns unchanged."
        for col in _ZIP_COLS:
            assert col in result.columns
            assert not any(v is None for v in result[col].tolist()), (
                f"Column {col!r} must not contain None after joint_mask."
            )

    def test_output_preserves_nulls_in_key_by(self):
        """Null key_by values must be handled gracefully (no crash; null or passthrough)."""
        df = pd.DataFrame(
            {
                "patient_id": [None, "real_key"],
                "zip": ["00000", "00000"],
                "city": ["x", "x"],
                "state": ["X", "X"],
            }
        )
        config = JointMaskConfig.from_dict(_make_config())
        # Should not raise; null key_by rows may passthrough or be suppressed.
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        assert len(result) == 2


# ── A.4: Config-time validation ───────────────────────────────────────────────


class TestConfigValidation:
    def test_missing_columns_raises(self):
        """A config without 'columns' must raise PlanCompileError at config time."""
        bad = {"reference": "us_zip5_city_state", "key_by": "patient_id"}
        with pytest.raises(PlanCompileError, match="columns"):
            validate_joint_mask_config(bad)

    def test_wrong_reference_name_raises(self):
        """A reference table that doesn't exist must raise PlanCompileError."""
        bad = _make_config(reference="nonexistent_table_xyz")
        with pytest.raises(PlanCompileError, match="reference"):
            validate_joint_mask_config(bad)

    def test_missing_key_by_raises(self):
        """A config without 'key_by' must raise PlanCompileError."""
        bad = {"columns": _ZIP_COLS, "reference": "us_zip5_city_state"}
        with pytest.raises(PlanCompileError, match="key_by"):
            validate_joint_mask_config(bad)

    def test_columns_not_in_reference_raises(self):
        """Columns not present in the reference table must raise PlanCompileError."""
        bad = _make_config(columns=["nonexistent_col_abc"])
        with pytest.raises(PlanCompileError, match="column"):
            validate_joint_mask_config(bad)

    def test_valid_config_does_not_raise(self):
        """A correct config must pass validation without raising."""
        good = _make_config()
        validate_joint_mask_config(good)  # must not raise

    def test_from_dict_validates(self):
        """JointMaskConfig.from_dict must raise PlanCompileError on bad config."""
        bad = {"columns": _ZIP_COLS}
        with pytest.raises(PlanCompileError):
            JointMaskConfig.from_dict(bad)

    def test_string_columns_already_rejected(self):
        """Sprint 13 / coercion-13 S3 investigation (GATE-1 Q4 sibling audit,
        2026-07-03): the guide's D5/D7 flags `tuple(cfg["columns"])`
        iterating characters when `columns` is a plain string, mirroring the
        truncate/bucketize/categorical silent-leak class. Verified NOT
        reachable here: `validate_joint_mask_config` iterates `columns`
        per-element against the reference table's real (multi-character)
        column names, so a string like "zip,city,state" fails on its first
        character ('z' is not a column of any shipped reference table)
        before any row is ever masked. No new engine check is added for
        joint_mask; this test locks the existing fail-closed behavior in
        place as a regression guard, and documents that this sibling was
        found already-safe rather than fixed."""
        bad = _make_config(columns="zip,city,state")
        with pytest.raises(PlanCompileError) as exc:
            validate_joint_mask_config(bad)
        assert exc.value.code == "joint_mask_column_not_in_reference"


# ── A.5: SP-06 keyed-access caveat ───────────────────────────────────────────


class TestKeyedAccessCaveat:
    def test_joint_mask_config_has_caveat_note(self):
        """JointMaskConfig must carry the SP-06 cross-version caveat as a docstring.

        This is a structural test: it ensures the caveat is documented in the
        type that users configure, not buried in a comment.
        """
        assert JointMaskConfig.__doc__ is not None
        lower = JointMaskConfig.__doc__.lower()
        assert "cross-version" in lower or "row_count" in lower or "keyed_row" in lower, (
            "JointMaskConfig docstring must reference the SP-06 keyed_row "
            "cross-version stability caveat."
        )
