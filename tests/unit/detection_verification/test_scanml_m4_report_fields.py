"""SCAN-ML M4: the report fields the model passes add, the classifier pass on the real bundled pack,
each model pass in its own `try`, and the detector log lines.

The classifier tests load `lgbm-v1` strictly (`load_detectors`), which every install carries. The
NER error case loads the real spaCy model and then makes the scorer raise; the regex findings must
survive beside one `error` row (AT-M4g, M-M4g2).
"""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd
import pytest

from tests._ner import needs_ner
from tests.unit.detection_verification import _scanml_m4_support as s

_FINDINGS = "decoy_engine.verification.postmask._model_findings"
_LOG = logging.getLogger("decoy_engine").getChild("detectors").name
N = 60


def _model_findings() -> Any:
    import importlib
    import importlib.util

    assert importlib.util.find_spec(_FINDINGS) is not None, "the model findings pass is missing"
    return importlib.import_module(_FINDINGS)


def _frames() -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    emails = [f"user{i}@example.com" for i in range(N)]
    people = pd.DataFrame({"email": emails, "amount": [float(i) for i in range(N)]})
    clean = pd.DataFrame({"code": ["REDACTED"] * N})
    return {"people": people, "clean": clean}, {"people": people.copy(), "clean": clean.copy()}


def _cfg(post_scan: dict[str, Any] | None = None, email: dict[str, Any] | None = None) -> dict:
    columns = [{"name": "email", **email}] if email else []
    return s.config(
        [s.table_config("people", columns), s.table_config("clean", [])], post_scan=post_scan
    )


def test_tables_checked_lists_every_output_table_clean_ones_included() -> None:
    sources, outputs = _frames()
    for detectors in (None, s.detector_set("classifier")):
        report = s.run_post_mask(sources, outputs, _cfg(), detectors)
        assert report.get("tables_checked") == ["people", "clean"], report.keys()
        assert not [f for f in report["residual_pii"] if f["table"] == "clean"]


def test_detector_runs_and_config_sha256_are_on_the_report() -> None:
    sources, outputs = _frames()
    plain = s.run_post_mask(sources, outputs, _cfg(), None)
    assert plain.get("detectors") == [{"source": "regex", "model": None}]
    assert "config_sha256" in plain and plain["config_sha256"] is None
    report = s.run_post_mask(
        sources, outputs, _cfg({"detectors": ["regex", "classifier"]}), s.detector_set("classifier")
    )
    assert report["detectors"] == [
        {"source": "regex", "model": None},
        {"source": "classifier", "model": {"id": "lgbm-v1", "version": "0.1.0"}},
    ]


def test_regex_findings_carry_the_regex_defaults() -> None:
    sources, outputs = _frames()
    report = s.run_post_mask(sources, outputs, _cfg(), s.detector_set("classifier"))
    regex = s.rows(report, "regex")
    assert regex, "the fixture must give a regex finding"
    for finding in regex:
        assert (finding["model"], finding["band"], finding["dismissed"], finding["promoted"]) == (
            None,
            None,
            False,
            False,
        )
        assert isinstance(finding["match_rate"], float)


def test_classifier_pass_on_the_real_pack_gives_a_band_and_no_rate() -> None:
    sources, outputs = _frames()
    report = s.run_post_mask(sources, outputs, _cfg(), s.detector_set("classifier"))
    found = s.rows(report, "classifier", column="email")
    assert len(found) == 1, report["residual_pii"]
    (finding,) = found
    assert finding["detector_id"] == "email"
    assert finding["band"] in ("high", "review")
    assert finding["match_rate"] is None
    assert finding["model"] == {"id": "lgbm-v1", "version": "0.1.0"}
    assert finding["severity"] == "warning"  # an unconfigured column
    assert finding["configured_strategy"] is None
    assert not s.rows(report, "classifier", table="clean")


def test_classifier_findings_on_kept_or_fake_values_are_info() -> None:
    sources, outputs = _frames()
    for strategy in (
        {"strategy": "passthrough"},
        {"strategy": "faker", "provider": "person_email"},
        {"strategy": "shuffle"},
    ):
        report = s.run_post_mask(
            sources, outputs, _cfg(email=strategy), s.detector_set("classifier")
        )
        (finding,) = s.rows(report, "classifier", column="email")
        assert finding["severity"] == "info", strategy


