"""DPS-3: compile-time DP generate contract (Task 5) + consume-only lock (Task 6).

Task 5: `global_settings.dp` hard-rejects the anti-DP generate-column knobs
(`allow_real_categories: true`, `high_cardinality: true`) at compile time.

Real-API reconciliation against the plan's representative sketch:
- No top-level `mode: generate` / `dp:` config key exists (FC-1 dropped
  the `mode` discriminator; per-table kind is inferred from `columns`
  vs `generate_columns`). The dp declaration lives at
  `global_settings.dp` (`config.DpGenerateSettings`), alongside the
  engine's other opt-in pipeline-wide knobs.
- The real compile error type is `PlanCompileError` (code, path,
  message), not a `ConfigError`.
- `compile_plan(config, profile, *, decoy_engine_version)` requires a
  `Profile`, not a bare dict; `run_config_only_checks(config)` is the
  existing profile-free entry point (mirrors
  `tests/unit/generation/test_statistical.py::TestCompileCheck`) and is
  used here instead, since the DP contract check is config-only.

Task 6: `test_generation_consumes_only_the_snapshot` locks that sampling
from a DP-noised snapshot needs no raw source frame -- through the REAL
`load_spec`/`sample_column` API (file-backed snapshot, per
`tests/unit/quality/test_dp.py`'s `TestSamplerConsumption` pattern), not
the plan's invented `load_spec_from_dict`.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from decoy_engine import run_config_only_checks
from decoy_engine.generation.statistical import load_spec, sample_column
from decoy_engine.plan import PlanCompileError
from decoy_engine.plan._checks_dp import check_dp_generate_contract, check_dp_snapshot_provenance
from decoy_engine.quality.dp import apply_dp_noise
from decoy_engine.quality.snapshot import compute_distribution_snapshot


def _dp_cfg(*, table_columns: list[dict]) -> dict:
    return {
        "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
        "tables": [{"name": "t", "row_count": 5, "generate_columns": table_columns}],
    }


class TestCheckDpGenerateContract:
    """Direct unit coverage of check_dp_generate_contract (config-only)."""

    def test_dp_unset_never_raises_even_with_anti_dp_knobs(self):
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "t",
                    "generate_columns": [
                        {
                            "name": "dx",
                            "type": "statistical",
                            "snapshot_file": "whatever.json",
                            "allow_real_categories": True,
                            "high_cardinality": True,
                        }
                    ],
                }
            ],
        }
        check_dp_generate_contract(cfg)  # no raise

    def test_dp_set_without_anti_dp_knobs_passes(self):
        cfg = _dp_cfg(
            table_columns=[{"name": "dx", "type": "statistical", "snapshot_file": "whatever.json"}]
        )
        check_dp_generate_contract(cfg)  # no raise

    def test_dp_rejects_high_cardinality(self):
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "whatever.json",
                    "allow_real_categories": True,
                    "high_cardinality": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_generate_contract(cfg)
        assert exc.value.code == "dp_generate_high_cardinality_unsupported"
        assert "dx" in exc.value.message

    def test_dp_rejects_allow_real_categories(self):
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "whatever.json",
                    "allow_real_categories": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_generate_contract(cfg)
        assert exc.value.code == "dp_generate_allow_real_categories_unsupported"
        assert "dx" in exc.value.message

    def test_non_statistical_generate_column_ignored(self):
        cfg = _dp_cfg(table_columns=[{"name": "id", "type": "sequence"}])
        check_dp_generate_contract(cfg)  # no raise: gate only applies to type: statistical


class TestCompileIntegration:
    """Wired into the real compile chokepoint (config-only branch)."""

    def test_dp_rejects_high_cardinality_via_run_config_only_checks(self):
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "whatever.json",
                    "allow_real_categories": True,
                    "high_cardinality": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_generate_high_cardinality_unsupported"

    def test_dp_rejects_allow_real_categories_via_run_config_only_checks(self):
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "whatever.json",
                    "allow_real_categories": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_generate_allow_real_categories_unsupported"

    def test_dp_contract_check_surfaces_before_snapshot_artifact_check(self):
        # The dp-contract violation is a config-shape verdict independent
        # of whether the referenced snapshot artifact even exists; it
        # must surface on its OWN code, not fall through to
        # statistical_snapshot_unreadable from the missing file.
        cfg = _dp_cfg(
            table_columns=[
                {
                    "name": "dx",
                    "type": "statistical",
                    "snapshot_file": "/nonexistent/path.json",
                    "allow_real_categories": True,
                    "high_cardinality": True,
                }
            ]
        )
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_generate_high_cardinality_unsupported"


# ── Fix 3 (gate remediation, P1 #2): consumed-snapshot DP provenance ───────
#
# `check_dp_generate_contract` (Task 5 above) only rejects two anti-DP
# generate-column KNOBS; it never looks at the snapshot artifact itself.
# An operator can declare `global_settings.dp`, point every statistical
# column at a completely ordinary (non-DP-fit) snapshot, and ship non-DP
# output while the config claims DP. `check_dp_snapshot_provenance` closes
# that: when `dp` is declared, every referenced snapshot must carry a `dp`
# block with a recorded `epsilon_total` (proof `apply_dp_noise` actually
# ran), and every referenced NUMERIC column must show
# `support_origin == "caller"` (proof it was fit with dp_mode +
# numeric_domains -- DPS-1 -- not a data-dependent real min/max range).


def _dp_declared_cfg(*, snapshot_file: str, col_name: str = "age", extra: dict | None = None):
    col = {"name": col_name, "type": "statistical", "snapshot_file": snapshot_file}
    col.update(extra or {})
    return {
        "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
        "tables": [{"name": "t", "row_count": 5, "generate_columns": [col]}],
    }


def _write_snapshot(tmp_path, snap: dict) -> str:
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    return str(path)


class TestCheckDpSnapshotProvenance:
    def test_dp_declared_plain_snapshot_rejected(self, tmp_path):
        df = pd.DataFrame({"age": [31, 42, 55]})
        snap = compute_distribution_snapshot(df)  # ordinary fit: no `dp` block at all
        cfg = _dp_declared_cfg(snapshot_file=_write_snapshot(tmp_path, snap))
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_not_dp_fit"
        assert "age" in exc.value.message

    def test_dp_declared_properly_dp_fit_snapshot_passes(self, tmp_path):
        df = pd.DataFrame({"age": [31, 42, 55]})
        snap = compute_distribution_snapshot(
            df, dp_mode=True, numeric_domains={"age": (0.0, 120.0)}
        )
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        cfg = _dp_declared_cfg(snapshot_file=_write_snapshot(tmp_path, noisy))
        check_dp_snapshot_provenance(cfg)  # no raise

    def test_dp_declared_non_dp_fit_numeric_rejected_despite_dp_block(self, tmp_path):
        # The `dp` block IS present (apply_dp_noise ran), but the numeric
        # column was fit WITHOUT dp_mode -- bin edges are the real
        # min/max, so support_origin stays "data" and the release is not
        # actually DP despite the block's presence. A wrong DP guarantee
        # is worse than none, so this must still be rejected.
        df = pd.DataFrame({"age": [31, 42, 55]})
        snap = compute_distribution_snapshot(df)  # no dp_mode
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        cfg = _dp_declared_cfg(snapshot_file=_write_snapshot(tmp_path, noisy))
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_numeric_support_data_dependent"
        assert "age" in exc.value.message

    def test_dp_unset_plain_snapshot_passes(self, tmp_path):
        df = pd.DataFrame({"age": [31, 42, 55]})
        snap = compute_distribution_snapshot(df)
        cfg = {
            "global_settings": {"seed": 1},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {
                            "name": "age",
                            "type": "statistical",
                            "snapshot_file": _write_snapshot(tmp_path, snap),
                        }
                    ],
                }
            ],
        }
        check_dp_snapshot_provenance(cfg)  # no raise: no dp declared, no gate

    def test_dp_declared_categorical_dp_mode_fit_passes(self, tmp_path):
        # A categorical column fit WITH dp_mode carries
        # support_origin="full_vocabulary" (Fix 7), so its candidacy is
        # data-independent (Fix 1) and the check passes.
        rng = np.random.default_rng(3)
        df = pd.DataFrame({"state": rng.choice(["CA", "NY", "TX"], size=100)})
        snap = compute_distribution_snapshot(df, dp_mode=True)
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, noisy),
            col_name="state",
            extra={"allow_real_categories": True},
        )
        check_dp_snapshot_provenance(cfg)  # no raise

    def test_dp_declared_categorical_without_dp_mode_rejected(self, tmp_path):
        # Fix 7 (closes the Fix 3 residual): an ALL-CATEGORICAL dp-declared
        # pipeline whose column was fit WITHOUT dp_mode -- ordinary top-K
        # truncation, so candidacy is data-dependent (non-DP) -- previously
        # slipped through because the `dp` block was present (apply_dp_noise
        # ran) and there was no numeric column to trip the numeric check.
        # It must now be rejected: no full_vocabulary marker.
        rng = np.random.default_rng(3)
        df = pd.DataFrame({"state": rng.choice(["CA", "NY", "TX"], size=100)})
        snap = compute_distribution_snapshot(df)  # NO dp_mode -> top-K truncation
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, noisy),
            col_name="state",
            extra={"allow_real_categories": True},
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_categorical_candidacy_data_dependent"
        assert "state" in exc.value.message

    def test_dp_declared_mixed_numeric_and_categorical_dp_mode_fit_passes(self, tmp_path):
        rng = np.random.default_rng(3)
        df = pd.DataFrame(
            {"age": [31, 42, 55, 27, 61], "state": rng.choice(["CA", "NY", "TX"], size=5)}
        )
        snap = compute_distribution_snapshot(
            df, dp_mode=True, numeric_domains={"age": (0.0, 120.0)}
        )
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        cfg = {
            "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {
                            "name": "age",
                            "type": "statistical",
                            "snapshot_file": _write_snapshot(tmp_path, noisy),
                        },
                        {
                            "name": "state",
                            "type": "statistical",
                            "snapshot_file": _write_snapshot(tmp_path, noisy),
                            "allow_real_categories": True,
                        },
                    ],
                }
            ],
        }
        check_dp_snapshot_provenance(cfg)  # no raise: both provenances satisfied

    def test_wired_into_run_config_only_checks(self, tmp_path):
        # The real chokepoint used by `decoy validate`, not just direct
        # unit coverage of the check function.
        df = pd.DataFrame({"age": [31, 42, 55]})
        snap = compute_distribution_snapshot(df)  # no dp block
        cfg = _dp_declared_cfg(snapshot_file=_write_snapshot(tmp_path, snap))
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_snapshot_not_dp_fit"

    def test_all_categorical_hole_closed_is_a_self_contained_verdict(self, tmp_path):
        # The Fix 3 residual was that check_dp_snapshot_provenance ITSELF
        # returned cleanly on an all-categorical, non-dp_mode-fit snapshot.
        # It now raises on its own -- a self-contained verdict, not relying
        # on an earlier check's ordering. (Through the full run_config_only_
        # checks chain a categorical column additionally trips the
        # allow_real_categories anti-DP gate first; this check must stand
        # correct-by-construction regardless of that ordering.)
        rng = np.random.default_rng(3)
        df = pd.DataFrame({"state": rng.choice(["CA", "NY", "TX"], size=100)})
        snap = compute_distribution_snapshot(df)  # NO dp_mode
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        # No allow_real_categories here -> reaches the provenance check
        # without the anti-DP-knob gate masking it.
        cfg = _dp_declared_cfg(snapshot_file=_write_snapshot(tmp_path, noisy), col_name="state")
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_categorical_candidacy_data_dependent"


# ── Task 6: consume-only contract lock ──────────────────────────────────────


def _source_df() -> pd.DataFrame:
    rng = np.random.default_rng(5)
    n = 300
    return pd.DataFrame({"state": rng.choice(["CA", "NY", "TX"], size=n, p=[0.5, 0.3, 0.2])})


def test_generation_consumes_only_the_snapshot(tmp_path):
    """Sampling from a DP snapshot must not require or read the raw source
    frame. Contract lock for post-processing immunity (DPS-3).

    Built through the REAL fit -> DP-noise -> load_spec -> sample_column
    pipeline (not the plan's invented load_spec_from_dict/StatisticalSpec
    construction): a genuine DP'd snapshot is written to disk, and only
    that file (never `_source_df()`) is touched from here on.
    """
    snap = compute_distribution_snapshot(_source_df())
    noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(7))
    path = tmp_path / "noisy.json"
    path.write_text(json.dumps(noisy), encoding="utf-8")

    spec = load_spec(
        {
            "name": "state",
            "type": "statistical",
            "snapshot_file": str(path),
            "allow_real_categories": True,
        }
    )
    out = sample_column(spec, 100, col_seed=42)
    assert len(out) == 100
    # Only labels present in the (threshold-released) artifact -- CA/NY/TX
    # all comfortably clear tau at n=300, so no "other"/suppression here.
    assert set(out) <= {"CA", "NY", "TX"}
