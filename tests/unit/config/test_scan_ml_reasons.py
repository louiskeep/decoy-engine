"""SCAN-ML M1: the six reason identities the slice owns are registered at registry 1.7.0 and pinned."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decoy_engine import reasons

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "f29" / "reason-manifest-v1.7-scanml-m1.json"
)
IDENTITIES = [
    ("config", "detectors_invalid"),
    ("config", "post_scan_inactive"),
    ("config", "ner_model_invalid"),
    ("config", "dismissal_invalid"),
    ("provider", "classifier_unavailable"),
    ("provider", "ner_model_incompatible"),
]


def test_registry_is_at_1_7_0() -> None:
    assert reasons.manifest()["registry_version"] == "1.7.0"


@pytest.mark.parametrize(("namespace", "code"), IDENTITIES)
def test_identity_is_registered_since_1_7_0(namespace: str, code: str) -> None:
    spec = reasons.find(namespace, code)
    assert spec is not None
    assert spec.since == "1.7.0"
    assert spec.rules[0].outcome.value == "configuration"
    assert spec.rules[0].retry.value == "configuration_repair"


def test_the_slice_adds_exactly_these_identities_and_no_later_slice_code() -> None:
    added = {
        (e["namespace"], e["code"]) for e in reasons.manifest()["entries"] if e["since"] == "1.7.0"
    }
    assert added == set(IDENTITIES)
    assert reasons.find("post_scan", "ner_blocked") is None  # M4's
    assert reasons.find("route", "post_scan_models_unsupported") is None  # M5's


def test_the_minor_is_pinned_in_its_own_fixture() -> None:
    assert FIXTURE.is_file(), "the 1.7 addition is pinned in reason-manifest-v1.7-scanml-m1.json"
    pinned = json.loads(FIXTURE.read_text())
    live = json.loads(json.dumps(reasons.manifest()))
    assert pinned["registry_version"] == "1.7.0"
    assert pinned["reason_schema_version"] == live["reason_schema_version"]
    assert pinned["entries"] == [e for e in live["entries"] if e["since"] == "1.7.0"]


def test_the_exceptions_map_to_their_reasons() -> None:
    from decoy_engine.detection.ner import NerModelNameError, NerUnavailableError
    from decoy_engine.reason_adapter import reason_from_exception

    name = reason_from_exception(NerModelNameError())
    assert (name.namespace, name.code) == ("config", "ner_model_invalid")
    incompatible = NerUnavailableError(code=reasons.provider.NER_MODEL_INCOMPATIBLE, message="m")
    record = reason_from_exception(incompatible)
    assert (record.namespace, record.code) == ("provider", "ner_model_incompatible")
