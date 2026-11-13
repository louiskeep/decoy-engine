"""AT-M4c: a dismissal lowers only the warning whose every field and the column's strategy match.

Classifier cases run on the real bundled pack; one NER case runs on the real spaCy model. A
dismissal that changes the strategy, the model version, the source or the detector id stops
applying: the finding reports at its own severity and the run log counts one stale dismissal for
the table. A regex `fail` and a source-identity warning are never lowered.
Mutation M-M4c (match without the strategy) turns the strategy case red.
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import pytest

from tests._ner import needs_ner
from tests.unit.detection_verification import _scanml_m4_support as s

_LOG = logging.getLogger("decoy_engine").getChild("detectors").name
N = 60
EMAILS = [f"user{i}@example.com" for i in range(N)]
OTHER_EMAILS = [f"person{i}@example.org" for i in range(N)]

CLASSIFIER = {
    "table": "t",
    "column": "email",
    "detector_id": "email",
    "source": "classifier",
    "model_id": "lgbm-v1",
    "model_version": "0.1.0",
    "strategy": None,
}


def _run(
    dismissals: list[dict[str, Any]],
    *,
    caplog: pytest.LogCaptureFixture | None = None,
    output: list[str] | None = None,
    columns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source = pd.DataFrame({"email": EMAILS})
    out = pd.DataFrame({"email": output if output is not None else OTHER_EMAILS})
    post_scan = {"detectors": ["regex", "classifier"], "dismissed": dismissals}
    cfg = s.config([s.table_config("t", columns or [])], post_scan=post_scan)
    if caplog is None:
        return s.run_post_mask({"t": source}, {"t": out}, cfg, s.detector_set("classifier"))
    with caplog.at_level(logging.INFO, logger=_LOG):
        return s.run_post_mask({"t": source}, {"t": out}, cfg, s.detector_set("classifier"))


def _models_line(caplog: pytest.LogCaptureFixture) -> str:
    lines = [r.getMessage() for r in caplog.records if r.name == _LOG]
    found = [line for line in lines if line.startswith("post_scan.models table=t ")]
    assert len(found) == 1, lines
    return found[0]


def test_a_matching_dismissal_lowers_the_classifier_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    report = _run([CLASSIFIER], caplog=caplog)
    (finding,) = s.rows(report, "classifier", column="email")
    assert (finding["severity"], finding["dismissed"]) == ("info", True)
    assert "dismissed=1 dismissed_stale=0" in _models_line(caplog)


@pytest.mark.parametrize(
    "change",
    [
        {"strategy": "passthrough"},
        {"model_version": "0.0.9"},
        {"source": "ner", "model_id": "en_core_web_sm", "model_version": "3.8.0"},
        {"detector_id": "ssn"},
    ],
    ids=["strategy", "model_version", "source", "detector_id"],
)
def test_a_changed_field_makes_the_finding_report_again(
    change: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    report = _run([{**CLASSIFIER, **change}], caplog=caplog)
    (finding,) = s.rows(report, "classifier", column="email")
    assert (finding["severity"], finding["dismissed"]) == ("warning", False)
    assert "dismissed=0 dismissed_stale=1" in _models_line(caplog)


def test_a_dismissal_matches_the_current_strategy_of_the_column() -> None:
    entry = {"name": "email", "strategy": "hash", "namespace": "e"}
    stale = _run([CLASSIFIER], columns=[entry])
    (finding,) = s.rows(stale, "classifier", column="email")
    assert (finding["severity"], finding["dismissed"]) == ("warning", False)
    fresh = _run([{**CLASSIFIER, "strategy": "hash"}], columns=[entry])
    (finding,) = s.rows(fresh, "classifier", column="email")
    assert (finding["severity"], finding["dismissed"]) == ("info", True)


def test_a_regex_warning_can_be_dismissed() -> None:
    regex = {**CLASSIFIER, "source": "regex", "model_id": None, "model_version": None}
    before = s.rows(_run([]), "regex", column="email")
    assert [f["severity"] for f in before] == ["warning"], before
    (finding,) = s.rows(_run([regex]), "regex", column="email")
    assert (finding["severity"], finding["dismissed"]) == ("info", True)


def test_a_regex_fail_is_never_lowered() -> None:
    regex = {**CLASSIFIER, "source": "regex", "model_id": None, "model_version": None}
    (finding,) = s.rows(_run([regex], output=EMAILS), "regex", column="email")
    assert (finding["severity"], finding["dismissed"]) == ("fail", False)


def test_a_source_identity_warning_is_never_lowered() -> None:
    # A faker column that kept 10% of its source values: a warning raised from identity evidence.
    output = [EMAILS[i] if i % 10 == 0 else OTHER_EMAILS[i] for i in range(N)]
    entry = {"name": "email", "strategy": "faker", "provider": "person_email"}
    regex = {**CLASSIFIER, "source": "regex", "model_id": None, "model_version": None}
    report = _run([{**regex, "strategy": "faker"}], output=output, columns=[entry])
    (finding,) = s.rows(report, "regex", column="email")
    assert finding["source_identity_rate"] >= 0.05
    assert (finding["severity"], finding["dismissed"]) == ("warning", False)


@needs_ner
def test_an_ner_dismissal_for_an_old_model_version_stops_applying() -> None:
    notes = s.notes_column(2)
    frame = pd.DataFrame({"notes": notes})
    detectors = s.detector_set("ner")
    dismissal = {
        "table": "t",
        "column": "notes",
        "detector_id": "person_name",
        "source": "ner",
        "model_id": detectors.ner.id,
        "model_version": detectors.ner.version,
        "strategy": None,
    }

    def run(entry: dict[str, Any]) -> dict[str, Any]:
        post_scan = {"detectors": ["regex", "ner"], "ner_findings": "fail", "dismissed": [entry]}
        cfg = s.config([s.table_config("t", [])], post_scan=post_scan)
        report = s.run_post_mask({"t": frame.copy()}, {"t": frame}, cfg, detectors)
        (finding,) = s.rows(report, "ner", column="notes", detector_id="person_name")
        return finding

    current = run(dismissal)
    assert (current["severity"], current["dismissed"], current["promoted"]) == ("info", True, False)
    old = run({**dismissal, "model_version": "3.7.0"})
    assert (old["severity"], old["dismissed"], old["promoted"]) == ("fail", False, True)


def test_the_regex_only_hook_honours_dismissals() -> None:
    """`run_post_mask` with `detectors=None` (the platform hook) applies `post_scan.dismissed`."""
    regex = {**CLASSIFIER, "source": "regex", "model_id": None, "model_version": None}
    source = pd.DataFrame({"email": EMAILS})
    out = pd.DataFrame({"email": OTHER_EMAILS})

    def run(dismissals: list[dict[str, Any]]) -> dict[str, Any]:
        cfg = s.config([s.table_config("t", [])], post_scan={"dismissed": dismissals})
        return s.run_post_mask({"t": source}, {"t": out}, cfg, None)

    (before,) = s.rows(run([]), "regex", column="email")
    assert (before["severity"], before["dismissed"]) == ("warning", False)
    report = run([regex])
    (after,) = s.rows(report, "regex", column="email")
    assert (after["severity"], after["dismissed"]) == ("info", True)
    assert not s.rows(report, "classifier") and not s.rows(report, "ner")
    assert report["detectors"] == [{"source": "regex", "model": None}]
