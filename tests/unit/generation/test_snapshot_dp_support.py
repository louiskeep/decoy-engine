"""DPS-1: data-independent numeric bin ranges (`numeric_domains` / `dp_mode`).

Real module location note: the plan cites `generation/snapshot.py`; the
actual fit-time snapshot module is `decoy_engine.quality.snapshot`
(`generation/statistical` only CONSUMES the artifact). Reconciled against
`src/decoy_engine/quality/snapshot.py:114` (compute_distribution_snapshot)
and `:373` (_numeric_stats).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from decoy_engine.quality.snapshot import compute_distribution_snapshot


def test_numeric_domain_overrides_data_min_max():
    df = pd.DataFrame({"age": [31, 42, 55]})  # data range 31..55
    snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)}, dp_mode=True)
    stats = snap["columns"]["age"]["stats"]
    assert stats["bin_edges"][0] == 0.0
    assert stats["bin_edges"][-1] == 120.0
    assert snap["columns"]["age"]["support_origin"] == "caller"


def test_dp_mode_requires_domain_for_numeric():
    df = pd.DataFrame({"age": [31, 42, 55]})
    with pytest.raises(ValueError, match="numeric_domain"):
        compute_distribution_snapshot(df, dp_mode=True)  # no domain -> fail closed


def test_plain_call_omits_support_origin_key_entirely():
    # Byte-identical-for-existing-callers contract (golden digest test,
    # tests/snapshots/test_distribution_snapshot_baseline.py): a caller
    # that never touches numeric_domains/dp_mode gets the exact prior
    # dict shape, not just the same values -- so the key is absent, not
    # merely "data".
    df = pd.DataFrame({"age": [31, 42, 55]})
    snap = compute_distribution_snapshot(df)
    assert "support_origin" not in snap["columns"]["age"]
    assert snap["columns"]["age"]["stats"]["bin_edges"][0] == 31.0
    assert snap["columns"]["age"]["stats"]["bin_edges"][-1] == 55.0


def test_numeric_domains_without_dp_mode_still_reports_support_origin():
    # numeric_domains alone (without dp_mode) is a legitimate non-DP use
    # (e.g. a caller wants a fixed comparable bin range across two
    # snapshots) and must still surface provenance once it's supplied.
    df = pd.DataFrame({"age": [31, 42, 55]})
    snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)})
    assert snap["columns"]["age"]["support_origin"] == "caller"


def test_domain_clamps_out_of_domain_values_instead_of_dropping_them():
    # A value outside the caller's declared domain must not silently vanish
    # from bin_counts (numpy.histogram's default range behavior would drop
    # it, undercounting row_count -- a data-integrity bug for a privacy
    # feature). It is clamped into the nearest edge bin instead.
    df = pd.DataFrame({"age": [5, 50, 500]})
    snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)}, dp_mode=True)
    stats = snap["columns"]["age"]["stats"]
    assert sum(stats["bin_counts"]) == 3


def test_dp_mode_does_not_require_domain_for_non_numeric_columns():
    # dp_mode's fail-closed precondition is scoped to numeric bin ranges
    # (the only support this task makes data-independent); categorical
    # label-set independence is a separate mechanism (Task 3, dp.py).
    df = pd.DataFrame({"age": [31, 42, 55], "state": ["CA", "NY", "CA"]})
    snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)}, dp_mode=True)
    assert snap["columns"]["state"]["kind"] == "categorical"


# ── Fix 1 (gate remediation, P0): categorical candidate set must be the ──
# ── FULL observed vocabulary under dp_mode, not a top-K-by-true-count ────
# ── truncation (top-K SELECTION is data-dependent; delta can't absorb ────
# ── a boundary label's release membership flipping between neighbors). ───


class TestDpModeCategoricalFullVocabularyCandidacy:
    """A label ranked below the top-K by TRUE count must still reach
    `apply_dp_noise` as a threshold *candidate* under dp_mode -- only its
    noised count vs tau decides release, never its rank. This is DISTINCT
    from the `high_cardinality` full-vocabulary-no-threshold path (still
    forbidden alongside `dp`, see plan._checks_dp)."""

    def _many_category_df(self) -> pd.DataFrame:
        # 25 distinct labels, strictly descending true count: the default
        # categorical_top_k=20 truncates ranks 21-25 (val20..val24) into
        # other_count.
        values: list[str] = []
        for i in range(25):
            values += [f"val{i}"] * (100 - i * 3)
        return pd.DataFrame({"cat": values})

    def test_non_dp_mode_truncates_low_rank_label_out_of_candidacy(self):
        df = self._many_category_df()
        snap = compute_distribution_snapshot(df)  # ordinary fit, top_k=20 default
        labels = {tv["value"] for tv in snap["columns"]["cat"]["stats"]["top_values"]}
        assert "val24" not in labels  # rank 25, truncated pre-threshold
        assert snap["columns"]["cat"]["stats"]["other_count"] > 0

    def test_dp_mode_retains_low_rank_label_as_candidate(self):
        df = self._many_category_df()
        snap = compute_distribution_snapshot(df, dp_mode=True)
        labels = {tv["value"] for tv in snap["columns"]["cat"]["stats"]["top_values"]}
        assert "val24" in labels  # candidacy is full-vocabulary, not top-20
        assert snap["columns"]["cat"]["stats"]["other_count"] == 0

    def test_dp_mode_all_distinct_labels_are_candidates(self):
        df = self._many_category_df()
        snap = compute_distribution_snapshot(df, dp_mode=True)
        assert len(snap["columns"]["cat"]["stats"]["top_values"]) == 25

    def test_dp_mode_categorical_distinct_from_high_cardinality_marker(self):
        # Full-vocabulary CANDIDACY (Fix 1) must not be mistaken for the
        # HC-5 full-vocabulary-RELEASE marker: the latter still bypasses
        # the tau threshold entirely and stays hard-forbidden alongside dp.
        df = self._many_category_df()
        snap = compute_distribution_snapshot(df, dp_mode=True)
        assert "high_cardinality" not in snap["columns"]["cat"]["stats"]

    def test_released_set_depends_on_noised_count_not_rank(self):
        from decoy_engine.quality.dp import apply_dp_noise

        df = self._many_category_df()
        snap = compute_distribution_snapshot(df, dp_mode=True)
        out = apply_dp_noise(snap, epsilon=2.0, delta=1e-3, rng=np.random.default_rng(0))
        labels = {tv["value"] for tv in out["columns"]["cat"]["stats"]["top_values"]}
        # val24's true count (28) comfortably clears a small tau; it must
        # be released despite being rank 25 -- release is a function of the
        # noised count alone, never of the original rank.
        assert "val24" in labels

    def test_dp_mode_threshold_suppression_still_works_downstream(self):
        # Fix 1 only widens CANDIDACY; a genuinely rare candidate must
        # still be suppressed by apply_dp_noise's unchanged tau logic.
        from decoy_engine.quality.dp import apply_dp_noise

        df = pd.concat(
            [self._many_category_df(), pd.DataFrame({"cat": ["rare_unique_patient"]})],
            ignore_index=True,
        )
        snap = compute_distribution_snapshot(df, dp_mode=True)
        assert "rare_unique_patient" in {
            tv["value"] for tv in snap["columns"]["cat"]["stats"]["top_values"]
        }  # a candidate at fit time
        out = apply_dp_noise(snap, epsilon=0.5, delta=1e-6, rng=np.random.default_rng(0))
        labels = {tv["value"] for tv in out["columns"]["cat"]["stats"]["top_values"]}
        assert "rare_unique_patient" not in labels  # suppressed by tau, not candidacy


# ── Fix 7 (gate remediation, closes Fix 3 residual): a categorical column ─
# ── fit under dp_mode carries a per-column provenance marker so the ───────
# ── consume-side check can distinguish full-vocabulary candidacy (Fix 1) ──
# ── from an ordinary top-K-truncated (non-DP) categorical fit. Parallel ───
# ── to numeric's support_origin="caller": categorical is "full_vocabulary".


class TestDpModeCategoricalSupportOriginMarker:
    def _df(self) -> pd.DataFrame:
        return pd.DataFrame({"state": ["CA", "NY", "CA", "TX", "NY"]})

    def test_dp_mode_categorical_support_origin_full_vocabulary(self):
        snap = compute_distribution_snapshot(self._df(), dp_mode=True)
        assert snap["columns"]["state"]["support_origin"] == "full_vocabulary"

    def test_dp_mode_bool_support_origin_full_vocabulary(self):
        # The bool branch also bypasses top-K under dp_mode, so it earns
        # the same data-independent-candidacy marker.
        df = pd.DataFrame({"flag": [True, False, True, True]})
        snap = compute_distribution_snapshot(df, dp_mode=True)
        assert snap["columns"]["flag"]["support_origin"] == "full_vocabulary"

    def test_plain_categorical_omits_support_origin_key_entirely(self):
        # Byte-identity for existing callers: no dp_mode, no numeric_domains
        # -> the key is absent, not "data".
        snap = compute_distribution_snapshot(self._df())
        assert "support_origin" not in snap["columns"]["state"]

    def test_numeric_domains_without_dp_mode_categorical_is_data_not_full_vocab(self):
        # numeric_domains alone (no dp_mode) does NOT make categorical
        # candidacy data-independent -- the top-K truncation still applies --
        # so the marker must stay "data", never "full_vocabulary".
        df = pd.DataFrame({"age": [31, 42, 55], "state": ["CA", "NY", "CA"]})
        snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)})
        assert snap["columns"]["state"]["support_origin"] == "data"

    def test_marker_survives_apply_dp_noise(self):
        from decoy_engine.quality.dp import apply_dp_noise

        snap = compute_distribution_snapshot(self._df(), dp_mode=True)
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        assert noisy["columns"]["state"]["support_origin"] == "full_vocabulary"


# ── Fix 2 (gate remediation, P0): datetime/freetext are OUT OF SCOPE for ─
# ── dp_mode -- their support is data-dependent and DPS-1 does not yet ────
# ── make it independent, so dp_mode must reject rather than silently ─────
# ── charge a non-DP release into epsilon_total. ───────────────────────────


class TestDpModeRejectsDatetimeAndFreetext:
    def test_dp_mode_datetime_column_raises(self):
        df = pd.DataFrame({"joined": pd.to_datetime(["2020-01-01", "2021-06-15", "2022-12-31"])})
        with pytest.raises(ValueError, match="datetime"):
            compute_distribution_snapshot(df, dp_mode=True)

    def test_dp_mode_freetext_column_raises(self):
        df = pd.DataFrame(
            {"comment": [f"unique free text comment number {i} with words" for i in range(40)]}
        )
        with pytest.raises(ValueError, match="freetext"):
            compute_distribution_snapshot(df, dp_mode=True)

    def test_non_dp_mode_datetime_unaffected(self):
        df = pd.DataFrame({"joined": pd.to_datetime(["2020-01-01", "2021-06-15", "2022-12-31"])})
        snap = compute_distribution_snapshot(df)
        assert snap["columns"]["joined"]["kind"] == "datetime"

    def test_non_dp_mode_freetext_unaffected(self):
        df = pd.DataFrame(
            {"comment": [f"unique free text comment number {i} with words" for i in range(40)]}
        )
        snap = compute_distribution_snapshot(df)
        assert snap["columns"]["comment"]["kind"] == "freetext"

    def test_dp_mode_datetime_alongside_valid_numeric_domain_still_raises(self):
        # A mixed frame: the numeric precondition is satisfied, but the
        # datetime column has no precondition to satisfy at all -- the
        # whole fit must still fail closed.
        df = pd.DataFrame(
            {
                "age": [31, 42, 55],
                "joined": pd.to_datetime(["2020-01-01", "2021-06-15", "2022-12-31"]),
            }
        )
        with pytest.raises(ValueError, match="datetime"):
            compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)}, dp_mode=True)
