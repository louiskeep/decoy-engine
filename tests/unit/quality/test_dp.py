"""Unit tests for `decoy_engine.quality.dp.fit_dp_snapshot` (DPS Scope B).

Supersedes the Option A `apply_dp_noise` suite entirely (that mechanism
is deleted). Covers the guide's named assertions for step 5 section 3
(config validation, release ID minting, disclosure-channel regressions,
categorical order/other_count derivation) plus the section 7.2 unseeded
statistical mechanism tests.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from decoy_engine.quality.dp import DpError, fit_dp_snapshot
from decoy_engine.quality.dp_budget import _FakeMeasurement

# Fixed test-confidence budget (guide section 7.2): 1e-6 false-failure
# probability per statistical test per run, derived once as a module
# constant so a later reader can recheck the arithmetic.
_ALPHA = 1e-6


def _mixed_df(n: int = 2000, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "age": rng.integers(0, 120, size=n).astype(float),
            "state": rng.choice(["CA", "NY", "TX", "ZZ"], size=n, p=[0.5, 0.3, 0.15, 0.05]),
        }
    )


class TestConfigValidation:
    def test_production_dp_fit_exposes_no_seed_or_rng_parameter(self):
        import inspect

        sig = inspect.signature(fit_dp_snapshot)
        assert "seed" not in sig.parameters
        assert "rng" not in sig.parameters

    def test_dp_fit_rejects_missing_public_column_declarations(self):
        df = pd.DataFrame({"age": [1.0, 2.0], "state": ["a", "b"]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=["state"],
                numeric_domains={},  # "age" undeclared
                epsilon=1.0,
                delta=1e-6,
            )
        assert exc.value.code == "dp_column_declaration_incomplete"

    def test_dp_fit_rejects_overlapping_public_column_declarations(self):
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=["age"],
                numeric_domains={"age": (0.0, 120.0)},
                epsilon=1.0,
                delta=1e-6,
            )
        assert exc.value.code == "dp_column_declaration_overlap"

    @pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), "abc"])
    def test_invalid_epsilon_rejected(self, bad):
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=[],
                numeric_domains={"age": (0.0, 120.0)},
                epsilon=bad,
                delta=1e-6,
            )
        assert exc.value.code == "dp_epsilon_invalid"

    @pytest.mark.parametrize("bad", [0, 1, 1.0, -1e-6, float("nan"), float("inf")])
    def test_invalid_delta_rejected_including_zero(self, bad):
        # delta=0 is rejected even for a numeric-only fit (guide 9.10 item 2).
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=[],
                numeric_domains={"age": (0.0, 120.0)},
                epsilon=1.0,
                delta=bad,
            )
        assert exc.value.code == "dp_delta_invalid"

    @pytest.mark.parametrize("bad", [1, 1.5, 0, -1])
    def test_invalid_numeric_bins_rejected(self, bad):
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df,
                categorical_columns=[],
                numeric_domains={"age": (0.0, 120.0)},
                epsilon=1.0,
                delta=1e-6,
                numeric_bins=bad,
            )
        assert exc.value.code == "dp_numeric_bins_invalid"

    @pytest.mark.parametrize(
        "bounds", [(120.0, 0.0), (0.0, 0.0), (float("nan"), 1.0), (0.0, float("inf"))]
    )
    def test_invalid_numeric_domain_rejected(self, bounds):
        df = pd.DataFrame({"age": [1.0, 2.0]})
        with pytest.raises(DpError) as exc:
            fit_dp_snapshot(
                df, categorical_columns=[], numeric_domains={"age": bounds}, epsilon=1.0, delta=1e-6
            )
        assert exc.value.code == "dp_numeric_domain_invalid"

    def test_numeric_bins_default_is_ten_and_recorded_in_artifact(self):
        df = pd.DataFrame({"age": [1.0, 2.0, 3.0]})
        snap = fit_dp_snapshot(
            df,
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        assert snap["dp"]["numeric_bins"] == 10
        assert len(snap["columns"]["age"]["stats"]["bin_counts"]) == 10


class TestReleaseIds:
    def test_independent_fits_mint_distinct_release_ids(self):
        df = pd.DataFrame({"age": [1.0, 2.0, 3.0]})
        snap_a = fit_dp_snapshot(
            df,
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        snap_b = fit_dp_snapshot(
            df,
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        assert snap_a["dp"]["release_id"] != snap_b["dp"]["release_id"]

    def test_copied_snapshot_preserves_release_id(self):
        import copy
        import json

        df = pd.DataFrame({"age": [1.0, 2.0, 3.0]})
        snap = fit_dp_snapshot(
            df,
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        copy_a = copy.deepcopy(snap)
        copy_b = json.loads(json.dumps(snap))
        assert copy_a["dp"]["release_id"] == snap["dp"]["release_id"]
        assert copy_b["dp"]["release_id"] == snap["dp"]["release_id"]


class TestArtifactShape:
    def test_dp_artifact_emits_no_exact_column_scalars(self):
        snap = fit_dp_snapshot(
            _mixed_df(),
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        age = snap["columns"]["age"]
        assert age["stats"]["mean"] is None
        assert age["stats"]["std"] is None
        assert age["stats"]["quantiles"] == {}
        state = snap["columns"]["state"]
        assert "high_cardinality" not in state["stats"]
        assert "support_origin" not in state

    def test_dp_artifact_records_opendp_and_dp_accounting_versions(self):
        import importlib.metadata

        snap = fit_dp_snapshot(
            pd.DataFrame({"age": [1.0, 2.0]}),
            categorical_columns=[],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=1.0,
            delta=1e-6,
        )
        assert snap["dp"]["opendp_version"] == importlib.metadata.version("opendp")
        assert snap["dp"]["dp_accounting_version"] == importlib.metadata.version("dp-accounting")

    def test_dp_artifact_totals_are_the_accountant_result_not_the_request(self):
        snap = fit_dp_snapshot(
            _mixed_df(),
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        assert snap["dp"]["epsilon_total"] <= 2.0
        assert snap["dp"]["epsilon_total"] != 2.0  # the accountant's result, not the request

    def test_dp_fit_certificate_count_equals_query_count(self):
        snap = fit_dp_snapshot(
            _mixed_df(),
            categorical_columns=["state"],
            numeric_domains={"age": (0.0, 120.0)},
            epsilon=2.0,
            delta=1e-6,
        )
        assert snap["dp"]["query_count"] == 1 + 1 + 2  # row_count + 1 numeric + 2*1 categorical


class TestDisclosureChannelRegressions:
    """Guide section 7.3: private values change, public declarations stay
    fixed; only the observable schema/success class/schedule may match,
    never the released values themselves."""

    def test_dp_fit_kind_and_success_are_identical_across_30_31_distinct_neighbors(self):
        df_30 = pd.DataFrame({"cat": [f"val{i}" for i in range(30)]})
        df_31 = pd.DataFrame({"cat": [f"val{i}" for i in range(31)]})
        snap_30 = fit_dp_snapshot(
            df_30, categorical_columns=["cat"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        snap_31 = fit_dp_snapshot(
            df_31, categorical_columns=["cat"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        assert (
            snap_30["columns"]["cat"]["kind"] == snap_31["columns"]["cat"]["kind"] == "categorical"
        )
        assert snap_30["dp"]["query_count"] == snap_31["dp"]["query_count"]

    def test_dp_fit_all_null_declared_categorical_runs_measurement_and_emits_categorical_shape(
        self,
    ):
        df = pd.DataFrame({"cat": [None, None, None, None]})
        snap = fit_dp_snapshot(
            df, categorical_columns=["cat"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        assert snap["columns"]["cat"]["kind"] == "categorical"
        assert snap["dp"]["query_count"] == 1 + 2  # row_count + 2 categorical queries

    def test_dp_fit_numeric_shape_and_charge_schedule_are_data_independent_for_all_null_and_all_inf(
        self,
    ):
        domains = {"age": (0.0, 120.0)}
        all_null = fit_dp_snapshot(
            pd.DataFrame({"age": [None, None, None]}),
            categorical_columns=[],
            numeric_domains=domains,
            epsilon=2.0,
            delta=1e-6,
        )
        all_inf = fit_dp_snapshot(
            pd.DataFrame({"age": [float("inf"), float("-inf"), float("inf")]}),
            categorical_columns=[],
            numeric_domains=domains,
            epsilon=2.0,
            delta=1e-6,
        )
        ordinary = fit_dp_snapshot(
            pd.DataFrame({"age": [10.0, 50.0, 90.0]}),
            categorical_columns=[],
            numeric_domains=domains,
            epsilon=2.0,
            delta=1e-6,
        )
        for snap in (all_null, all_inf, ordinary):
            assert (
                snap["columns"]["age"]["stats"]["bin_edges"]
                == ordinary["columns"]["age"]["stats"]["bin_edges"]
            )
            assert len(snap["columns"]["age"]["stats"]["bin_counts"]) == 10
            assert snap["dp"]["query_count"] == ordinary["dp"]["query_count"]

    def test_all_distinct_categorical_versus_single_repeated_label_same_schema(self):
        all_distinct = pd.DataFrame({"cat": [f"v{i}" for i in range(50)]})
        one_label = pd.DataFrame({"cat": ["only"] * 50})
        snap_distinct = fit_dp_snapshot(
            all_distinct, categorical_columns=["cat"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        snap_one = fit_dp_snapshot(
            one_label, categorical_columns=["cat"], numeric_domains={}, epsilon=2.0, delta=1e-6
        )
        assert snap_distinct["dp"]["query_count"] == snap_one["dp"]["query_count"]
        # Neither raises, and other_count is well-formed (no divide-by-zero
        # or exact fallback) even when nothing is retained.
        assert snap_distinct["columns"]["cat"]["stats"]["other_count"] >= 0
        assert snap_one["columns"]["cat"]["stats"]["other_count"] >= 0


class _FixedCategoricalBackend:
    """H3/H4 seam (guide section 5 step 3 / section 6 rows 1a/1b): forces
    `fit_dp_snapshot`'s categorical release to a FIXED grouped-count dict
    and non-null total regardless of `values`, so a test can vary the
    real, suppressed input while observing an artifact whose released
    categorical quantities are pinned. `count_measurement`/`numeric_
    measurement` are also fixed so the WHOLE artifact (not just the
    categorical column) is reproducible across different input frames,
    letting a byte-identity assertion cover more than the one column."""

    def __init__(self, *, row_count: int, grouped: dict[str, int], total: int) -> None:
        self._row_count = row_count
        self._grouped = dict(grouped)
        self._total = total

    def count_measurement(self, eps_q: float) -> _FakeMeasurement:
        return _FakeMeasurement(certificate=0.01, released=self._row_count)

    def numeric_measurement(self, eps_q: float, interior_edges: tuple[float, ...]):
        return _FakeMeasurement(certificate=0.01, released=[0] * (len(interior_edges) + 1))

    def categorical_measurements(self, eps_q: float, delta_alloc: float):
        return (
            _FakeMeasurement(certificate=(0.01, delta_alloc / 2 or 1e-9), released=self._grouped),
            _FakeMeasurement(certificate=0.01, released=self._total),
        )


class TestCategoricalRelease:
    def test_categorical_release_order_uses_noised_counts_not_true_rank(self):
        """Defect 1a (guide section 6): the previous version of this test
        used a fixture whose true counts (~1500/900/450/150 at epsilon
        5.0) are so far apart that Laplace noise cannot plausibly invert
        them -- the assertion held whether the code sorted by released
        count (correct) or by true rank (the defect), so it never
        falsified anything. This version uses the `_session_backend` seam
        to force a released grouped-count dict that INVERTS the true
        frequency order of its input frame, then asserts the artifact's
        `top_values` follows the FORCED (inverted) order. Sorting by true
        rank instead of released count would emit CA/NY/TX; sorting by
        released count (correct) emits TX/NY/CA."""
        # True frequency order in the input: CA (900) > NY (90) > TX (10).
        df = pd.DataFrame({"state": ["CA"] * 900 + ["NY"] * 90 + ["TX"] * 10})
        # Forced RELEASED order inverts it: TX highest, NY middle, CA lowest.
        backend = _FixedCategoricalBackend(
            row_count=1000, grouped={"CA": 10, "NY": 90, "TX": 900}, total=1000
        )
        snap = fit_dp_snapshot(
            df,
            categorical_columns=["state"],
            numeric_domains={},
            epsilon=5.0,
            delta=1e-6,
            _session_backend=backend,
        )
        top = snap["columns"]["state"]["stats"]["top_values"]
        assert [t["value"] for t in top] == ["TX", "NY", "CA"]
        assert [t["count"] for t in top] == [900, 90, 10]
        counts = [t["count"] for t in top]
        assert counts == sorted(counts, reverse=True)

    def test_categorical_other_count_is_derived_only_from_noised_total_and_kept_counts(self):
        """Defect 1b (guide section 6): the previous version of this test
        ran ONE scenario and restated `other_count`'s own formula line for
        line -- a tautology, not a proof of independence from suppressed
        values. This version runs TWO scenarios whose forced released
        total and kept pairs are IDENTICAL but whose true, suppressed
        (non-top) labels differ, and asserts the resulting artifacts are
        byte-identical except `release_id` (which always mints fresh per
        fit by design -- guide section 7.3/binding decision 10). Any
        dependence on the suppressed values -- for `other_count` or
        anything else in the artifact -- would make the two differ.

        BLOCKER B-2: an earlier version of this test made the two
        scenarios' suppressed tails differ ONLY in label NAMES (20 rows
        across 20 distinct labels in both, `rareA{i}` vs `rareB{i}`) --
        same suppressed MASS, same suppressed CARDINALITY. That falsifies
        dependence on label strings and nothing else: a defect reading the
        true suppressed row count or the true suppressed label count
        straight off the input (instead of deriving `other_count` from the
        noised total and kept counts alone) would compute the identical
        number for both scenarios and this test would not notice. The two
        tails below differ in both mass (20 rows vs. 40) and cardinality
        (20 distinct labels vs. 7), so a defect reading either true
        quantity off the suppressed values makes `snap_a` and `snap_b`
        disagree."""
        backend = _FixedCategoricalBackend(
            row_count=520, grouped={"CA": 300, "NY": 150, "TX": 50}, total=520
        )
        # Same kept labels/counts and same released total; DIFFERENT
        # suppressed tail MASS and CARDINALITY (not merely label names):
        # df_a has 20 rows across 20 distinct rare labels, df_b has 40
        # rows across 7 distinct rare labels.
        df_a = pd.DataFrame(
            {"state": ["CA"] * 300 + ["NY"] * 150 + ["TX"] * 50 + [f"rareA{i}" for i in range(20)]}
        )
        rare_labels_b = [f"rareB{i}" for i in range(7)]
        tail_b = [rare_labels_b[i % len(rare_labels_b)] for i in range(40)]
        df_b = pd.DataFrame({"state": ["CA"] * 300 + ["NY"] * 150 + ["TX"] * 50 + tail_b})
        snap_a = fit_dp_snapshot(
            df_a,
            categorical_columns=["state"],
            numeric_domains={},
            epsilon=5.0,
            delta=1e-6,
            _session_backend=backend,
        )
        snap_b = fit_dp_snapshot(
            df_b,
            categorical_columns=["state"],
            numeric_domains={},
            epsilon=5.0,
            delta=1e-6,
            _session_backend=backend,
        )
        stats_a = snap_a["columns"]["state"]["stats"]
        kept_sum = sum(t["count"] for t in stats_a["top_values"])
        assert stats_a["other_count"] == max(
            0, snap_a["columns"]["state"]["non_null_count"] - kept_sum
        )

        del snap_a["dp"]["release_id"]
        del snap_b["dp"]["release_id"]
        assert snap_a == snap_b


class TestUnseededStatisticalMechanism:
    """Guide section 7.2. Neither test may be seeded; both are designed
    so a correct implementation fails less often than `_ALPHA` per run."""

    def test_independent_dp_fits_are_not_deterministic(self):
        df = pd.DataFrame({"age": [float(i % 120) for i in range(2000)]})
        domains = {"age": (0.0, 120.0)}
        vectors = [
            tuple(
                fit_dp_snapshot(
                    df, categorical_columns=[], numeric_domains=domains, epsilon=5.0, delta=1e-6
                )["columns"]["age"]["stats"]["bin_counts"]
            )
            for _ in range(5)
        ]
        # Collision probability of 5 independent 10-bin noised-count
        # vectors all matching, under a real Laplace mechanism at this
        # scale, is far below _ALPHA; a broken (e.g. zero-noise) mechanism
        # would make every vector identical.
        assert len(set(vectors)) > 1

    def test_count_one_release_probability_upper_bound(self):
        """A label with true count 1 must be released no more often than
        its measurement's own certified delta (times a documented slack),
        one-sided Clopper-Pearson at alpha=_ALPHA. Compares against the
        CERTIFIED delta of the thresholded chain, not the fit-wide delta
        or the allocation target (guide section 7.2).

        Calibration (the allocation search) runs ONCE: it is a pure
        function of the public (epsilon, delta, schedule) request, never
        of values (guide section 4.3.2), so re-deriving it per trial would
        only slow the test down, not change what it measures. The
        calibrated measurement is invoked fresh (unseeded OpenDP
        randomness) on every trial.

        The schedule's (epsilon, delta) here is a private test fixture,
        not a production default: it exists only to fix a certified delta
        against which the empirical release rate is checked, and the
        property under test (empirical rate tracks certified delta) holds
        at any delta. A one-categorical-column schedule at delta=1e-3
        (certified delta ~5e-4) needs an infeasible trial count to resolve
        at alpha=1e-6: with 1 expected release in 2000 trials the
        Clopper-Pearson upper bound is dominated by sampling noise (~16x
        certified delta), not by mechanism behavior, so the test cannot
        tell a correct implementation from a broken one. delta=0.02 raises
        the certified delta to ~0.01, so 5000 trials give ~50 expected
        releases; per Clopper-Pearson, that pins the upper bound to
        ~1.8-2.7x certified delta even in a 5-sigma-high draw (verified
        against scipy.stats.beta.ppf during test design), comfortably
        inside the slack factor below while still catching an
        order-of-magnitude regression in release probability.
        """
        from decoy_engine.quality.dp_budget import OpenDpReleaseSession
        from decoy_engine.quality.dp_schedule import CategoricalQuerySpec, Schedule

        schedule = Schedule(
            row_count_name="rc",
            numeric=(),
            categorical=(CategoricalQuerySpec("g", "t"),),
        )
        session = OpenDpReleaseSession(schedule, epsilon=1.0, delta=0.02)
        grouped_meas, _total_meas = session._categorical_measurements(
            session._eps_q, session._delta_per_categorical
        )
        certified_delta = grouped_meas.map(1)[1]

        n_trials = 5000
        values = ["common"] * 999 + ["rare_one"]
        releases = sum(1 for _ in range(n_trials) if "rare_one" in grouped_meas.invoke(values))

        # One-sided Clopper-Pearson upper bound at alpha=_ALPHA, computed
        # via the regularized incomplete beta function's relationship to
        # the F distribution (Clopper & Pearson 1934); stdlib-only so this
        # test carries no extra runtime dependency.
        upper = _clopper_pearson_upper(releases, n_trials, _ALPHA)
        slack_factor = 10  # documented slack: catches an order-of-magnitude regression
        assert upper <= certified_delta * slack_factor, (
            f"N={n_trials} releases={releases} observed_rate={releases / n_trials} "
            f"upper={upper} certified_delta={certified_delta}"
        )


def _clopper_pearson_upper(successes: int, n: int, alpha: float) -> float:
    """One-sided Clopper-Pearson upper confidence bound on a binomial
    success probability (Clopper & Pearson, "The Use of Confidence or
    Fiducial Limits Illustrated in the Case of the Binomial", Biometrika
    1934). `successes == n` gives the trivial upper bound of 1.0; otherwise
    the bound is the value `p` solving `Beta_CDF(observed_rate; successes+1,
    n-successes) = 1 - alpha`, found here by bisection over the
    regularized incomplete beta function (`math.lgamma`-based series, no
    scipy dependency) rather than scipy's `beta.ppf`, since scipy is not a
    declared runtime dependency of this engine."""
    if successes >= n:
        return 1.0

    def reg_incomplete_beta(x: float, a: float, b: float) -> float:
        # Continued-fraction evaluation (Numerical Recipes 6.4), accurate
        # to double precision for the parameter ranges this test uses.
        if x <= 0.0:
            return 0.0
        if x >= 1.0:
            return 1.0
        ln_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
        front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - ln_beta) / a
        if x >= (a + 1.0) / (a + b + 2.0):
            return 1.0 - reg_incomplete_beta(1.0 - x, b, a)
        f, c, d = 1.0, 1.0, 0.0
        for i in range(200):
            m = i // 2
            if i == 0:
                numerator = 1.0
            elif i % 2 == 0:
                numerator = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
            else:
                numerator = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
            d = 1.0 + numerator * d
            if abs(d) < 1e-30:
                d = 1e-30
            d = 1.0 / d
            c = 1.0 + numerator / c
            if abs(c) < 1e-30:
                c = 1e-30
            f *= d * c
            if abs(d * c - 1.0) < 1e-12:
                break
        return front * (f - 1.0)

    a, b = successes + 1, n - successes
    lo, hi = successes / n, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if reg_incomplete_beta(mid, a, b) < 1 - alpha:
            lo = mid
        else:
            hi = mid
    return hi
