"""Unit tests for the FORECAST recommender.

These tests build StormProfiles directly (without running run_storm) so we
can isolate recommender behavior from the profiler. End-to-end coverage
lives in tests/integration/test_forecast_hipaa_golden.py.
"""

import yaml

from decoy_engine.forecast import recommend
from decoy_engine.storm.types import (
    DetectorMatch,
    FieldStats,
    SentinelFlag,
    StormProfile,
)


def _field(
    name: str,
    *,
    detectors: list[tuple[str, float]] | None = None,
    sentinels: list[SentinelFlag] | None = None,
    inferred: str = "string",
    unique: bool = True,
) -> FieldStats:
    # Plan B-2: FORECAST choosers branch on cardinality, so tests get an
    # explicit knob. ``unique=True`` (default) makes the field look like
    # an identifier (unique_rate=1.0, is_likely_unique=True) — matches
    # the pre-B-2 behavior of all helpers. ``unique=False`` makes it
    # look like a low-cardinality derived field so faker-style branches
    # are exercised.
    if unique:
        unique_rate = 1.0
        is_likely_unique = True
        distinct_count = 100
        value_set_size_class = "unique"
    else:
        unique_rate = 0.1
        is_likely_unique = False
        distinct_count = 10
        value_set_size_class = "low"
    return FieldStats(
        name=name,
        inferred_type=inferred,
        dtype_raw="object",
        row_count=100,
        null_count=0,
        null_rate=0.0,
        distinct_count=distinct_count,
        unique_rate=unique_rate,
        is_likely_unique=is_likely_unique,
        value_set_size_class=value_set_size_class,
        detector_matches=[DetectorMatch(detector_id=did, match_rate=rate) for did, rate in (detectors or [])],
        sentinels=sentinels or [],
    )


def _profile(*fields: FieldStats, qi_groups: list[list[str]] | None = None) -> StormProfile:
    return StormProfile(
        source_label="test.csv",
        row_count=100,
        sample_strategy="full",
        fields=list(fields),
        quasi_identifier_groups=qi_groups or [],
    )


# ── per-field recommendations ────────────────────────────────────────────────

class TestPerFieldRecommendations:
    def test_low_card_email_column_recommends_faker_email(self):
        # Low-cardinality email (e.g. a "customer_segment_email" lookup
        # column) → faker so the recommendation matches the column's
        # actual role rather than treating every email column as a PK.
        profile = _profile(_field("contact_segment", detectors=[("email", 1.0)], unique=False))
        report = recommend(profile)
        assert len(report.field_recommendations) == 1
        rec = report.field_recommendations[0]
        assert rec.field_name == "contact_segment"
        assert rec.recommended_mask == "faker"
        assert rec.mask_params == {"faker_type": "email"}
        assert rec.matched_detector == "email"

    def test_high_card_email_column_recommends_faker_email(self):
        # Email-shaped values should stay email-shaped in generated output.
        profile = _profile(_field("email", detectors=[("email", 1.0)], unique=True))
        report = recommend(profile)
        rec = report.field_recommendations[0]
        assert rec.recommended_mask == "faker"
        assert rec.mask_params == {"faker_type": "email"}

    def test_ssn_column_recommends_fpe(self):
        profile = _profile(_field("ssn", detectors=[("ssn", 1.0)]))
        report = recommend(profile)
        assert report.field_recommendations[0].recommended_mask == "fpe"

    def test_zip_column_recommends_redact_keep_3(self):
        profile = _profile(_field("zip", detectors=[("us_zip", 1.0)]))
        report = recommend(profile)
        rec = report.field_recommendations[0]
        assert rec.recommended_mask == "redact"
        assert rec.mask_params == {"keep_chars": 3}

    def test_field_with_no_detectors_gets_no_recommendation(self):
        profile = _profile(_field("user_id"))
        report = recommend(profile)
        assert report.field_recommendations == []


# ── Disguise ranking ─────────────────────────────────────────────────────────

