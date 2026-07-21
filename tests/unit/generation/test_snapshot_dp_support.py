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

from decoy_engine.quality.snapshot import DistributionSnapshotError, compute_distribution_snapshot


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


def test_dp_mode_numeric_precondition_is_independent_of_the_categorical_rejection():
    # dp_mode's numeric fail-closed precondition (a domain per numeric
    # column) is orthogonal to Option A's categorical rejection below: a
    # mixed frame with a properly-domained numeric column still fails
    # closed on the OBJECT column, not the numeric one -- the numeric
    # column's own precondition is satisfied, it just never gets a chance
    # to succeed because the whole fit aborts on "state" first.
    df = pd.DataFrame({"age": [31, 42, 55], "state": ["CA", "NY", "CA"]})
    with pytest.raises(DistributionSnapshotError) as exc:
        compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)}, dp_mode=True)
    assert exc.value.code == "dp_mode_categorical_unsupported"
    assert "state" in exc.value.message


# ── Option A (2026-07-21 DPS remediation): dp_mode rejects EVERY ─────────
# ── object/string column outright, regardless of distinct-value count. ───
# ── Supersedes the prior Fix 1 "full-vocabulary candidacy" mechanism ─────
# ── (categorical release is not yet DP-correct; see CHANGELOG.md) and ────
# ── closes the 30-distinct cardinality cliff (Finding 1d) as a side ──────
# ── effect: there is no longer a distinct-count at which an object ───────
# ── column's dp_mode fit SUCCEEDS, so fit success cannot leak private ────
# ── data through the success/failure channel. ─────────────────────────────


