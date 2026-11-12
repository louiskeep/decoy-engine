"""The seven reason identities this slice owns are registered at registry 1.8.0 and pinned."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decoy_engine import reasons

FIXTURE = (
    Path(__file__).resolve().parents[2] / "fixtures" / "f29" / "reason-manifest-v1.8-scanml-m1.json"
)
IDENTITIES = [
    ("config", "detectors_invalid"),
    ("config", "post_scan_inactive"),
    ("config", "pre_scan_ner_inactive"),
    ("config", "ner_model_invalid"),
    ("config", "dismissal_invalid"),
    ("provider", "classifier_unavailable"),
    ("provider", "ner_model_incompatible"),
]


def test_registry_is_at_least_1_8_0() -> None:
    version = tuple(int(part) for part in reasons.manifest()["registry_version"].split("."))
    assert version >= (1, 8, 0)


@pytest.mark.parametrize(("namespace", "code"), IDENTITIES)
def test_identity_is_registered_since_1_7_0(namespace: str, code: str) -> None:
    spec = reasons.find(namespace, code)
    assert spec is not None
    assert spec.since == "1.8.0"
    assert spec.rules[0].outcome.value == "configuration"
    assert spec.rules[0].retry.value == "configuration_repair"


def test_the_slice_adds_exactly_these_identities_and_no_later_slice_code() -> None:
    added = {
        (e["namespace"], e["code"]) for e in reasons.manifest()["entries"] if e["since"] == "1.8.0"
    }
    assert added == set(IDENTITIES)
    m4 = reasons.find("post_scan", "ner_blocked")  # M4's, in its own later minor
    assert m4 is not None and m4.since == "1.26.0"
    m5 = reasons.find("route", "post_scan_models_unsupported")  # M5's, in its own later minor
    assert m5 is not None and m5.since == "1.25.0"


def test_the_minor_is_pinned_in_its_own_fixture() -> None:
    assert FIXTURE.is_file(), "the 1.8 addition is pinned in reason-manifest-v1.8-scanml-m1.json"
    pinned = json.loads(FIXTURE.read_text())
    live = json.loads(json.dumps(reasons.manifest()))
    assert pinned["registry_version"] == "1.8.0"
    assert pinned["reason_schema_version"] == live["reason_schema_version"]
    assert pinned["entries"] == [e for e in live["entries"] if e["since"] == "1.8.0"]


def test_the_exceptions_map_to_their_reasons() -> None:
    from decoy_engine.detection.ner import NerModelNameError, NerUnavailableError
    from decoy_engine.reason_adapter import reason_from_exception

    name = reason_from_exception(NerModelNameError())
    assert (name.namespace, name.code) == ("config", "ner_model_invalid")
    incompatible = NerUnavailableError(code=reasons.provider.NER_MODEL_INCOMPATIBLE, message="m")
    record = reason_from_exception(incompatible)
    assert (record.namespace, record.code) == ("provider", "ner_model_incompatible")


def test_a_bad_ner_model_reaches_consumers_as_its_registered_config_reason() -> None:
    from decoy_engine.plan import PlanCompileError
    from decoy_engine.plan._checks import check_text_redact_ner_available
    from decoy_engine.reason_adapter import reason_from_exception

    config = {
        "tables": [
            {
                "name": "p",
                "columns": [
                    {
                        "name": "n",
                        "strategy": "text_redact",
                        "provider_config": {"ner": {"model": "../x"}},
                    }
                ],
            }
        ]
    }
    with pytest.raises(PlanCompileError) as caught:
        check_text_redact_ner_available(config)
    record = reason_from_exception(caught.value)
    assert (record.namespace, record.code) == ("config", "ner_model_invalid")


def test_ner_model_not_installed_maps_to_its_registered_provider_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.detection import ner
    from decoy_engine.plan import PlanCompileError
    from decoy_engine.plan._checks import check_text_redact_ner_available
    from decoy_engine.reason_adapter import reason_from_exception

    monkeypatch.setattr(ner, "spacy_installed", lambda: True)
    config = {
        "tables": [
            {
                "name": "p",
                "columns": [
                    {
                        "name": "n",
                        "strategy": "text_redact",
                        "provider_config": {"ner": {"model": "no_such_model_pkg"}},
                    }
                ],
            }
        ]
    }
    with pytest.raises(PlanCompileError) as caught:
        check_text_redact_ner_available(config)
    record = reason_from_exception(caught.value)
    assert (record.namespace, record.code) == ("provider", "ner_model_not_installed")


def test_a_family_exception_cannot_claim_an_unregistered_identity() -> None:
    from decoy_engine.plan import PlanCompileError
    from decoy_engine.reason_adapter import reason_from_exception

    record = reason_from_exception(PlanCompileError("not_a_real_code", None, "m"))
    assert (record.namespace, record.code) == ("decoy", "unregistered_reason")


@pytest.mark.parametrize(
    "symbol", [reasons.plan.SORT_UNSUPPORTED_TYPE, reasons.plan.DEDUPE_UNSUPPORTED_TYPE]
)
def test_a_code_registered_in_both_namespaces_keeps_the_family_record(
    symbol: reasons.ReasonCode,
) -> None:
    from decoy_engine.execution._transforms import TransformError
    from decoy_engine.reason_adapter import reason_from_exception

    record = reason_from_exception(TransformError(code=symbol, message="m"))
    assert (record.namespace, record.code) == ("runtime", str(symbol))
