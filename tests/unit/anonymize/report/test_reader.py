"""The reader is derived from the writer: strict numbers, a required tag, tolerant minors."""

from __future__ import annotations

import copy
import json
from typing import Any, get_args, get_origin

import pandas as pd
import pytest
from pydantic import BaseModel, ValidationError

from decoy_engine.anonymize.report import (
    RiskUtilityReport,
    RiskUtilityReportRead,
    build_risk_utility_report,
)
from decoy_engine.quality.anonymity import KAnonymity, RecursiveCLDiversity

from ._helpers import release, table


def _payload() -> dict[str, Any]:
    frame = table()
    lattice, result, released = release(frame, (KAnonymity(2), RecursiveCLDiversity("dx", 2, 1)))
    population = pd.concat([frame, frame], ignore_index=True)
    return build_risk_utility_report(
        released, lattice, result, risk_threshold=0.2, population=population
    ).to_payload()


def _models(model: type[BaseModel]) -> dict[str, set[str]]:
    """Field names per nested model, keyed by the path that reaches it."""
    out: dict[str, set[str]] = {}

    def walk(current: type[BaseModel], path: str) -> None:
        out[path] = set(current.model_fields)
        for name, field in current.model_fields.items():
            stack = [field.annotation]
            while stack:
                ann = stack.pop()
                if isinstance(ann, type) and issubclass(ann, BaseModel):
                    walk(ann, f"{path}.{name}")
                elif get_origin(ann) is not None:
                    stack.extend(a for a in get_args(ann) if a is not Ellipsis)

    walk(model, "")
    return out


def test_reader_fields_equal_writer_fields_at_every_level() -> None:
    assert _models(RiskUtilityReportRead) == _models(RiskUtilityReport)


def test_round_trip() -> None:
    payload = _payload()
    read = RiskUtilityReportRead.model_validate(payload)
    assert read.to_payload() == payload
    assert RiskUtilityReportRead.model_validate(json.loads(json.dumps(payload))).to_payload() == payload
    assert RiskUtilityReport.model_validate(payload).to_payload() == payload


def _both_refuse(doc: dict[str, Any]) -> None:
    for model in (RiskUtilityReport, RiskUtilityReportRead):
        with pytest.raises(ValidationError):
            model.model_validate(doc)


def test_another_major_is_refused() -> None:
    doc = _payload()
    doc["schema"] = "risk-utility-report/v2"
    _both_refuse(doc)
    doc["schema"] = "risk-utility-report/v1\n"
    _both_refuse(doc)


def test_a_missing_tag_is_refused_on_read() -> None:
    doc = _payload()
    del doc["schema"]
    with pytest.raises(ValidationError):
        RiskUtilityReportRead.model_validate(doc)


@pytest.mark.parametrize(
    "path",
    [("method",), ("search", "metric"), ("risk", "population"), ("assumptions", 0),
     ("not_established", 0), ("models", 0, "kind")],
)
def test_free_text_in_a_token_is_refused(path: tuple[Any, ...]) -> None:
    doc = _payload()
    target = doc
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = "free text, not a token"
    _both_refuse(doc)


def test_a_later_minor_token_and_an_added_field_read() -> None:
    doc = _payload()
    doc["method"] = "local_recoding"
    doc["assumptions"] = [*doc["assumptions"], "a_later_assumption"]
    doc["added_in_a_later_minor"] = {"anything": 1}
    doc["risk"]["added_risk_figure"] = 0.5
    read = RiskUtilityReportRead.model_validate(doc)
    assert read.method == "local_recoding"
    with pytest.raises(ValidationError):
        RiskUtilityReport.model_validate(doc)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("suppression", "rows_input"), True),
        (("suppression", "rows_input"), "12"),
        (("suppression", "rows_input"), -1),
        (("node", 0), False),
        (("risk", "prosecutor", "highest"), "0.2"),
        (("risk", "prosecutor", "highest"), float("nan")),
        (("risk", "prosecutor", "highest"), 1.5),
        (("risk", "prosecutor", "average"), -0.1),
        (("risk", "risk_threshold"), 0.0),
        (("risk", "risk_threshold"), True),
        (("utility", "loss_metric"), 1.01),
        (("utility", "non_uniform_entropy"), -1.0),
        (("utility", "discernibility"), 1.0),
        (("suppression", "suppression_limit"), 2.0),
        (("models", 0, "k"), 0),
        (("models", 1, "c"), 0.0),
        (("models", 1, "l"), 0),
        (("models", 0, "met"), 1),
        (("models", 0, "met"), "true"),
    ],
)
def test_typed_numeric_leaves(path: tuple[Any, ...], value: Any) -> None:
    doc = copy.deepcopy(_payload())
    target: Any = doc
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    _both_refuse(doc)


def test_t_must_be_in_the_unit_interval() -> None:
    doc = _payload()
    doc["models"][1]["t"] = 1.5
    _both_refuse(doc)
