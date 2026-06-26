"""ML0 proof test (BF2): the regex-detector baseline over labeled fixtures.

This is the evidence artifact. It pins the concrete places the 26 built-in
detectors miss, measured by running the REAL detector set over deterministic
synthetic fixtures with known ground truth:

  - Cryptic-header health PII (MRN, health-plan ID under opaque column
    names) is MISSED entirely: those detectors are name-hint-gated, so an
    opaque header (q9, z3) gives them nothing to fire on.
  - Content-based detectors (SSN, email, PAN) ARE header-agnostic and fire
    correctly even under opaque headers (c1, f07, xref).
  - A non-PII business key (account_id) is a FALSE POSITIVE: the mrn
    detector deliberately claims generic ``account``/``acct`` identifier
    columns, so it over-fires at high confidence on a column that is not
    medical PII.

If the detector set changes, these pinned numbers change - that is the
point: the harness makes detector recall/precision a measured quantity.
"""

import pandas as pd

from decoy_engine.storm.eval import (
    NO_DETECTOR,
    LabeledFixture,
    build_fixtures,
    run_baseline,
)


class TestFixtures:
    def test_five_fixtures_with_labels(self):
        fixtures = build_fixtures()
        assert [f.name for f in fixtures] == [
            "hipaa",
            "pci",
            "account_order",
            "claim",
            "cryptic_header",
        ]
        for fx in fixtures:
            # every column carries a ground-truth label
            assert set(fx.labels) == set(fx.df.columns)
            assert len(fx.df) > 0

    def test_fixtures_are_deterministic(self):
        a = build_fixtures()
        b = build_fixtures()
        for fa, fb in zip(a, b, strict=True):
            pd.testing.assert_frame_equal(fa.df, fb.df)
            assert fa.labels == fb.labels


class TestBaselineMetrics:
    def test_overall_recall_pinned(self):
        rep = run_baseline()
        # 11 of 13 PII columns correctly identified.
        assert rep.overall_recall == 0.8462

    def test_harness_is_deterministic(self):
        assert run_baseline().to_dict() == run_baseline().to_dict()

    def test_cryptic_header_name_hint_detectors_miss(self):
        rep = run_baseline()
        by_col = {(r.fixture, r.column): r for r in rep.columns}
        # MRN values under opaque header "q9" -> no detector fires.
        mrn_miss = by_col[("cryptic_header", "q9")]
        assert mrn_miss.truth_label == "mrn"
        assert mrn_miss.predicted_id is None
        assert mrn_miss.correct is False
        # Health-plan IDs under opaque header "z3" -> no detector fires.
        plan_miss = by_col[("cryptic_header", "z3")]
        assert plan_miss.truth_label == "health_plan_id"
        assert plan_miss.predicted_id is None
        assert plan_miss.correct is False

    def test_content_detectors_fire_under_opaque_headers(self):
        rep = run_baseline()
        by_col = {(r.fixture, r.column): r for r in rep.columns}
        for column, expected in [("c1", "ssn"), ("f07", "email"), ("xref", "pan")]:
            r = by_col[("cryptic_header", column)]
            assert r.predicted_id == expected, column
            assert r.correct is True

    def test_account_id_is_a_false_positive(self):
        # Non-PII business key mis-tagged as MRN at high confidence: the mrn
        # detector intentionally claims generic account/acct identifier columns.
        rep = run_baseline()
        by_col = {(r.fixture, r.column): r for r in rep.columns}
        acct = by_col[("account_order", "account_id")]
        assert acct.truth_label == NO_DETECTOR
        assert acct.predicted_id == "mrn"
        assert acct.predicted_confidence == "high"
        assert acct.correct is False
        assert rep.false_positive_count == 1

    def test_per_field_type_recall_pinned(self):
        rep = run_baseline()
        recall = {ft: m.recall for ft, m in rep.by_field_type.items()}
        # Name-hint-gated health detectors halve their recall because of the
        # cryptic-header miss; content detectors stay perfect.
        assert recall["mrn"] == 0.5
        assert recall["health_plan_id"] == 0.5
        assert recall["ssn"] == 1.0
        assert recall["email"] == 1.0
        assert recall["pan"] == 1.0
        assert recall["iso_date"] == 1.0
        # NO_DETECTOR field type has no recall concept.
        assert recall[NO_DETECTOR] is None

    def test_mrn_precision_halved_by_false_positive(self):
        rep = run_baseline()
        # mrn predicted twice (hipaa.mrn correct, account_id wrong) -> 0.5.
        assert rep.precision_by_predicted["mrn"] == 0.5
        assert rep.precision_by_predicted["ssn"] == 1.0

    def test_clean_fixtures_have_no_review_burden(self):
        # All canonical fixtures are unambiguous -> no medium-confidence bucket.
        rep = run_baseline()
        assert rep.total_review_burden == 0


class TestReviewBurdenAggregation:
    def test_medium_confidence_matches_counted(self):
        # Crafted column: ~60% valid emails under an opaque header fires the
        # email detector at MEDIUM confidence (no name hint, rate in
        # [0.50, 0.75)) -> one unit of review burden.
        values = [f"user{i}@example.com" for i in range(6)] + ["n/a", "n/a", "n/a", "n/a"]
        df = pd.DataFrame({"blob": values})
        fx = LabeledFixture("crafted", df, {"blob": "email"})
        rep = run_baseline([fx])
        result = rep.columns[0]
        assert result.predicted_id == "email"
        assert result.predicted_confidence == "medium"
        assert result.medium_match_count >= 1
        assert rep.total_review_burden >= 1
        assert rep.by_field_type["email"].review_burden >= 1
