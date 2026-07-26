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


# ── Validation: machine-observable error fields ───────────────────────────────


class TestValidationMachineFields:
    """Each rejected config must carry the exact machine `code` and `path`.

    Callers route on `PlanCompileError.code`/`.path` (UI field targeting, CLI
    exit semantics), not the human message, so both are contract-level outputs
    and must stay pinned per failure kind.
    """

    def test_valid_config_is_accepted(self):
        """The canonical valid config must pass validation unchanged."""
        validate_joint_mask_config(_make_config())  # must not raise

    def test_each_failure_kind_has_exact_code_and_path(self):
        cases = [
            (
                {"reference": "us_zip5_city_state", "key_by": "patient_id"},
                "joint_mask_columns_missing",
                "joint_masks.columns",
            ),
            (
                {"columns": [], "reference": "us_zip5_city_state", "key_by": "patient_id"},
                "joint_mask_columns_missing",
                "joint_masks.columns",
            ),
            (
                {"columns": _ZIP_COLS, "reference": "us_zip5_city_state"},
                "joint_mask_key_by_missing",
                "joint_masks.key_by",
            ),
            (
                {"columns": _ZIP_COLS, "key_by": "patient_id"},
                "joint_mask_reference_missing",
                "joint_masks.reference",
            ),
            (
                {"columns": _ZIP_COLS, "reference": "", "key_by": "patient_id"},
                "joint_mask_reference_missing",
                "joint_masks.reference",
            ),
            (
                {
                    "columns": _ZIP_COLS,
                    "reference": "no_such_table_xyz",
                    "key_by": "patient_id",
                },
                "joint_mask_reference_not_found",
                "joint_masks.reference",
            ),
        ]
        for cfg, code, path in cases:
            with pytest.raises(PlanCompileError) as exc:
                validate_joint_mask_config(cfg)
            assert exc.value.code == code, cfg
            assert exc.value.path == path, cfg

    def test_column_not_in_reference_code_and_path(self):
        """An unknown target column names itself in the error path."""
        bad = _make_config(columns=["nonexistent_col_abc"])
        with pytest.raises(PlanCompileError) as exc:
            validate_joint_mask_config(bad)
        assert exc.value.code == "joint_mask_column_not_in_reference"
        assert exc.value.path == "joint_masks.columns.nonexistent_col_abc"

    def test_id_column_is_not_a_maskable_target(self):
        """'id' is reference-table infrastructure, not a domain column; a config
        that lists it as a target must be rejected (the id key is excluded from
        the maskable column set)."""
        bad = _make_config(columns=["id"])
        with pytest.raises(PlanCompileError) as exc:
            validate_joint_mask_config(bad)
        assert exc.value.code == "joint_mask_column_not_in_reference"

    def test_unreadable_reference_table_reports_invalid(self, tmp_path):
        """A customer table that loads but violates the schema (no id column)
        surfaces as reference_invalid, distinct from not_found."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        bad_path = tmp_path / "no_id.parquet"
        pq.write_table(pa.table({"zip": ["1"], "city": ["x"], "state": ["Y"]}), str(bad_path))
        bad = {"columns": ["zip"], "reference": f"customer:{bad_path}", "key_by": "k"}
        with pytest.raises(PlanCompileError) as exc:
            validate_joint_mask_config(bad)
        assert exc.value.code == "joint_mask_reference_invalid"
        assert exc.value.path == "joint_masks.reference"


# ── apply_joint_mask: mode + namespace routing ────────────────────────────────


class TestApplyRouting:
    def test_default_mode_is_mask(self):
        """Omitting `mode` must select mask mode, matching an explicit mask call."""
        df = _make_df(6)
        config = JointMaskConfig.from_dict(_make_config())
        implicit = apply_joint_mask(df, config, job_seed=_JOB_SEED)
        explicit = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        for col in _ZIP_COLS:
            assert implicit[col].tolist() == explicit[col].tolist()

    def test_namespace_feeds_the_hmac_key(self):
        """The namespace argument must reach the HMAC key derivation: a distinct
        namespace must produce a distinct reference-row mapping for the same
        keys and seed."""
        keys = [f"patient_{i}" for i in range(15)]
        df = pd.DataFrame(
            {"patient_id": keys, "zip": ["0"] * 15, "city": ["x"] * 15, "state": ["X"] * 15}
        )
        config = JointMaskConfig.from_dict(_make_config())
        default_ns = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        custom_ns = apply_joint_mask(
            df, config, mode="mask", job_seed=_JOB_SEED, namespace="custom_ns"
        )
        assert default_ns["zip"].tolist() != custom_ns["zip"].tolist()


# ── _pick_rows_mask: keyed selection, fallback, null handling ─────────────────


class TestMaskRowSelection:
    def test_selection_uses_secret_derived_hmac_key(self):
        """Row selection must run under derive(job_seed, namespace, source), the
        secret-derived key (DE-02), not the public reference-table salt. Pinning
        the output to the derived-key selection proves the run secret drives the
        real-value -> reference-row mapping."""
        from decoy_engine.determinism import derive
        from decoy_engine.transforms.joint_mask import (
            _DEFAULT_NAMESPACE,
            _KEYED_ROW_SOURCE,
        )

        keys = [f"K{i}" for i in range(6)]
        df = pd.DataFrame(
            {"patient_id": keys, "zip": ["0"] * 6, "city": ["x"] * 6, "state": ["X"] * 6}
        )
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)

        hmac_key = derive(_JOB_SEED, _DEFAULT_NAMESPACE, _KEYED_ROW_SOURCE)
        tbl = config.table
        for i, key in enumerate(keys):
            expected = tbl.keyed_row(key, hmac_key=hmac_key)
            out = (result.iloc[i]["zip"], result.iloc[i]["city"], result.iloc[i]["state"])
            assert out == (expected["zip"], expected["city"], expected["state"])

    def test_keyed_selection_golden_output_is_pinned(self):
        """Lock the frozen DE-02 keyed surface by a HARDCODED golden, not a value
        recomputed from the constants. A silent change to `_KEYED_ROW_SOURCE` (the
        domain-separation label) or `_DEFAULT_NAMESPACE` would alter every masked
        output and break cross-version determinism; this literal expected value
        catches that where a self-derived oracle cannot."""
        keys = [f"K{i}" for i in range(6)]
        df = pd.DataFrame(
            {"patient_id": keys, "zip": ["0"] * 6, "city": ["x"] * 6, "state": ["X"] * 6}
        )
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        out = [
            (result.iloc[i]["zip"], result.iloc[i]["city"], result.iloc[i]["state"])
            for i in range(6)
        ]
        assert out == [
            ("30302", "Atlanta", "GA"),
            ("92104", "San Diego", "CA"),
            ("85004", "Phoenix", "AZ"),
            ("98103", "Seattle", "WA"),
            ("77002", "Houston", "TX"),
            ("77003", "Houston", "TX"),
        ]

    def test_unsupported_mode_fails_closed(self):
        """An unrecognized mode must fail closed (ValueError), not silently emit."""
        df = pd.DataFrame({"patient_id": ["K0"], "zip": ["0"], "city": ["x"], "state": ["X"]})
        config = JointMaskConfig.from_dict(_make_config())
        with pytest.raises(ValueError):
            apply_joint_mask(df, config, mode="bogus", job_seed=_JOB_SEED)

    def test_missing_key_column_falls_back_per_row(self):
        """When the key_by column is absent, every row must fall back to a seeded
        random reference row; the output row count and validity are preserved."""
        from decoy_engine.reference_tables import load_table

        df = pd.DataFrame({"zip": ["0"] * 4, "city": ["x"] * 4, "state": ["X"] * 4})
        config = JointMaskConfig.from_dict(_make_config())  # key_by=patient_id, absent here
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)

        assert len(result) == 4
        tbl = load_table("us_zip5_city_state")
        valid = {
            (tbl.row(i)["zip"], tbl.row(i)["city"], tbl.row(i)["state"])
            for i in range(tbl.row_count)
        }
        for _, row in result.iterrows():
            assert (row["zip"], row["city"], row["state"]) in valid

    def test_float_key_values_are_treated_as_present(self):
        """A non-null float key must take the keyed branch (isnan is checked on
        the value, not a constant); it must not crash or fall back."""
        df = pd.DataFrame(
            {
                "patient_id": [3.14, 2.71, 1.41],
                "zip": ["0"] * 3,
                "city": ["x"] * 3,
                "state": ["X"] * 3,
            }
        )
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)

        tbl = config.table
        valid = {
            (tbl.row(i)["zip"], tbl.row(i)["city"], tbl.row(i)["state"])
            for i in range(tbl.row_count)
        }
        for _, row in result.iterrows():
            assert (row["zip"], row["city"], row["state"]) in valid

    def test_null_key_fallback_is_seed_deterministic(self):
        """Null key_by values use a seeded fallback RNG; the same seed must
        reproduce identical fallback rows across runs."""
        df = pd.DataFrame(
            {"patient_id": [None] * 8, "zip": ["0"] * 8, "city": ["x"] * 8, "state": ["X"] * 8}
        )
        config = JointMaskConfig.from_dict(_make_config())
        r1 = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        r2 = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        assert r1["zip"].tolist() == r2["zip"].tolist()
        assert r1["city"].tolist() == r2["city"].tolist()

    def test_null_key_fallback_seed_is_first_eight_bytes(self):
        """The fallback RNG is seeded from the first 8 bytes of job_seed: two
        seeds equal in those bytes must give identical fallback rows."""
        df = pd.DataFrame(
            {"patient_id": [None] * 8, "zip": ["0"] * 8, "city": ["x"] * 8, "state": ["X"] * 8}
        )
        config = JointMaskConfig.from_dict(_make_config())
        seed_a = bytes(range(8)) + b"\x00" * 24
        seed_b = bytes(range(8)) + b"\xff" * 24  # same first 8 bytes, different tail
        ra = apply_joint_mask(df, config, mode="mask", job_seed=seed_a)
        rb = apply_joint_mask(df, config, mode="mask", job_seed=seed_b)
        assert ra["zip"].tolist() == rb["zip"].tolist()

    def test_null_keys_produce_varied_fallback_rows(self):
        """Each null key draws independently from the fallback RNG, so many null
        keys must not collapse to a single repeated row (which is what treating
        a null as an ordinary HMAC key would produce)."""
        df = pd.DataFrame(
            {"patient_id": [None] * 12, "zip": ["0"] * 12, "city": ["x"] * 12, "state": ["X"] * 12}
        )
        config = JointMaskConfig.from_dict(_make_config())
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        tuples = set(
            zip(result["zip"], result["city"], result["state"], strict=True),
        )
        assert len(tuples) > 1

    def test_null_key_fallback_can_select_first_row(self, tmp_path):
        """The fallback samples the full [0, row_count) range, so with a two-row
        table both rows must appear (row index 0 is reachable)."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = tmp_path / "two_rows.parquet"
        pq.write_table(
            pa.table({"id": pa.array([1, 2], type=pa.int64()), "code": ["A", "B"]}), str(path)
        )
        n = 60
        df = pd.DataFrame({"entity_id": [None] * n, "code": ["x"] * n})
        config = JointMaskConfig.from_dict(
            {"columns": ["code"], "reference": f"customer:{path}", "key_by": "entity_id"}
        )
        result = apply_joint_mask(df, config, mode="mask", job_seed=_JOB_SEED)
        assert set(result["code"].tolist()) == {"A", "B"}


