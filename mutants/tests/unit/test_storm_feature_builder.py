"""Unit tests for the deterministic ML1 column feature builder (BF2).

Covers: determinism (byte-identical repeat), per-feature-family correctness
on crafted columns, JSON-serializability, and empty/all-null edge cases.
"""

import json

import pandas as pd

from decoy_engine.storm.features import (
    ColumnFeatures,
    build_column_features,
    tokenize_header,
)

# ── header tokenization ──────────────────────────────────────────────────────


class TestTokenizeHeader:
    def test_underscore_split(self):
        assert tokenize_header("order_id") == ["order", "id"]

    def test_camelcase_split(self):
        assert tokenize_header("patientMRN") == ["patient", "mrn"]

    def test_mixed_separators(self):
        assert tokenize_header("claim-service.date") == ["claim", "service", "date"]

    def test_opaque_header_stays_single_token(self):
        # No letter/digit split: cryptic headers stay intact and lowercased.
        assert tokenize_header("f07") == ["f07"]
        assert tokenize_header("C1") == ["c1"]
        assert tokenize_header("xref") == ["xref"]


# ── determinism ──────────────────────────────────────────────────────────────


class TestDeterminism:
    def test_repeat_is_byte_identical(self):
        df = pd.DataFrame(
            {"ssn": ["123-45-6789", "078-05-1120", "457-55-5462", None, "457-55-5462"]}
        )
        a = build_column_features(df["ssn"], "ssn").to_dict()
        b = build_column_features(df["ssn"], "ssn").to_dict()
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)

    def test_independent_of_series_name(self):
        s = pd.Series(["a", "b", "c"])
        # col_name drives header tokens, not series.name.
        feats = build_column_features(s, "device_id")
        assert feats.column_name == "device_id"
        assert feats.header_tokens == ["device", "id"]


# ── char-class fractions ─────────────────────────────────────────────────────


class TestCharClassFractions:
    def test_fractions_sum_to_one(self):
        s = pd.Series(["Ab1 .", "Cd2 -", "Ef3 _"])
        feats = build_column_features(s, "c")
        total = sum(feats.char_class_fractions.values())
        assert abs(total - 1.0) < 1e-6

    def test_pure_digit_column(self):
        s = pd.Series(["12345", "67890", "11111"])
        feats = build_column_features(s, "c")
        assert feats.char_class_fractions["digit"] == 1.0
        assert feats.char_class_fractions["upper"] == 0.0

    def test_empty_column_all_zero(self):
        s = pd.Series([None, None], dtype="object")
        feats = build_column_features(s, "c")
        assert all(v == 0.0 for v in feats.char_class_fractions.values())


# ── Shannon entropy ──────────────────────────────────────────────────────────


class TestEntropy:
    def test_constant_column_zero_entropy(self):
        s = pd.Series(["x"] * 10)
        feats = build_column_features(s, "c")
        assert feats.shannon_entropy == 0.0
        assert feats.normalized_entropy == 0.0

    def test_uniform_distribution_max_entropy(self):
        # 4 equally-likely values -> entropy = log2(4) = 2 bits, normalized 1.0.
        s = pd.Series(["a", "b", "c", "d"])
        feats = build_column_features(s, "c")
        assert abs(feats.shannon_entropy - 2.0) < 1e-6
        assert abs(feats.normalized_entropy - 1.0) < 1e-6

    def test_entropy_within_bounds(self):
        s = pd.Series(["a", "a", "b", "c", "c", "c", "d"])
        feats = build_column_features(s, "c")
        # 0 <= H <= log2(distinct); normalized in [0, 1].
        assert 0.0 <= feats.normalized_entropy <= 1.0
        assert 0.0 <= feats.shannon_entropy <= 2.0  # log2(4 distinct)


# ── checksum pass rates + regex signals ──────────────────────────────────────


