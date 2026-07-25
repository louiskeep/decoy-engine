"""ML1 x ML0 separability smoke test (BF2).

Numbers-don't-lie artifact: run the deterministic feature builder over the
labeled fixtures and show the feature vectors are measurably distinct
across field types - in particular that structural PII hiding under opaque
headers (a PAN in column ``xref``, an SSN in column ``c1``) still lights up
the checksum / regex / shape features even though its header carries no
hint. This motivates a downstream column classifier (ML2/ML3) WITHOUT
shipping a model.

Honest scope: name-hint-only identifiers (MRN, health-plan ID) have no
content structure, so under an opaque header neither the regex baseline NOR
these content features can recover them - that genuine ceiling is asserted
too, so the artifact does not overclaim.
"""

from decoy_engine.storm.eval import build_fixtures
from decoy_engine.storm.features import build_column_features


def _all_features() -> dict[tuple[str, str], object]:
    out = {}
    for fx in build_fixtures():
        for col in fx.df.columns:
            out[(fx.name, col)] = build_column_features(fx.df[col], col)
    return out


def _content_evidence(feats) -> float:
    """Max single content signal: best regex match-rate or checksum pass-rate."""
    return max(
        max(feats.regex_signals.values(), default=0.0),
        max(feats.checksum_pass_rates.values(), default=0.0),
    )


class TestCrypticHeaderStillVisible:
    def test_pan_under_opaque_header_shows_checksum(self):
        feats = _all_features()[("cryptic_header", "xref")]
        # Header gives nothing - single opaque token, no detector name in it.
        assert feats.header_tokens == ["xref"]
        # ...but the content features expose it: full Luhn pass + PAN regex.
        assert feats.checksum_pass_rates["luhn"] == 1.0
        assert feats.regex_signals["pan"] == 1.0
        assert feats.shape.dominant_mask == "d" * 16

    def test_ssn_under_opaque_header_shows_shape(self):
        feats = _all_features()[("cryptic_header", "c1")]
        assert feats.header_tokens == ["c1"]
        assert feats.regex_signals["ssn"] == 1.0
        assert feats.shape.dominant_mask == "ddd-dd-dddd"

    def test_email_under_opaque_header_shows_regex(self):
        feats = _all_features()[("cryptic_header", "f07")]
        assert feats.regex_signals["email"] == 1.0


class TestSeparability:
    def test_structural_pii_separates_from_business_keys(self):
        feats = _all_features()
        # Structural PII (checksum/regex-bearing) -> strong content evidence.
        for key in [
            ("pci", "pan"),
            ("pci", "iban"),
            ("hipaa", "npi"),
            ("cryptic_header", "c1"),
            ("cryptic_header", "f07"),
            ("cryptic_header", "xref"),
        ]:
            assert _content_evidence(feats[key]) >= 0.99, key
        # Non-PII business identifiers -> zero content evidence.
        for key in [
            ("account_order", "account_id"),
            ("claim", "claim_id"),
            ("claim", "claim_amount"),
        ]:
            assert _content_evidence(feats[key]) == 0.0, key

    def test_opaque_pan_and_business_key_are_distinguishable(self):
        feats = _all_features()
        pan = feats[("cryptic_header", "xref")]
        acct = feats[("account_order", "account_id")]
        # Both are digit columns, yet the vectors differ decisively.
        assert pan.to_dict() != acct.to_dict()
        assert pan.checksum_pass_rates["luhn"] != acct.checksum_pass_rates["luhn"]

    def test_shape_signatures_are_distinct_across_types(self):
        feats = _all_features()
        masks = {
            feats[("pci", "pan")].shape.dominant_mask,
            feats[("cryptic_header", "c1")].shape.dominant_mask,
            feats[("claim", "service_date")].shape.dominant_mask,
            feats[("account_order", "account_id")].shape.dominant_mask,
        }
        # Four field types -> four distinct dominant masks.
        assert len(masks) == 4

    def test_name_hint_only_pii_has_no_content_ceiling(self):
        # Honest ceiling: MRN / health-plan-id under opaque headers have no
        # content structure, so content features cannot recover them either.
        feats = _all_features()
        for key in [("cryptic_header", "q9"), ("cryptic_header", "z3")]:
            assert _content_evidence(feats[key]) == 0.0, key
