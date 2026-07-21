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
from decoy_engine.plan._checks_dp import (
    check_dp_categorical_unsupported,
    check_dp_generate_contract,
    check_dp_snapshot_provenance,
)
from decoy_engine.quality.dp import apply_dp_noise
from decoy_engine.quality.snapshot import (
    DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
    compute_distribution_snapshot,
)


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


# ── Option A (2026-07-21 DPS remediation): categorical not yet supported ───
#
# Before this check, a categorical `type: statistical` column under
# `global_settings.dp` fell into a two-error deadlock: without
# `allow_real_categories`, `generation.statistical.load_spec` raised
# `statistical_real_categories_not_allowed` (implying the fix is to add the
# flag); WITH the flag, `check_dp_generate_contract` raised
# `dp_generate_allow_real_categories_unsupported` (implying the fix is to
# remove it). No config compiled either way, and neither message named the
# real reason (categorical DP is not yet implemented correctly).
# `check_dp_categorical_unsupported` runs first and gives ONE clear code
# regardless of the flag.


def _categorical_snapshot_cfg(tmp_path, *, allow_real_categories: bool | None = None) -> dict:
    df = pd.DataFrame({"state": ["CA", "NY", "CA", "TX", "NY"]})
    snap = compute_distribution_snapshot(df)  # ordinary fit: kind == "categorical"
    col: dict = {
        "name": "state",
        "type": "statistical",
        "snapshot_file": _write_snapshot(tmp_path, snap),
    }
    if allow_real_categories is not None:
        col["allow_real_categories"] = allow_real_categories
    return {
        "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
        "tables": [{"name": "t", "row_count": 5, "generate_columns": [col]}],
    }


class TestCheckDpCategoricalUnsupported:
    """Direct unit coverage of check_dp_categorical_unsupported."""

    def test_dp_unset_never_raises_even_for_a_categorical_column(self, tmp_path):
        cfg = _categorical_snapshot_cfg(tmp_path)
        cfg["global_settings"] = {"seed": 1}  # no dp declared
        check_dp_categorical_unsupported(cfg)  # no raise

    def test_dp_set_categorical_without_allow_real_categories_rejected(self, tmp_path):
        cfg = _categorical_snapshot_cfg(tmp_path)  # flag not set at all
        with pytest.raises(PlanCompileError) as exc:
            check_dp_categorical_unsupported(cfg)
        assert exc.value.code == "dp_categorical_not_yet_supported"
        assert "state" in exc.value.message

    def test_dp_set_categorical_with_allow_real_categories_still_rejected(self, tmp_path):
        # The deadlock-closing case: setting the flag does NOT unlock
        # anything -- categorical is rejected either way, with the SAME code.
        cfg = _categorical_snapshot_cfg(tmp_path, allow_real_categories=True)
        with pytest.raises(PlanCompileError) as exc:
            check_dp_categorical_unsupported(cfg)
        assert exc.value.code == "dp_categorical_not_yet_supported"

    def test_dp_set_numeric_column_not_rejected(self, tmp_path):
        df = pd.DataFrame({"age": [31, 42, 55]})
        snap = compute_distribution_snapshot(df)
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
                            "snapshot_file": _write_snapshot(tmp_path, snap),
                        }
                    ],
                }
            ],
        }
        check_dp_categorical_unsupported(cfg)  # no raise: not categorical

    def test_non_statistical_generate_column_ignored(self):
        cfg = _dp_cfg(table_columns=[{"name": "id", "type": "sequence"}])
        check_dp_categorical_unsupported(cfg)  # no raise

    def test_unreadable_snapshot_deferred_to_check_statistical_columns(self):
        cfg = _dp_cfg(
            table_columns=[
                {"name": "dx", "type": "statistical", "snapshot_file": "/nonexistent/path.json"}
            ]
        )
        check_dp_categorical_unsupported(cfg)  # no raise: defers to row 12's verdict