class TestDisguiseRanking:
    def test_hipaa_outranks_default_when_ssn_present(self):
        # Profile with SSN + dates + ZIP + name + email + phone — both Disguises
        # match, but HIPAA has SSN as `any_detectors` plus the co-occurrence trio.
        profile = _profile(
            _field("ssn", detectors=[("ssn", 1.0)]),
            _field("first_name", detectors=[("person_name", 1.0)]),
            _field("dob", detectors=[("iso_date", 1.0)]),
            _field("zip", detectors=[("us_zip", 1.0)]),
            _field("email", detectors=[("email", 1.0)]),
            _field("phone", detectors=[("us_phone", 1.0)]),
        )
        report = recommend(profile)
        ids = [r.disguise_id for r in report.disguise_recommendations]
        assert ids[0] == "hipaa", f"expected hipaa first, got {ids}"
        assert "default" in ids
        # Top score is strictly greater than the next.
        scores = [r.match_score for r in report.disguise_recommendations]
        assert scores[0] > scores[1]

    def test_default_alone_when_only_basic_pii(self):
        # No SSN, no co-occurrence trio — HIPAA shouldn't fire.
        profile = _profile(
            _field("email", detectors=[("email", 1.0)]),
            _field("phone", detectors=[("us_phone", 1.0)]),
        )
        report = recommend(profile)
        ids = [r.disguise_id for r in report.disguise_recommendations]
        assert "hipaa" not in ids
        assert "default" in ids

    def test_no_recommendations_for_non_pii_dataset(self):
        # Just numeric IDs — no detectors fire.
        profile = _profile(
            _field("user_id", inferred="integer"),
            _field("amount", inferred="float"),
        )
        report = recommend(profile)
        assert report.disguise_recommendations == []

    def test_ranked_descending_score(self):
        profile = _profile(
            _field("ssn", detectors=[("ssn", 1.0)]),
            _field("first_name", detectors=[("person_name", 1.0)]),
            _field("dob", detectors=[("iso_date", 1.0)]),
            _field("zip", detectors=[("us_zip", 1.0)]),
            _field("email", detectors=[("email", 1.0)]),
            _field("phone", detectors=[("us_phone", 1.0)]),
        )
        report = recommend(profile)
        scores = [r.match_score for r in report.disguise_recommendations]
        assert scores == sorted(scores, reverse=True)


# ── apply payload structure ──────────────────────────────────────────────────

class TestApplyPayload:
    def test_apply_payload_has_field_masks_in_pipeline_shape(self):
        profile = _profile(_field("ssn", detectors=[("ssn", 1.0)]))
        report = recommend(profile)
        top = report.disguise_recommendations[0]
        masks = top.apply_payload["field_masks"]
        assert all("column" in m and "type" in m for m in masks)

    def test_apply_payload_includes_disguise_id(self):
        profile = _profile(_field("ssn", detectors=[("ssn", 1.0)]))
        report = recommend(profile)
        top = report.disguise_recommendations[0]
        assert top.apply_payload["disguise_id"] == top.disguise_id


# ── risk flags from sentinels ────────────────────────────────────────────────

class TestRiskFlags:
    def test_date_sentinel_surfaces_as_risk_flag_with_fixes(self):
        s = SentinelFlag(kind="date_sentinel", value="0001-01-01", count=2, note="placeholder")
        profile = _profile(_field("dob", sentinels=[s]))
        report = recommend(profile)
        assert len(report.risk_flags) == 1
        rf = report.risk_flags[0]
        assert rf.field_name == "dob"
        assert rf.kind == "date_sentinel"
        assert rf.fix_options  # populated from _FIX_OPTIONS

    def test_no_sentinels_means_no_risk_flags(self):
        profile = _profile(_field("clean_field"))
        report = recommend(profile)
        assert report.risk_flags == []


# ── proposed pipeline YAML ───────────────────────────────────────────────────

class TestProposedYAML:
    def test_yaml_parses_back_to_dict(self):
        profile = _profile(_field("email", detectors=[("email", 1.0)]))
        report = recommend(profile)
        cfg = yaml.safe_load(report.proposed_pipeline_yaml)
        assert cfg["mode"] == "graph"
        assert any(n["kind"] == "source.file" for n in cfg["nodes"])
        assert any(n["kind"] == "mask" for n in cfg["nodes"])
        assert any(n["kind"] == "target.file" for n in cfg["nodes"])
        assert "edges" in cfg

    def test_yaml_uses_top_disguise_rules_when_disguise_recommended(self):
        profile = _profile(
            _field("ssn", detectors=[("ssn", 1.0)]),
            _field("first_name", detectors=[("person_name", 1.0)]),
        )
        report = recommend(profile)
        cfg = yaml.safe_load(report.proposed_pipeline_yaml)
        mask_node = next(n for n in cfg["nodes"] if n["kind"] == "mask")
        columns = mask_node["config"]["columns"]
        # Each column entry has strategy + (optional) params, no _why hints.
        for col_name, col_cfg in columns.items():
            assert "strategy" in col_cfg, f"missing strategy key in column {col_name!r}"
            assert not any(k.startswith("_") for k in col_cfg.keys())

    def test_source_label_appears_in_yaml_stub(self):
        profile = _profile(_field("email", detectors=[("email", 1.0)]))
        profile.source_label = "patients_2026Q1.csv"
        report = recommend(profile)
        assert "patients_2026Q1.csv" in report.proposed_pipeline_yaml


# ── output is JSON-serializable ──────────────────────────────────────────────

def test_forecast_report_is_json_serializable():
    import json
    profile = _profile(_field("ssn", detectors=[("ssn", 1.0)]))
    report = recommend(profile)
    json.dumps(report.to_dict())
