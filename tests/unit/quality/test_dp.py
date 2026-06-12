"""Differentially private snapshot release (deferred follow-up 3, 2026-06-12).

`apply_dp_noise` adds per-count Laplace noise (Dwork et al. 2006;
OpenDP/SmartNoise histogram release pattern) to a distribution
snapshot, removes exact moments, and widens min/max to edge resolution.
The artifact stays distribution-snapshot/v1 (additive `dp` block) and
the seeded samplers consume it unchanged, so generation from a FIXED
noisy snapshot is still deterministic.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from decoy_engine.generation.statistical import StatisticalSpecError, load_spec, sample_column
from decoy_engine.quality.dp import DpError, apply_dp_noise
from decoy_engine.quality.snapshot import compute_distribution_snapshot


def _source_df() -> pd.DataFrame:
    rng = np.random.default_rng(5)
    n = 500
    return pd.DataFrame(
        {
            "amount": rng.normal(100.0, 25.0, size=n).round(2),
            "state": rng.choice(["CA", "NY", "TX"], size=n, p=[0.5, 0.3, 0.2]),
            "joined": pd.to_datetime("2020-01-01")
            + pd.to_timedelta(rng.integers(0, 365 * 4, size=n), unit="D"),
            "comment": [f"row comment number {i} with some text" * (1 + i % 3) for i in range(n)],
        }
    )


def _snapshot() -> dict:
    return compute_distribution_snapshot(_source_df())


class TestNoiseApplication:
    def test_counts_noised_nonnegative_ints_and_input_unmutated(self):
        snap = _snapshot()
        before = json.dumps(snap, sort_keys=True)
        noisy = apply_dp_noise(snap, epsilon=0.5, rng=np.random.default_rng(0))
        assert json.dumps(snap, sort_keys=True) == before  # input untouched

        num = noisy["columns"]["amount"]["stats"]
        assert num["bin_counts"] != snap["columns"]["amount"]["stats"]["bin_counts"]
        for col in noisy["columns"].values():
            stats = col.get("stats") or {}
            for c in stats.get("bin_counts") or []:
                assert isinstance(c, int) and c >= 0
            for item in stats.get("top_values") or []:
                assert isinstance(item["count"], int) and item["count"] >= 0
            for item in stats.get("year_bins") or []:
                assert isinstance(item["count"], int) and item["count"] >= 0
            for c in stats.get("length_bin_counts") or []:
                assert isinstance(c, int) and c >= 0
        assert isinstance(noisy["row_count"], int) and noisy["row_count"] >= 0

    def test_exact_moments_removed_and_minmax_widened(self):
        snap = _snapshot()
        noisy = apply_dp_noise(snap, epsilon=1.0, rng=np.random.default_rng(1))
        num = noisy["columns"]["amount"]["stats"]
        assert num["quantiles"] == {}
        assert num["mean"] is None and num["std"] is None
        assert num["min"] == num["bin_edges"][0]
        assert num["max"] == num["bin_edges"][-1]
        dt = noisy["columns"]["joined"]["stats"]
        assert dt["min"].endswith("-01-01T00:00:00")
        assert dt["max"].endswith("-12-31T23:59:59")
        ft = noisy["columns"]["comment"]["stats"]
        assert ft["length"]["mean"] is None and ft["length"]["std"] is None

    def test_tiny_epsilon_clamps_to_zero_never_negative(self):
        noisy = apply_dp_noise(_snapshot(), epsilon=1e-3, rng=np.random.default_rng(2))
        counts = noisy["columns"]["amount"]["stats"]["bin_counts"]
        assert all(c >= 0 for c in counts)
        assert 0 in counts  # scale 1000 noise must zero something

    def test_dp_metadata_block(self):
        noisy = apply_dp_noise(_snapshot(), epsilon=2.0, rng=np.random.default_rng(3))
        assert noisy["dp"] == {
            "epsilon": 2.0,
            "mechanism": "laplace",
            "sensitivity": 1,
            "adjacency": "add-remove-one-row",
            "scope": "per-column-histogram",
        }
        # Schema unchanged: v1-additive.
        assert noisy["schema_version"] == _snapshot()["schema_version"]


class TestValidation:
    @pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), "abc"])
    def test_invalid_epsilon_rejected(self, bad):
        with pytest.raises(DpError) as exc:
            apply_dp_noise(_snapshot(), epsilon=bad)
        assert exc.value.code == "dp_epsilon_invalid"

    def test_joints_rejected(self):
        snap = compute_distribution_snapshot(_source_df(), joint_columns=[("state", "comment")])
        if not snap["joints"]:  # comment is freetext; use a real categorical pair
            df = _source_df()
            df["tier"] = ["gold" if s == "CA" else "bronze" for s in df["state"]]
            snap = compute_distribution_snapshot(df, joint_columns=[("state", "tier")])
        assert snap["joints"]
        with pytest.raises(DpError) as exc:
            apply_dp_noise(snap, epsilon=1.0)
        assert exc.value.code == "dp_joint_unsupported"


class TestSamplerConsumption:
    def _noisy_path(self, tmp_path, epsilon=1.0):
        noisy = apply_dp_noise(_snapshot(), epsilon=epsilon, rng=np.random.default_rng(7))
        path = tmp_path / "noisy.json"
        path.write_text(json.dumps(noisy), encoding="utf-8")
        return str(path)

    def test_noisy_snapshot_roundtrips_load_spec_for_all_kinds(self, tmp_path):
        path = self._noisy_path(tmp_path)
        for name, extra in (
            ("amount", {}),
            ("state", {"allow_real_categories": True}),
            ("joined", {}),
        ):
            spec = load_spec({"name": name, "type": "statistical", "snapshot_file": path, **extra})
            assert spec.kind in ("numeric", "categorical", "datetime")

    def test_generation_from_fixed_noisy_snapshot_is_deterministic(self, tmp_path):
        path = self._noisy_path(tmp_path)
        spec = load_spec({"name": "amount", "type": "statistical", "snapshot_file": path})
        a = sample_column(spec, 300, col_seed=99)
        b = sample_column(spec, 300, col_seed=99)
        assert a == b
        cat = load_spec(
            {
                "name": "state",
                "type": "statistical",
                "snapshot_file": path,
                "allow_real_categories": True,
            }
        )
        assert sample_column(cat, 300, col_seed=99) == sample_column(cat, 300, col_seed=99)

    def test_all_zero_weights_raise_degenerate(self, tmp_path):
        noisy = apply_dp_noise(_snapshot(), epsilon=1.0, rng=np.random.default_rng(7))
        for item in noisy["columns"]["state"]["stats"]["top_values"]:
            item["count"] = 0
        noisy["columns"]["state"]["stats"]["other_count"] = 0
        for item in noisy["columns"]["joined"]["stats"]["year_bins"]:
            item["count"] = 0
        path = tmp_path / "zeroed.json"
        path.write_text(json.dumps(noisy), encoding="utf-8")

        cat = load_spec(
            {
                "name": "state",
                "type": "statistical",
                "snapshot_file": str(path),
                "allow_real_categories": True,
            }
        )
        with pytest.raises(StatisticalSpecError) as exc:
            sample_column(cat, 10, col_seed=1)
        assert exc.value.code == "statistical_stats_degenerate"

        dt = load_spec({"name": "joined", "type": "statistical", "snapshot_file": str(path)})
        with pytest.raises(StatisticalSpecError) as exc:
            sample_column(dt, 10, col_seed=1)
        assert exc.value.code == "statistical_stats_degenerate"
