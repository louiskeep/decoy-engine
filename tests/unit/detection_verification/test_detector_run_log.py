"""One INFO record per phase on `decoy_engine.detectors`, names and counts only."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from decoy_engine.detection import _model_status as ms
from decoy_engine.detection.types import DetectorRun, ModelRef

LOGGER = logging.getLogger("decoy_engine").getChild("detectors").name
RUNS = (
    DetectorRun("regex", None),
    DetectorRun("classifier", ModelRef("lgbm-v1", "0.1.0")),
    DetectorRun("ner", ModelRef("en_core_web_sm", "3.8.0")),
)


@pytest.fixture(autouse=True)
def _spacy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ms, "_spacy_version", lambda: "3.8.15")


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER]


def test_pre_scan_line_names_sources_models_versions_and_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        ms.log_detector_run("pre_scan", RUNS, {"tables": 3, "columns": 41})
    records = _records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == (
        "pre_scan.detectors sources=regex,classifier,ner classifier=lgbm-v1@0.1.0 "
        "ner=en_core_web_sm@3.8.0 spacy=3.8.15 tables=3 columns=41"
    )


def test_post_scan_line_with_regex_and_ner_only(caplog: pytest.LogCaptureFixture) -> None:
    runs = (RUNS[0], RUNS[2])
    with caplog.at_level(logging.INFO, logger=LOGGER):
        ms.log_detector_run("post_scan", runs, {"tables": 1, "columns": 7})
    assert len(_records(caplog)) == 1
    record = _records(caplog)[0]
    assert record.getMessage() == (
        "post_scan.detectors sources=regex,ner ner=en_core_web_sm@3.8.0 spacy=3.8.15 "
        "tables=1 columns=7"
    )


def test_regex_only_line_has_no_model_fields(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        ms.log_detector_run("pre_scan", (RUNS[0],), {"tables": 2, "columns": 5})
    assert len(_records(caplog)) == 1
    record = _records(caplog)[0]
    assert record.getMessage() == "pre_scan.detectors sources=regex tables=2 columns=5"


def test_one_record_per_phase_call_and_nothing_else_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        ms.log_detector_run("pre_scan", RUNS, {"tables": 1, "columns": 1})
        ms.log_detector_run("post_scan", RUNS, {"tables": 1, "columns": 1})
    assert [r.getMessage().split(" ", 1)[0] for r in _records(caplog)] == [
        "pre_scan.detectors",
        "post_scan.detectors",
    ]
    assert [r for r in caplog.records if r.name != LOGGER] == []


def test_a_bound_context_gets_one_step_event_with_the_same_name() -> None:
    steps: list[tuple[str, dict[str, Any]]] = []

    class Logger:
        def step(self, name: str, **kw: Any) -> None:
            steps.append((name, kw))

        def info(self, *_a: Any, **_k: Any) -> None:
            raise AssertionError("narrative lines go through the engine logger, not the job logger")

    ctx = type("Ctx", (), {"logger": Logger()})()
    ms.log_detector_run("post_scan", RUNS, {"tables": 1, "columns": 1}, ctx=ctx)
    assert [name for name, _ in steps] == ["post_scan.detectors"]
    assert all(v is None or isinstance(v, str) for v in steps[0][1].values())


def test_no_context_means_no_step_event(caplog: pytest.LogCaptureFixture) -> None:
    ms.log_detector_run("pre_scan", RUNS, {"tables": 1, "columns": 1}, ctx=None)


def test_a_phase_outside_the_two_scans_is_refused() -> None:
    with pytest.raises(ValueError):
        ms.log_detector_run("mask", RUNS, {"tables": 1, "columns": 1})


def test_the_logger_is_named_decoy_engine_detectors() -> None:
    assert ms._log is logging.getLogger("decoy_engine").getChild("detectors")
    assert ms._log.parent is logging.getLogger("decoy_engine")


@pytest.mark.parametrize(
    "run",
    [
        DetectorRun("ner", ModelRef("en_core_web_sm\nINJECTED", "3.8.0")),
        DetectorRun("ner", ModelRef("en_core_web_sm", "3.8.0 secret=1")),
        DetectorRun("classifier", ModelRef("lgbm v1", "0.1.0")),
    ],
)
def test_model_id_and_version_must_be_identifiers_too(
    run: DetectorRun, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(ValueError):
        ms.log_detector_run("pre_scan", (RUNS[0], run), {"tables": 1, "columns": 1})
    assert _records(caplog) == []