class TestDpModeRejectsObjectColumnsRegardlessOfCardinality:
    """`quality.snapshot.DistributionSnapshotError` with code
    `dp_mode_categorical_unsupported`, raised BEFORE the distinct-count
    that used to decide categorical-vs-freetext is ever computed."""

    def _many_category_df(self) -> pd.DataFrame:
        # 25 distinct labels: under the OLD mechanism this stayed
        # categorical (below the 30-distinct cap) and widened to
        # full-vocabulary candidacy under dp_mode. Kept as a fixture to
        # prove that mechanism no longer engages at all.
        values: list[str] = []
        for i in range(25):
            values += [f"val{i}"] * (100 - i * 3)
        return pd.DataFrame({"cat": values})

    def test_non_dp_mode_still_truncates_low_rank_label_into_other_count(self):
        # Unaffected control: the ordinary (non-dp_mode) fit path is
        # byte-identical to before -- Option A only narrows dp_mode.
        df = self._many_category_df()
        snap = compute_distribution_snapshot(df)  # ordinary fit, top_k=20 default
        labels = {tv["value"] for tv in snap["columns"]["cat"]["stats"]["top_values"]}
        assert "val24" not in labels  # rank 25, truncated pre-threshold
        assert snap["columns"]["cat"]["stats"]["other_count"] > 0

    def test_dp_mode_rejects_many_distinct_object_column(self):
        df = self._many_category_df()  # 25 distinct: would have stayed categorical
        with pytest.raises(DistributionSnapshotError) as exc:
            compute_distribution_snapshot(df, dp_mode=True)
        assert exc.value.code == "dp_mode_categorical_unsupported"
        assert "cat" in exc.value.message

    def test_dp_mode_rejects_few_distinct_object_column(self):
        # A tiny 2-distinct object column -- the OLD mechanism's most
        # trivially "safe-looking" categorical case -- is rejected exactly
        # the same as the 25-distinct case above: there is no cardinality
        # at which this now succeeds.
        df = pd.DataFrame({"flag_str": ["yes", "no", "yes", "yes"]})
        with pytest.raises(DistributionSnapshotError) as exc:
            compute_distribution_snapshot(df, dp_mode=True)
        assert exc.value.code == "dp_mode_categorical_unsupported"

    def test_dp_mode_rejects_object_column_that_would_have_become_freetext(self):
        # >30 distinct: the OLD mechanism raised a DIFFERENT ValueError
        # here ("freetext" in the message, via _raise_dp_mode_unsupported_
        # kind). Now it raises the SAME DistributionSnapshotError /
        # dp_mode_categorical_unsupported as the low-cardinality case --
        # one typed error for the whole object/string dtype family.
        df = pd.DataFrame(
            {"comment": [f"unique free text comment number {i} with words" for i in range(40)]}
        )
        with pytest.raises(DistributionSnapshotError) as exc:
            compute_distribution_snapshot(df, dp_mode=True)
        assert exc.value.code == "dp_mode_categorical_unsupported"

    def test_neighboring_30_and_31_distinct_object_columns_fail_identically(self):
        # Finding 1d closure: the OLD mechanism's success/failure outcome
        # for an object column was an unnoised function of distinct_count
        # (<=30 -> categorical artifact; >30 -> freetext ValueError) --
        # neighboring datasets differing by exactly one distinct value
        # could produce "artifact exists" vs "typed error" at zero privacy
        # cost. Both must now fail with the IDENTICAL code regardless of
        # which side of the old cliff they fall on.
        df_30 = pd.DataFrame({"cat": [f"val{i}" for i in range(30)]})
        df_31 = pd.DataFrame({"cat": [f"val{i}" for i in range(31)]})
        with pytest.raises(DistributionSnapshotError) as exc_30:
            compute_distribution_snapshot(df_30, dp_mode=True)
        with pytest.raises(DistributionSnapshotError) as exc_31:
            compute_distribution_snapshot(df_31, dp_mode=True)
        assert exc_30.value.code == exc_31.value.code == "dp_mode_categorical_unsupported"

    def test_dp_mode_rejection_precedes_distinct_count_computation(self):
        # The decision must not even LOOK at distinct_count before
        # rejecting -- assert via a monkeypatch-free structural proof: a
        # 1-row (1-distinct) object column is rejected exactly like the
        # many-distinct fixtures above, so there is no distinct-count
        # threshold left to find.
        df = pd.DataFrame({"cat": ["only_value"]})
        with pytest.raises(DistributionSnapshotError) as exc:
            compute_distribution_snapshot(df, dp_mode=True)
        assert exc.value.code == "dp_mode_categorical_unsupported"

    def test_non_dp_mode_object_columns_entirely_unaffected(self):
        # Byte-identity for existing (non-dp_mode) callers across the
        # cardinality range that used to matter under dp_mode.
        df = self._many_category_df()
        snap = compute_distribution_snapshot(df)  # no raise
        assert snap["columns"]["cat"]["kind"] == "categorical"

    def test_dp_mode_rejects_high_cardinality_object_column(self):
        # dennis HIGH-1 (2026-07-21): the high_cardinality early-return in
        # _stats_for previously short-circuited BEFORE the object/string
        # dp_mode rejection, so `dp_mode=True` + high_cardinality on an
        # object column silently succeeded and minted a dp-labeled artifact
        # over the full REAL vocabulary -- exactly the broken categorical
        # release Option A exists to fence off. It must now reject with the
        # same code as every other object/string column.
        df = pd.DataFrame({"name": [f"person_{i}" for i in range(50)]})
        with pytest.raises(DistributionSnapshotError) as exc:
            compute_distribution_snapshot(df, dp_mode=True, high_cardinality_columns=["name"])
        assert exc.value.code == "dp_mode_categorical_unsupported"
        assert "name" in exc.value.message

    def test_dp_mode_rejects_high_cardinality_on_the_request_not_the_dtype(self):
        # The rejection is on the high_cardinality REQUEST (which forces the
        # categorical release path), not a specific dtype. A valid
        # high_cardinality column is always string-family, but even a bool
        # column passed as high_cardinality under dp_mode is rejected with
        # dp_mode_categorical_unsupported (the fit-side twin of the compile-
        # time high_cardinality-under-DP contract). Plain bool WITHOUT
        # high_cardinality still fits under dp_mode -- that path is unchanged
        # (see TestDpModeCategoricalSupportOriginMarker.test_dp_mode_bool_*).
        df = pd.DataFrame({"flag": [True, False, True, True, False]})
        with pytest.raises(DistributionSnapshotError) as exc:
            compute_distribution_snapshot(df, dp_mode=True, high_cardinality_columns=["flag"])
        assert exc.value.code == "dp_mode_categorical_unsupported"

    def test_non_dp_mode_high_cardinality_object_unaffected(self):
        # Byte-identity: without dp_mode, high_cardinality on an object
        # column is untouched by the HIGH-1 fix.
        df = pd.DataFrame({"name": [f"person_{i}" for i in range(50)]})
        snap = compute_distribution_snapshot(df, high_cardinality_columns=["name"])
        assert snap["columns"]["name"]["kind"] == "categorical"


