"""`statistical` generate type (capability-gaps WS3, 2026-06-12).

Samples synthetic columns from a distribution-snapshot/v1 artifact
(quality/snapshot.py) instead of a hand-declared faker/categorical
config: histogram inverse-CDF for numeric, weighted top-k for
categorical, year-bin + uniform-within-year for datetime, declared-pair
conditional sampling via the snapshot's joint contingency tables.
compute_fidelity is the acceptance oracle for distribution shape.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from decoy_engine.generation.statistical import StatisticalSpecError, load_spec, sample_column


def _write_snapshot(tmp_path, df: pd.DataFrame, joints=None) -> str:
    from decoy_engine.quality.snapshot import compute_distribution_snapshot

    snap = compute_distribution_snapshot(df, joint_columns=joints)
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


def _source_df() -> pd.DataFrame:
    import numpy as np

    rng = np.random.default_rng(7)
    n = 2_000
    states = rng.choice(["CA", "NY", "TX", "WA"], size=n, p=[0.5, 0.3, 0.15, 0.05])
    # tier correlates hard with state: CA -> gold, NY -> silver, rest -> bronze.
    tier = [
        {"CA": "gold", "NY": "silver"}.get(s, "bronze") for s in states
    ]
    return pd.DataFrame(
        {
            "amount": rng.normal(100.0, 25.0, size=n).round(2),
            "age": rng.integers(18, 80, size=n),
            "state": states,
            "tier": tier,
            "joined": pd.to_datetime("2020-01-01")
            + pd.to_timedelta(rng.integers(0, 365 * 5, size=n), unit="D"),
        }
    )


def _col(name: str, snapshot_file: str, **extra) -> dict:
    return {"name": name, "type": "statistical", "snapshot_file": snapshot_file, **extra}


class TestNumericSampling:
    def test_values_in_source_range_and_deterministic(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = load_spec(_col("amount", snap))
        a = sample_column(spec, 500, col_seed=1234)
        b = sample_column(spec, 500, col_seed=1234)
        assert a == b
        assert all(df["amount"].min() <= v <= df["amount"].max() for v in a)
        assert all(isinstance(v, float) for v in a)

    def test_different_seed_different_values(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        spec = load_spec(_col("amount", snap))
        assert sample_column(spec, 200, col_seed=1) != sample_column(spec, 200, col_seed=2)

    def test_integer_dtype_emits_ints(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = load_spec(_col("age", snap))
        out = sample_column(spec, 300, col_seed=9)
        assert all(isinstance(v, int) for v in out)
        assert all(df["age"].min() <= v <= df["age"].max() for v in out)

    def test_distribution_shape_matches_source(self, tmp_path):
        """compute_fidelity is the acceptance oracle: synthetic vs source
        must score clearly above what a uniform stand-in would."""
        from decoy_engine.quality.fidelity import compute_fidelity
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = load_spec(_col("amount", snap))
        synth = pd.DataFrame({"amount": sample_column(spec, 2_000, col_seed=77)})
        report = compute_fidelity(
            compute_distribution_snapshot(df[["amount"]]),
            compute_distribution_snapshot(synth),
        )
        assert report["overall_score"] >= 0.85, report


class TestCategoricalSampling:
    def test_requires_allow_real_categories(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        with pytest.raises(StatisticalSpecError) as exc:
            load_spec(_col("state", snap))
        assert exc.value.code == "statistical_real_categories_not_allowed"

    def test_redistribute_emits_only_top_values(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = load_spec(_col("state", snap, allow_real_categories=True))
        out = sample_column(spec, 1_000, col_seed=5)
        assert set(out) <= {"CA", "NY", "TX", "WA"}
        # Rough shape: CA is the majority class at p=0.5.
        assert 350 <= out.count("CA") <= 650

    def test_emit_mode_emits_other_token(self, tmp_path):
        # Force a tail: top_k=2 collapses TX/WA into other_count.
        from decoy_engine.quality.snapshot import compute_distribution_snapshot

        df = _source_df()
        snap_dict = compute_distribution_snapshot(df, categorical_top_k=2)
        path = tmp_path / "s.json"
        path.write_text(json.dumps(snap_dict), encoding="utf-8")
        spec = load_spec(
            _col("state", str(path), allow_real_categories=True, other_mode="emit")
        )
        out = sample_column(spec, 1_000, col_seed=5)
        assert "__other__" in set(out)
        assert set(out) <= {"CA", "NY", "__other__"}

    def test_deterministic(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        spec = load_spec(_col("state", snap, allow_real_categories=True))
        assert sample_column(spec, 400, col_seed=3) == sample_column(spec, 400, col_seed=3)


class TestDatetimeSampling:
    def test_within_source_range_and_deterministic(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = load_spec(_col("joined", snap))
        a = sample_column(spec, 300, col_seed=11)
        b = sample_column(spec, 300, col_seed=11)
        assert a == b
        lo, hi = df["joined"].min(), df["joined"].max()
        assert all(lo <= pd.Timestamp(v) <= hi for v in a)


class TestConditionalSampling:
    def test_condition_on_respects_joint(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df, joints=[("state", "tier")])
        parent_spec = load_spec(_col("state", snap, allow_real_categories=True))
        parents = sample_column(parent_spec, 1_000, col_seed=21)
        child_spec = load_spec(
            _col("tier", snap, allow_real_categories=True, condition_on="state")
        )
        children = sample_column(child_spec, 1_000, col_seed=22, parent_values=parents)
        # The source correlation is deterministic: CA -> gold, NY -> silver.
        pairs = list(zip(parents, children, strict=True))
        ca = [t for s, t in pairs if s == "CA"]
        ny = [t for s, t in pairs if s == "NY"]
        assert ca and ca.count("gold") / len(ca) > 0.9
        assert ny and ny.count("silver") / len(ny) > 0.9

    def test_condition_on_requires_joint_in_snapshot(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())  # no joints captured
        with pytest.raises(StatisticalSpecError) as exc:
            load_spec(_col("tier", snap, allow_real_categories=True, condition_on="state"))
        assert exc.value.code == "statistical_joint_missing"


class TestSpecErrors:
    def test_missing_snapshot_file(self, tmp_path):
        with pytest.raises(StatisticalSpecError) as exc:
            load_spec(_col("amount", str(tmp_path / "nope.json")))
        assert exc.value.code == "statistical_snapshot_unreadable"

    def test_unknown_column(self, tmp_path):
        snap = _write_snapshot(tmp_path, _source_df())
        with pytest.raises(StatisticalSpecError) as exc:
            load_spec(_col("ghost", snap))
        assert exc.value.code == "statistical_column_not_in_snapshot"

    def test_freetext_kind_rejected(self, tmp_path):
        df = pd.DataFrame({"notes": [f"long free text value number {i} with words" for i in range(40)]})
        snap = _write_snapshot(tmp_path, df)
        with pytest.raises(StatisticalSpecError) as exc:
            load_spec(_col("notes", snap))
        assert exc.value.code == "statistical_kind_unsupported"

    def test_source_column_override(self, tmp_path):
        df = _source_df()
        snap = _write_snapshot(tmp_path, df)
        spec = load_spec(_col("renamed_amount", snap, source_column="amount"))
        out = sample_column(spec, 50, col_seed=2)
        assert len(out) == 50


class TestGenerateTablesIntegration:
    def test_statistical_column_through_generate_tables(self, tmp_path):
        from decoy_engine.config import PipelineConfig
        from decoy_engine.generation.synthesize import generate_tables

        df = _source_df()
        snap = _write_snapshot(tmp_path, df, joints=[("state", "tier")])
        cfg = {
            "version": 1,
            "global_settings": {"seed": 42},
            "tables": [
                {
                    "name": "synthetic",
                    "row_count": 200,
                    "generate_columns": [
                        {
                            "name": "amount",
                            "type": "statistical",
                            "snapshot_file": snap,
                        },
                        {
                            "name": "state",
                            "type": "statistical",
                            "snapshot_file": snap,
                            "allow_real_categories": True,
                        },
                        {
                            "name": "tier",
                            "type": "statistical",
                            "snapshot_file": snap,
                            "allow_real_categories": True,
                            "condition_on": "state",
                        },
                    ],
                }
            ],
            "targets": {
                "synthetic": {
                    "type": "file",
                    "format": "csv",
                    "path": str(tmp_path / "out.csv"),
                }
            },
        }
        validated = PipelineConfig.model_validate(cfg).model_dump()
        out = generate_tables(validated)
        tbl = out["synthetic"]
        assert tbl.num_rows == 200
        states = tbl.column("state").to_pylist()
        tiers = tbl.column("tier").to_pylist()
        ca_tiers = [t for s, t in zip(states, tiers, strict=True) if s == "CA"]
        assert ca_tiers and ca_tiers.count("gold") / len(ca_tiers) > 0.9
        # Determinism: same config, same bytes.
        again = generate_tables(validated)
        assert again["synthetic"].equals(tbl)

    def test_condition_on_must_reference_earlier_column(self, tmp_path):
        from decoy_engine.generation.synthesize import generate_tables

        df = _source_df()
        snap = _write_snapshot(tmp_path, df, joints=[("state", "tier")])
        cfg = {
            "global_settings": {"seed": 42},
            "tables": [
                {
                    "name": "synthetic",
                    "row_count": 10,
                    "generate_columns": [
                        {
                            # tier conditions on state, but state comes later.
                            "name": "tier",
                            "type": "statistical",
                            "snapshot_file": snap,
                            "allow_real_categories": True,
                            "condition_on": "state",
                        },
                        {
                            "name": "state",
                            "type": "statistical",
                            "snapshot_file": snap,
                            "allow_real_categories": True,
                        },
                    ],
                }
            ],
        }
        with pytest.raises(StatisticalSpecError) as exc:
            generate_tables(cfg)
        assert exc.value.code == "statistical_condition_column_unavailable"


class TestCompileCheck:
    """Row 12 (`statistical_columns`): config-only callers reject a bad
    snapshot/config pairing before a run."""

    def _cfg(self, cols: list[dict]) -> dict:
        return {
            "global_settings": {"seed": 1},
            "tables": [{"name": "t", "row_count": 5, "generate_columns": cols}],
        }

    def test_missing_snapshot_rejected_config_only(self, tmp_path):
        from decoy_engine import run_config_only_checks
        from decoy_engine.plan import PlanCompileError

        cfg = self._cfg(
            [{"name": "amount", "type": "statistical", "snapshot_file": str(tmp_path / "nope.json")}]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "statistical_snapshot_unreadable"

    def test_condition_order_rejected_config_only(self, tmp_path):
        from decoy_engine import run_config_only_checks
        from decoy_engine.plan import PlanCompileError

        snap = _write_snapshot(tmp_path, _source_df(), joints=[("state", "tier")])
        cfg = self._cfg(
            [
                {
                    "name": "tier",
                    "type": "statistical",
                    "snapshot_file": snap,
                    "allow_real_categories": True,
                    "condition_on": "state",
                },
                {
                    "name": "state",
                    "type": "statistical",
                    "snapshot_file": snap,
                    "allow_real_categories": True,
                },
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "statistical_condition_column_unavailable"

    def test_clean_statistical_config_passes(self, tmp_path):
        from decoy_engine import run_config_only_checks

        snap = _write_snapshot(tmp_path, _source_df())
        cfg = self._cfg(
            [{"name": "amount", "type": "statistical", "snapshot_file": snap}]
        )
        assert "statistical_columns" in run_config_only_checks(cfg)
