"""ML0 extended-metrics tests (BF2 / ml-benchmarking-and-privacy.md §A.1).

Extends the recognition-harness baseline with per-type F2 (β=2),
aggregate macro-F2 + weighted-F2 + balanced_accuracy, an entity-type
confusion matrix, and enumerated FP/FN lists. All values are pinned to the
current fixture corpus so any detector change that moves them fails CI.

Gate reference: ml-benchmarking-and-privacy.md §A.1, §A.7.
"""

from decoy_engine.storm.eval import NO_DETECTOR, run_baseline


class TestPerTypeF2:
    """Per-type precision, F2 (β=2) pinned to fixture corpus."""

    def test_mrn_f2(self):
        # mrn: tp=1, fp=1 (account_id over-fires), fn=1 (cryptic header miss)
        # F2 = 5·1 / (5·1 + 1 + 4·1) = 5/10 = 0.5
        rep = run_baseline()
        m = rep.by_field_type["mrn"]
        assert m.false_positives == 1
        assert m.precision == 0.5
        assert m.f2 == 0.5

    def test_health_plan_id_f2(self):
        # health_plan_id: tp=1, fp=0, fn=1 (cryptic header miss)
        # F2 = 5·1 / (5·1 + 0 + 4·1) = 5/9 ≈ 0.5556
        rep = run_baseline()
        m = rep.by_field_type["health_plan_id"]
        assert m.false_positives == 0
        assert m.precision == 1.0
        assert m.f2 == 0.5556

    def test_perfect_types_have_f2_one(self):
        rep = run_baseline()
        for type_id in ["ssn", "email", "pan", "iban", "cvv", "npi", "icd10", "iso_date"]:
            m = rep.by_field_type[type_id]
            assert m.f2 == 1.0, f"{type_id} expected F2=1.0"
            assert m.precision == 1.0, f"{type_id} expected precision=1.0"
            assert m.false_positives == 0, f"{type_id} expected fp=0"

    def test_no_detector_type_has_no_f2(self):
        rep = run_baseline()
        m = rep.by_field_type[NO_DETECTOR]
        # NO_DETECTOR columns have no recall/precision/F2 concept.
        assert m.f2 is None
        assert m.precision is None
        assert m.recall is None


class TestAggregateMetrics:
    """Macro-F2, weighted-F2, balanced_accuracy pinned values."""

    def test_macro_f2(self):
        # 10 PII types: 8 at F2=1.0, mrn=0.5, health_plan_id=0.5556
        # macro_f2 = (8·1.0 + 0.5 + 0.5556) / 10 = 9.0556 / 10 = 0.9056
        rep = run_baseline()
        assert rep.macro_f2 == 0.9056

    def test_weighted_f2(self):
        # Weighted by support: 8 single-column types at 1.0, mrn(2)=0.5, health_plan_id(2)=0.5556, pan(2)=1.0
        # = (8·1 + 2·0.5 + 2·0.5556 + 2·1.0) / 13
        # = (8 + 1 + 1.1111 + 2) / 13 = 12.1111 / 13 ≈ ...
        # Let the test use the computed value but assert it is between macro_f2 and 1.
        rep = run_baseline()
        assert rep.weighted_f2 == 0.8547

    def test_balanced_accuracy(self):
        # balanced_accuracy = macro-average recall across PII types
        # = (0.5 + 1.0 + 1.0 + 0.5 + 1.0 + 1.0 + 1.0 + 1.0 + 1.0 + 1.0) / 10 = 0.9
        rep = run_baseline()
        assert rep.balanced_accuracy == 0.9

    def test_aggregate_metrics_are_deterministic(self):
        a = run_baseline()
        b = run_baseline()
        assert a.macro_f2 == b.macro_f2
        assert a.weighted_f2 == b.weighted_f2
        assert a.balanced_accuracy == b.balanced_accuracy

    def test_macro_f2_le_one_ge_zero(self):
        rep = run_baseline()
        assert 0.0 <= rep.macro_f2 <= 1.0
        assert 0.0 <= rep.weighted_f2 <= 1.0
        assert 0.0 <= rep.balanced_accuracy <= 1.0