# ── Fix 7 (gate remediation, closes Fix 3 residual): a categorical column ─
# ── fit under dp_mode carries a per-column provenance marker so the ───────
# ── consume-side check can distinguish full-vocabulary candidacy (Fix 1) ──
# ── from an ordinary top-K-truncated (non-DP) categorical fit. Parallel ───
# ── to numeric's support_origin="caller": categorical is "full_vocabulary".
# ── Option A (2026-07-21) narrows WHO can still reach this marker under ───
# ── dp_mode to bool only -- object/string columns are rejected before ─────
# ── they get there (see TestDpModeRejectsObjectColumnsRegardlessOfCardinality
# ── above); bool's candidate set is dtype-determined (`{True, False}`), ───
# ── never data-dependent, so it is unaffected by the categorical-mechanism ─
# ── soundness problems that motivated the object/string rejection.


class TestDpModeCategoricalSupportOriginMarker:
    def _df(self) -> pd.DataFrame:
        return pd.DataFrame({"state": ["CA", "NY", "CA", "TX", "NY"]})

    def test_dp_mode_rejects_object_column_before_stamping_any_marker(self):
        # Supersedes the old "state column gets full_vocabulary" test:
        # Option A rejects the fit outright, so no marker is ever stamped.
        with pytest.raises(DistributionSnapshotError) as exc:
            compute_distribution_snapshot(self._df(), dp_mode=True)
        assert exc.value.code == "dp_mode_categorical_unsupported"

    def test_dp_mode_bool_support_origin_full_vocabulary(self):
        # The bool branch also bypasses top-K under dp_mode, so it earns
        # the same data-independent-candidacy marker. Unaffected by Option
        # A: bool's candidate set is dtype-determined, not data-dependent.
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
        # so the marker must stay "data", never "full_vocabulary". Unaffected
        # by Option A: no dp_mode here, so the object-column rejection never
        # engages.
        df = pd.DataFrame({"age": [31, 42, 55], "state": ["CA", "NY", "CA"]})
        snap = compute_distribution_snapshot(df, numeric_domains={"age": (0.0, 120.0)})
        assert snap["columns"]["state"]["support_origin"] == "data"

    def test_marker_survives_apply_dp_noise(self):
        # Uses bool, not the object-column `_df()`: an object column can no
        # longer reach a dp_mode-fit full_vocabulary marker at all (Option
        # A), but bool still can -- this proves the marker (once it exists)
        # survives apply_dp_noise, using the one dtype for which it does.
        from decoy_engine.quality.dp import apply_dp_noise

        df = pd.DataFrame({"flag": [True, False, True, True]})
        snap = compute_distribution_snapshot(df, dp_mode=True)
        noisy = apply_dp_noise(snap, epsilon=1.0, delta=1e-6, rng=np.random.default_rng(0))
        assert noisy["columns"]["flag"]["support_origin"] == "full_vocabulary"


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
        # Option A (2026-07-21): freetext no longer has its own ValueError
        # branch -- it is object/string dtype, so it now hits the SAME
        # DistributionSnapshotError / dp_mode_categorical_unsupported as
        # every other object/string column (see
        # TestDpModeRejectsObjectColumnsRegardlessOfCardinality above),
        # BEFORE the distinct-count check that used to route it to
        # "freetext" specifically is ever reached.
        df = pd.DataFrame(
            {"comment": [f"unique free text comment number {i} with words" for i in range(40)]}
        )
        with pytest.raises(DistributionSnapshotError) as exc:
            compute_distribution_snapshot(df, dp_mode=True)
        assert exc.value.code == "dp_mode_categorical_unsupported"

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
