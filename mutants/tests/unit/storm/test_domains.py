"""Item 1 (gap-closure): semantic Domain enum over detector IDs.

Every registered detector maps to exactly one Domain; the mapping is total
and derived (never persisted). `domain` rides on DetectorMatch (and the
winning detector's domain on FieldStats) so the platform/web can group and
filter by semantic type instead of 25 ad-hoc detector strings.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.storm.detectors import (
    REGISTERED_DETECTORS,
    detect_email,
    detect_pan,
    detect_ssn,
)
from decoy_engine.storm.domains import (
    DOMAIN_BY_DETECTOR,
    Domain,
    domain_for,
    registered_detector_ids,
)
from decoy_engine.storm.profiler import run_storm


def _registered_ids() -> set[str]:
    return {fn.__name__.removeprefix("detect_") for fn in REGISTERED_DETECTORS}


class TestDomainTable:
    def test_mapping_total_over_registered_detectors(self) -> None:
        missing = _registered_ids() - set(DOMAIN_BY_DETECTOR)
        assert not missing, f"detectors with no domain: {sorted(missing)}"

    def test_registered_detector_ids_matches_registry(self) -> None:
        assert registered_detector_ids() == frozenset(_registered_ids())

    def test_every_registered_id_maps_to_a_domain_member(self) -> None:
        for det_id in _registered_ids():
            assert isinstance(domain_for(det_id), Domain)

    def test_unknown_id_returns_other(self) -> None:
        assert domain_for("custom__uk_nhs_number") is Domain.OTHER
        assert domain_for("") is Domain.OTHER

    def test_each_non_other_domain_has_at_least_one_detector(self) -> None:
        used = {DOMAIN_BY_DETECTOR[i] for i in _registered_ids()}
        for member in Domain:
            if member is Domain.OTHER:
                continue
            assert member in used, f"{member} has no registered detector"

    def test_domain_is_a_str_enum_for_json(self) -> None:
        # str-Enum so dataclasses.asdict yields the bare string, not a repr.
        assert Domain.IDENTITY.value == "IDENTITY"
        assert isinstance(Domain.IDENTITY, str)


class TestDetectorMatchCarriesDomain:
    def test_ssn_match_is_identity(self) -> None:
        s = pd.Series([f"{500 + i:03d}-{10 + i:02d}-{1000 + i:04d}" for i in range(20)])
        m = detect_ssn(s, "ssn")
        assert m is not None
        assert m.domain == "IDENTITY"

    def test_email_match_is_contact(self) -> None:
        s = pd.Series([f"user{i}@example.com" for i in range(20)])
        m = detect_email(s, "email")
        assert m is not None
        assert m.domain == "CONTACT"

    def test_pan_match_is_financial(self) -> None:
        s = pd.Series(["4111111111111111", "4012888888881881"] * 10)
        m = detect_pan(s, "pan")
        assert m is not None
        assert m.domain == "FINANCIAL"


class TestFieldStatsDomain:
    def test_winning_domain_on_field(self) -> None:
        df = pd.DataFrame(
            {"ssn": [f"{500 + i:03d}-{10 + i:02d}-{1000 + i:04d}" for i in range(20)]}
        )
        profile = run_storm(df, "people.csv")
        field = {f.name: f for f in profile.fields}["ssn"]
        assert field.domain == "IDENTITY"