class TestCheckDpCategoricalUnsupportedIntegration:
    """Wired into the real compile chokepoint: proves the deadlock is
    closed end-to-end, not just at the unit level."""

    def test_categorical_without_flag_gets_the_clear_code_not_the_consent_gate(self, tmp_path):
        cfg = _categorical_snapshot_cfg(tmp_path)
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_categorical_not_yet_supported"
        assert exc.value.code != "statistical_real_categories_not_allowed"

    def test_categorical_with_flag_still_gets_the_clear_code_not_the_knob_rejection(self, tmp_path):
        # This is the exact "add the flag" move a user would try after
        # reading the without-flag error above -- it must NOT flip to
        # dp_generate_allow_real_categories_unsupported (the old deadlock).
        cfg = _categorical_snapshot_cfg(tmp_path, allow_real_categories=True)
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_categorical_not_yet_supported"
        assert exc.value.code != "dp_generate_allow_real_categories_unsupported"


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


def _dp_declared_cfg(
    *,
    snapshot_file: str,
    col_name: str = "age",
    extra: dict | None = None,
    declared_epsilon: float = 1.0,
    declared_delta: float = 1e-6,
):
    col = {"name": col_name, "type": "statistical", "snapshot_file": snapshot_file}
    col.update(extra or {})
    return {
        "global_settings": {
            "seed": 1,
            "dp": {"epsilon": declared_epsilon, "delta": declared_delta},
        },
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
        # Finding 4 (2026-07-21): the declared ceiling must cover the
        # snapshot's COMPOSED spend, not the per-release epsilon passed to
        # apply_dp_noise -- a single numeric column charges row_count +
        # null_count + non_null_count + distinct_count + histogram
        # separately (5 releases @ epsilon=1.0 = epsilon_total 5.0, per
        # dp.py's module docstring), so declaring epsilon=1.0 here would
        # now correctly trip dp_budget_exceeded. Declare comfortably above
        # the actual spend so this test proves provenance, not budget.
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, noisy), declared_epsilon=10.0
        )
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

    def test_dp_declared_categorical_dp_mode_fit_rejected_option_a(self, tmp_path):
        # Option A (2026-07-21): even a categorical column carrying the
        # dp_mode full-vocabulary marker is rejected -- the RELEASE
        # mechanism (apply_dp_noise's stable-histogram branch) does not
        # satisfy its stated (epsilon, delta) bound regardless of fit-side
        # candidacy. `compute_distribution_snapshot(df, dp_mode=True)` can
        # no longer even PRODUCE this artifact live for an object column
        # (it now fails closed at fit time,
        # dp_mode_categorical_unsupported, tests/unit/generation/
        # test_snapshot_dp_support.py) -- the marker is stamped directly
        # here to prove the CONSUME-side gate still rejects an artifact
        # carrying it (e.g. one fit by a pre-Option-A engine version, or
        # hand-edited), not just that the fit path is closed.
        rng = np.random.default_rng(3)
        df = pd.DataFrame({"state": rng.choice(["CA", "NY", "TX"], size=100)})
        snap = compute_distribution_snapshot(df)  # ordinary (non-dp_mode) fit
        snap["columns"]["state"]["support_origin"] = "full_vocabulary"
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, noisy),
            col_name="state",
            extra={"allow_real_categories": True},
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_categorical_not_yet_supported"
        assert "state" in exc.value.message

    def test_dp_declared_categorical_without_dp_mode_rejected(self, tmp_path):
        # Option A: an ALL-CATEGORICAL dp-declared pipeline is rejected
        # regardless of whether the column was fit with dp_mode -- the same
        # blanket dp_categorical_not_yet_supported code as the dp_mode-fit
        # case above, since categorical DP is not supported at all yet.
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
        assert exc.value.code == "dp_categorical_not_yet_supported"
        assert "state" in exc.value.message

    def test_dp_declared_mixed_numeric_and_categorical_dp_mode_fit_rejected_option_a(
        self, tmp_path
    ):
        # Option A: a pipeline with BOTH a sound numeric DP column and a
        # categorical column under the same global_settings.dp is rejected
        # on the categorical column -- there is no partial-DP pipeline; the
        # whole dp-declared config must compile or not.
        rng = np.random.default_rng(3)
        df = pd.DataFrame(
            {"age": [31, 42, 55, 27, 61], "state": rng.choice(["CA", "NY", "TX"], size=5)}
        )
        # dp_mode=True can no longer fit this frame at all (the categorical
        # "state" column fails closed at fit time, aborting before "age" is
        # even reached) -- fit WITHOUT dp_mode (numeric_domains alone still
        # stamps age's support_origin="caller") and hand-stamp state's
        # full_vocabulary marker to simulate the mixed artifact a
        # pre-Option-A engine would have produced.
        snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)})
        snap["columns"]["state"]["support_origin"] = "full_vocabulary"
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        cfg = {
            "global_settings": {"seed": 1, "dp": {"epsilon": 10.0, "delta": 1e-6}},
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
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_categorical_not_yet_supported"
        assert "state" in exc.value.message

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
        # check_dp_snapshot_provenance rejects an all-categorical snapshot
        # on its own -- a self-contained verdict, not relying on
        # check_dp_categorical_unsupported having run first (through the
        # full compile_plan/run_config_only_checks chain, THAT check fires
        # first and is what a real caller sees; this proves the SIBLING
        # check stands correct-by-construction regardless of ordering, per
        # this module's established convention for the anti-DP-knob check).
        rng = np.random.default_rng(3)
        df = pd.DataFrame({"state": rng.choice(["CA", "NY", "TX"], size=100)})
        snap = compute_distribution_snapshot(df)  # NO dp_mode
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        # No allow_real_categories here -> reaches the provenance check
        # without the anti-DP-knob gate masking it.
        cfg = _dp_declared_cfg(snapshot_file=_write_snapshot(tmp_path, noisy), col_name="state")
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_categorical_not_yet_supported"


class TestDpSnapshotProvenanceAllowList:
    """PoC-encoding regression tests for the fail-closed allow-list.

    The consume-side check previously only RAISED for numeric-not-'caller'
    and categorical-not-'full_vocabulary'; datetime/freetext/empty kinds
    fell through and PASSED. Because `apply_dp_noise` noises ANY snapshot
    (datetime year_bins, freetext length bins included), an operator could
    fit WITHOUT dp_mode, run apply_dp_noise (adds a `dp` block +
    epsilon_total), declare `global_settings.dp`, and ship a non-DP
    datetime/freetext support (e.g. an outlier admission year) under a DP
    declaration. Fix 2's FIT-TIME rejection does not cover this -- the
    snapshot was never dp_mode-fit. The allow-list default-rejects every
    kind that is not a proven-data-independent numeric or categorical."""

    def _noised(self, tmp_path, df: pd.DataFrame) -> str:
        # Fit WITHOUT dp_mode (data-dependent support, no support_origin),
        # then run apply_dp_noise so a `dp` block + epsilon_total exist.
        snap = compute_distribution_snapshot(df)
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        return _write_snapshot(tmp_path, noisy)

    def test_datetime_consume_side_bypass_now_rejected(self, tmp_path):
        df = pd.DataFrame(
            {"joined": pd.to_datetime(["1931-01-01", "2020-06-15", "2021-12-31", "2022-03-03"])}
        )
        cfg = _dp_declared_cfg(snapshot_file=self._noised(tmp_path, df), col_name="joined")
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_kind_not_dp_eligible"
        assert "joined" in exc.value.message

    def test_freetext_consume_side_bypass_now_rejected(self, tmp_path):
        df = pd.DataFrame(
            {"note": [f"free text clinical note number {i} with distinct words" for i in range(40)]}
        )
        cfg = _dp_declared_cfg(snapshot_file=self._noised(tmp_path, df), col_name="note")
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_kind_not_dp_eligible"
        assert "note" in exc.value.message

    def test_empty_kind_column_rejected_fail_closed(self, tmp_path):
        # `empty` (all-null) releases stats={} and is harmless today, but
        # must ride the same fail-closed default so a future change cannot
        # reopen the hole.
        df = pd.DataFrame({"blank": [None, None, None]})
        snap = compute_distribution_snapshot(df)
        assert snap["columns"]["blank"]["kind"] == "empty"
        cfg = _dp_declared_cfg(snapshot_file=self._noised(tmp_path, df), col_name="blank")
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_kind_not_dp_eligible"

    def test_datetime_bypass_closed_end_to_end_via_run_config_only_checks(self, tmp_path):
        # datetime is a load_spec-supported kind that needs no
        # allow_real_categories, so it genuinely reaches the provenance
        # check through the real `decoy validate` chain -- the true PoC.
        df = pd.DataFrame(
            {"joined": pd.to_datetime(["1931-01-01", "2020-06-15", "2021-12-31", "2022-03-03"])}
        )
        cfg = _dp_declared_cfg(snapshot_file=self._noised(tmp_path, df), col_name="joined")
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_snapshot_kind_not_dp_eligible"

    def test_freetext_bypass_closed_end_to_end_via_run_config_only_checks(self, tmp_path):
        df = pd.DataFrame(
            {"note": [f"free text clinical note number {i} with distinct words" for i in range(40)]}
        )
        cfg = _dp_declared_cfg(snapshot_file=self._noised(tmp_path, df), col_name="note")
        with pytest.raises(PlanCompileError) as exc:
            run_config_only_checks(cfg)
        assert exc.value.code == "dp_snapshot_kind_not_dp_eligible"


# ── Finding 4 (2026-07-21): declared (epsilon, delta) budget enforcement ───
#
# `global_settings.dp` previously only checked `epsilon_total` was present
# and numeric; `delta_total` was never validated, and the DECLARED
# epsilon/delta were never compared against what the artifacts actually
# spent. A config declaring epsilon=1e-6 would accept an artifact whose real
# epsilon_total was 5. These five cases are the plan's exact enumeration.


def _numeric_dp_artifact(*, epsilon_total: object, delta_total: object = 0.0) -> dict:
    """A minimal, schema-valid, PROVENANCE-ELIGIBLE numeric DP artifact with
    an EXACTLY controlled `dp.epsilon_total`/`delta_total` -- hand-built
    rather than routed through `apply_dp_noise` (whose composed total is a
    function of how many scalars a real fit charges, not a single knob) so
    the budget-composition arithmetic below is exact and legible. `object`
    typing on the two dp fields lets the malformed-input tests pass
    non-numeric/None/NaN values through untouched.
    """
    return {
        "schema_version": DISTRIBUTION_SNAPSHOT_SCHEMA_VERSION,
        "row_count": 5,
        "columns": {
            "age": {
                "dtype": "int64",
                "kind": "numeric",
                "null_count": 0,
                "non_null_count": 5,
                "distinct_count": 5,
                "support_origin": "caller",
                "stats": {"bin_edges": [0.0, 120.0], "bin_counts": [5]},
            }
        },
        "joints": [],
        "dp": {"epsilon_total": epsilon_total, "delta_total": delta_total},
    }


class TestFinding4BudgetEnforcement:
    def test_i_declared_budget_below_artifact_spend_rejected(self, tmp_path):
        # Codex's repro shape: declared epsilon=1e-6, artifact epsilon_total=5.
        artifact = _numeric_dp_artifact(epsilon_total=5.0)
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, artifact), declared_epsilon=1e-6
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_budget_exceeded"
        assert "5.0" in exc.value.message
        assert "1e-06" in exc.value.message or "1e-6" in exc.value.message.replace("1e-06", "1e-6")

    def test_ii_declared_budget_exactly_equal_to_spend_passes(self, tmp_path):
        # Boundary: spend <= declared passes; equality is the boundary case.
        artifact = _numeric_dp_artifact(epsilon_total=2.5)
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, artifact), declared_epsilon=2.5
        )
        check_dp_snapshot_provenance(cfg)  # no raise

    def test_iii_two_distinct_artifacts_compose_and_can_exceed_declared_budget(self, tmp_path):
        # Two DIFFERENT artifacts (distinguishing distinct_count so the
        # bytes genuinely differ -- a real content-hash miss, not a
        # collision) each spend 0.6; declared 1.0 cannot cover 0.6 + 0.6.
        artifact_a = _numeric_dp_artifact(epsilon_total=0.6)
        artifact_b = _numeric_dp_artifact(epsilon_total=0.6)
        artifact_b["columns"]["age"]["distinct_count"] = 6
        path_a = _write_snapshot(tmp_path, artifact_a)
        path_b = tmp_path / "snap_b.json"
        path_b.write_text(json.dumps(artifact_b), encoding="utf-8")
        cfg = {
            "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {
                            "name": "age_a",
                            "type": "statistical",
                            "snapshot_file": path_a,
                            "source_column": "age",
                        },
                        {
                            "name": "age_b",
                            "type": "statistical",
                            "snapshot_file": str(path_b),
                            "source_column": "age",
                        },
                    ],
                }
            ],
        }
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_budget_exceeded"

    def test_iii_same_artifact_referenced_by_ten_columns_charged_once(self, tmp_path):
        # The SAME artifact (one content hash) consumed by 10 columns is
        # ONE DP release, charged once -- 0.6 <= declared 1.0 passes, where
        # naively summing 10x0.6 would have wrongly rejected it.
        artifact = _numeric_dp_artifact(epsilon_total=0.6)
        path = _write_snapshot(tmp_path, artifact)
        cfg = {
            "global_settings": {"seed": 1, "dp": {"epsilon": 1.0, "delta": 1e-6}},
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {
                            "name": f"age_{i}",
                            "type": "statistical",
                            "snapshot_file": path,
                            "source_column": "age",
                        }
                        for i in range(10)
                    ],
                }
            ],
        }
        check_dp_snapshot_provenance(cfg)  # no raise: charged once, not 10x

    def test_iv_delta_total_absent_rejected_fail_closed(self, tmp_path):
        # `apply_dp_noise` ALWAYS writes delta_total (even 0.0); an absent
        # key only happens on a tampered/pre-DPS-2 artifact and must not be
        # silently treated as 0.0.
        artifact = _numeric_dp_artifact(epsilon_total=1.0)
        del artifact["dp"]["delta_total"]
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, artifact), declared_epsilon=10.0
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_budget_malformed"

    def test_iv_delta_total_nan_rejected_fail_closed(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=1.0, delta_total=float("nan"))
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, artifact), declared_epsilon=10.0
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_budget_malformed"

    def test_iv_delta_total_negative_rejected_fail_closed(self, tmp_path):
        artifact = _numeric_dp_artifact(epsilon_total=1.0, delta_total=-0.1)
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, artifact), declared_epsilon=10.0
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_snapshot_budget_malformed"

    def test_v_delta_path_artifact_delta_above_declared_delta_rejected(self, tmp_path):
        # The delta AXIS of the comparison, exercised independently of
        # epsilon. Option A ships numeric-only DP, and a genuine numeric
        # fit always spends delta_total=0.0 (no threshold-release
        # mechanism runs), so this is a hand-crafted artifact -- exactly
        # the shape the plan's Finding 4(d)(v) case describes, adapted to
        # Option A's scope (the plan's literal case used a categorical
        # artifact, which Option A rejects before budget accounting is
        # ever reached; delta_total > 0 is otherwise only reachable via a
        # categorical release, so this is the faithful in-scope
        # equivalent: prove the delta comparison is real, not just epsilon).
        artifact = _numeric_dp_artifact(epsilon_total=0.1, delta_total=0.01)
        cfg = _dp_declared_cfg(
            snapshot_file=_write_snapshot(tmp_path, artifact),
            declared_epsilon=10.0,
            declared_delta=1e-6,
        )
        with pytest.raises(PlanCompileError) as exc:
            check_dp_snapshot_provenance(cfg)
        assert exc.value.code == "dp_budget_exceeded"

    def test_declared_dp_without_epsilon_skips_budget_enforcement_gracefully(self, tmp_path):
        # Defensive path: these checks run on a raw, not-yet-pydantic-
        # validated config dict. A malformed DECLARATION (missing epsilon)
        # must not crash the walk; the budget comparison is simply skipped
        # (schema validation elsewhere owns rejecting a malformed
        # declaration) while provenance checks still run normally.
        artifact = _numeric_dp_artifact(epsilon_total=5.0)
        cfg = {
            "global_settings": {"seed": 1, "dp": {"delta": 1e-6}},  # epsilon missing
            "tables": [
                {
                    "name": "t",
                    "row_count": 5,
                    "generate_columns": [
                        {
                            "name": "age",
                            "type": "statistical",
                            "snapshot_file": _write_snapshot(tmp_path, artifact),
                        }
                    ],
                }
            ],
        }
        check_dp_snapshot_provenance(cfg)  # no raise: provenance is fine, budget skipped


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