def test_no_classifier_pass_without_the_classifier_in_the_set() -> None:
    sources, outputs = _frames()
    report = s.run_post_mask(sources, outputs, _cfg(), s.detector_set())
    assert not s.rows(report, "classifier")


def test_a_raising_classifier_pass_keeps_the_regex_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources, outputs = _frames()
    cfg = _cfg({"detectors": ["regex", "classifier"]})
    detectors = s.detector_set("classifier")
    baseline = s.rows(s.run_post_mask(sources, outputs, cfg, None), "regex")
    mf = _model_findings()

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("secret cell value 123-45-6789")

    monkeypatch.setattr(mf, "classify_frame", boom)
    report = s.run_post_mask(sources, outputs, cfg, detectors)
    assert s.rows(report, "regex") == baseline
    errors = s.rows(report, "classifier", severity="error")
    assert len(errors) == 1
    assert errors[0]["model"] == {"id": "lgbm-v1", "version": "0.1.0"}
    assert "RuntimeError" in errors[0]["message"]
    assert "123-45-6789" not in str(report)
    assert report["error_count"] == 1


@needs_ner
def test_a_raising_ner_pass_keeps_the_regex_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    sources, outputs = _frames()
    outputs["people"]["notes"] = s.notes_column(2, n=N)
    sources["people"]["notes"] = outputs["people"]["notes"]
    cfg = _cfg({"detectors": ["regex", "ner"]})
    detectors = s.detector_set("ner")
    baseline = s.rows(s.run_post_mask(sources, outputs, cfg, None), "regex")
    assert baseline
    mf = _model_findings()

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("Ask Debra Archer")

    monkeypatch.setattr(mf, "ner_rates", boom)
    report = s.run_post_mask(sources, outputs, cfg, detectors)
    assert s.rows(report, "regex") == baseline, "the regex findings were replaced"
    errors = s.rows(report, "ner", severity="error")
    assert len(errors) == 1
    assert errors[0]["model"]["id"] == "en_core_web_sm"
    assert "ValueError" in errors[0]["message"]
    assert "Debra" not in str(report)


def test_detector_log_lines_name_runs_and_per_table_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sources, outputs = _frames()
    cfg = _cfg({"detectors": ["regex", "classifier"]})
    with caplog.at_level(logging.INFO, logger=_LOG):
        s.run_post_mask(sources, outputs, cfg, s.detector_set("classifier"))
    lines = [r.getMessage() for r in caplog.records if r.name == _LOG]
    assert lines and lines[0].startswith("post_scan.detectors sources=regex,classifier "), lines
    assert "classifier=lgbm-v1@0.1.0" in lines[0]
    assert "tables=2 columns=3 ner_findings=warn" in lines[0]
    assert (
        "post_scan.models table=people ner_findings=0 classifier_findings=1 dismissed=0 "
        "dismissed_stale=0"
    ) in lines
    assert (
        "post_scan.models table=clean ner_findings=0 classifier_findings=0 dismissed=0 "
        "dismissed_stale=0"
    ) in lines


def test_platform_call_without_detectors_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    sources, outputs = _frames()
    with caplog.at_level(logging.INFO, logger=_LOG):
        report = s.run_post_mask(sources, outputs, _cfg(), None)
    assert "tables_checked" in report
    assert not [r for r in caplog.records if r.name == _LOG]


def test_classifier_pass_reads_a_60_row_subsample_of_each_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from decoy_engine.profiling.features import builder

    seen: list[int] = []
    real = builder.build_column_features

    def spy(series: Any, name: str) -> Any:
        seen.append(len(series))
        return real(series, name)

    monkeypatch.setattr(builder, "build_column_features", spy)
    sources, outputs = _frames()
    small = s.run_post_mask(sources, outputs, _cfg(), s.detector_set("classifier"))
    tiled = {name: pd.concat([frame] * 170, ignore_index=True) for name, frame in outputs.items()}
    assert len(tiled["people"]) > 10_000
    seen.clear()
    large = s.run_post_mask(tiled, tiled, _cfg(), s.detector_set("classifier"))
    assert seen and max(seen) <= 60, seen
    pick = ("column", "detector_id", "band", "severity")
    assert [{k: f[k] for k in pick} for f in s.rows(large, "classifier")] == [
        {k: f[k] for k in pick} for f in s.rows(small, "classifier")
    ]