# ── _pick_rows_gen: seeded sampling ───────────────────────────────────────────


class TestGenRowSelection:
    def test_gen_seed_is_first_eight_bytes(self):
        """Gen mode seeds its RNG from the first 8 bytes of job_seed: two seeds
        equal in those bytes must produce identical output."""
        df = _make_df(8)
        config = JointMaskConfig.from_dict(_make_config())
        seed_a = bytes(range(8)) + b"\x00" * 24
        seed_b = bytes(range(8)) + b"\xff" * 24
        ra = apply_joint_mask(df, config, mode="gen", job_seed=seed_a)
        rb = apply_joint_mask(df, config, mode="gen", job_seed=seed_b)
        assert ra["zip"].tolist() == rb["zip"].tolist()

    def test_gen_sampling_can_select_first_row(self, tmp_path):
        """Gen sampling spans the full [0, row_count) range; with a two-row
        table both rows must appear (row index 0 is reachable)."""
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = tmp_path / "two_rows_gen.parquet"
        pq.write_table(
            pa.table({"id": pa.array([1, 2], type=pa.int64()), "code": ["A", "B"]}), str(path)
        )
        n = 60
        df = pd.DataFrame({"code": ["x"] * n})
        config = JointMaskConfig.from_dict(
            {"columns": ["code"], "reference": f"customer:{path}", "key_by": "unused_in_gen"}
        )
        result = apply_joint_mask(df, config, mode="gen", job_seed=_JOB_SEED)
        assert set(result["code"].tolist()) == {"A", "B"}