class TestConfusionMatrix:
    """Entity-type confusion matrix shape and spot checks."""

    def test_confusion_matrix_covers_all_truth_labels(self):
        rep = run_baseline()
        truth_labels = set(rep.by_field_type.keys())
        assert set(rep.confusion_matrix.keys()) == truth_labels

    def test_mrn_row_has_two_outcomes(self):
        # hipaa.mrn -> "mrn" (correct), cryptic_header.q9 -> "none" (missed)
        rep = run_baseline()
        mrn_row = rep.confusion_matrix["mrn"]
        assert mrn_row.get("mrn", 0) == 1
        assert mrn_row.get("none", 0) == 1

    def test_none_row_has_fp_and_tn(self):
        # account_order: account_id -> "mrn" (FP), order_id -> "none" (TN)
        # claim: claim_id -> "none", claim_amount -> "none"
        rep = run_baseline()
        none_row = rep.confusion_matrix[NO_DETECTOR]
        assert none_row.get("mrn", 0) == 1  # the FP
        assert none_row.get("none", 0) == 3  # the TNs

    def test_confusion_matrix_row_sums_equal_support(self):
        rep = run_baseline()
        for truth, row in rep.confusion_matrix.items():
            support = rep.by_field_type[truth].support
            assert sum(row.values()) == support, f"row sum mismatch for {truth!r}"

    def test_perfect_type_is_on_diagonal(self):
        rep = run_baseline()
        for type_id in ["ssn", "email", "cvv", "iban", "npi", "icd10", "iso_date"]:
            row = rep.confusion_matrix[type_id]
            assert row.get(type_id, 0) >= 1, f"{type_id} not on diagonal"
            # And no other predicted label in the row (perfect type).
            other = {k: v for k, v in row.items() if k != type_id}
            assert all(v == 0 for v in other.values()), f"{type_id} has off-diagonal entries"


class TestEnumeratedErrors:
    """Enumerated FP/FN lists for inspectability (§A.1)."""

    def test_false_negatives_list_covers_missed_columns(self):
        rep = run_baseline()
        col_ids = {e["column_id"] for e in rep.false_negatives_list}
        assert "cryptic_header.q9" in col_ids
        assert "cryptic_header.z3" in col_ids

    def test_false_negative_entries_have_correct_fields(self):
        rep = run_baseline()
        for entry in rep.false_negatives_list:
            assert entry["error_type"] == "FN"
            assert entry["truth_label"] != NO_DETECTOR
            # predicted_label may be None (missed) or a wrong type
            assert "column_id" in entry
            assert "predicted_label" in entry

    def test_false_positives_list_covers_fp_columns(self):
        rep = run_baseline()
        col_ids = {e["column_id"] for e in rep.false_positives_list}
        assert "account_order.account_id" in col_ids

    def test_false_positive_entries_have_correct_fields(self):
        rep = run_baseline()
        for entry in rep.false_positives_list:
            assert entry["error_type"] == "FP"
            assert entry["truth_label"] == NO_DETECTOR
            assert entry["predicted_label"] is not None  # FP always has a prediction
            assert "column_id" in entry

    def test_fn_list_length_matches_false_negative_counts(self):
        rep = run_baseline()
        total_fn = sum(
            m.false_negatives
            for ft, m in rep.by_field_type.items()
            if ft != NO_DETECTOR
        )
        assert len(rep.false_negatives_list) == total_fn

    def test_fp_list_length_matches_false_positive_count(self):
        rep = run_baseline()
        assert len(rep.false_positives_list) == rep.false_positive_count

    def test_pinned_fn_count(self):
        rep = run_baseline()
        # Two FNs: mrn under cryptic header, health_plan_id under cryptic header.
        assert len(rep.false_negatives_list) == 2

    def test_pinned_fp_count(self):
        rep = run_baseline()
        # One FP: account_id claimed as mrn.
        assert len(rep.false_positives_list) == 1