class TestChecksumAndRegex:
    def test_valid_pans_pass_luhn(self):
        # Known Luhn-valid test PANs (Visa/MC test numbers).
        s = pd.Series(["4111111111111111", "5500005555555559", "4012888888881881"])
        feats = build_column_features(s, "xref")
        assert feats.checksum_pass_rates["luhn"] == 1.0
        # regex signal also fires (regex + luhn gate) even under opaque header.
        assert feats.regex_signals["pan"] == 1.0

    def test_invalid_pans_fail_luhn(self):
        # Same digit count, deliberately broken checksum.
        s = pd.Series(["4111111111111112", "5500005555555550"])
        feats = build_column_features(s, "xref")
        assert feats.checksum_pass_rates["luhn"] == 0.0
        assert feats.regex_signals["pan"] == 0.0

    def test_valid_iban_passes(self):
        # Canonical valid IBAN examples.
        s = pd.Series(["GB82WEST12345698765432", "DE89370400440532013000"])
        feats = build_column_features(s, "acct")
        assert feats.checksum_pass_rates["iban"] == 1.0

    def test_ssn_regex_signal_without_name_hint(self):
        s = pd.Series(["123-45-6789", "078-05-1120", "457-55-5462"])
        feats = build_column_features(s, "c1")  # opaque header
        assert feats.regex_signals["ssn"] == 1.0


# ── shape signature ──────────────────────────────────────────────────────────


class TestShapeSignature:
    def test_ssn_mask(self):
        s = pd.Series(["123-45-6789", "078-05-1120", "457-55-5462"])
        feats = build_column_features(s, "c")
        assert feats.shape.dominant_mask == "ddd-dd-dddd"
        assert feats.shape.dominant_mask_rate == 1.0
        assert feats.shape.min_length == 11
        assert feats.shape.max_length == 11

    def test_zip_mask(self):
        s = pd.Series(["02139", "90210", "10001"])
        feats = build_column_features(s, "c")
        assert feats.shape.dominant_mask == "ddddd"

    def test_long_freetext_excluded_from_mask_vote(self):
        long_val = "x" * 60
        s = pd.Series([long_val, long_val])
        feats = build_column_features(s, "notes")
        assert feats.shape.dominant_mask is None
        assert feats.shape.max_length == 60


# ── type / rates / classifiers ───────────────────────────────────────────────


class TestRatesAndType:
    def test_rates_and_type(self):
        s = pd.Series([1, 2, 3, None, 3], dtype="Float64")
        feats = build_column_features(s, "amount")
        assert feats.row_count == 5
        assert feats.non_null_count == 4
        assert feats.null_rate == 0.2
        assert feats.distinct_count == 3
        # distinct / row_count vs distinct / non_null
        assert feats.distinct_rate == 0.6
        assert feats.unique_rate == 0.75

    def test_numeric_range_class_money(self):
        s = pd.Series([10.50, 99.99, 4.25, 1200.00])
        feats = build_column_features(s, "amount")
        assert feats.numeric_range_class == "decimal_money"


# ── JSON-serializability + edge cases ────────────────────────────────────────


class TestSerializationAndEdges:
    def test_to_dict_is_json_serializable(self):
        s = pd.Series(["123-45-6789", "078-05-1120"])
        feats = build_column_features(s, "ssn")
        blob = json.dumps(feats.to_dict())
        restored = json.loads(blob)
        assert restored["column_name"] == "ssn"
        assert restored["shape"]["dominant_mask"] == "ddd-dd-dddd"

    def test_all_null_column(self):
        s = pd.Series([None, None, None], dtype="object")
        feats = build_column_features(s, "empty")
        assert feats.non_null_count == 0
        assert feats.sample_size == 0
        assert feats.shannon_entropy == 0.0
        assert feats.shape.dominant_mask is None
        # still JSON-serializable
        json.dumps(feats.to_dict())

    def test_empty_dataframe_column(self):
        s = pd.Series([], dtype="object")
        feats = build_column_features(s, "c")
        assert feats.row_count == 0
        assert feats.null_rate == 0.0
        assert isinstance(feats, ColumnFeatures)
        json.dumps(feats.to_dict())
