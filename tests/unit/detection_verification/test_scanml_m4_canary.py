"""AT-M4d: the canary corpus on the post-scan path, with the real spaCy model and classifier pack.

The output cells hold a canary person name spaCy tags as PERSON, a canary token and a canary email
(`_scanml_m4_support.canary_notes`). Each test first proves the markers are in the input and that
the pass made a model finding and wrote a `post_scan.models` record, then proves no marker reached
the report, a log record, the engine log file, a progress event or the blocked-run error.
Mutation M-M4d (put a span in a finding's `message`) turns these red.
"""

from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from tests._ner import needs_ner
from tests.unit.detection_verification import _scanml_m4_support as s

_LOG = logging.getLogger("decoy_engine").getChild("detectors").name


def _frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    notes = s.canary_notes()
    emails = [s.CANARY_EMAIL if i % 3 == 0 else f"user{i}@example.com" for i in range(len(notes))]
    frame = pd.DataFrame({"notes": notes, "email": emails})
    return frame, frame.copy()


def _assert_markers_in(cells: list[str]) -> None:
    text = "\n".join(cells)
    for marker in s.CANARIES:
        assert marker in text, marker


def _assert_clean(where: str, text: str) -> None:
    for marker in s.CANARIES:
        assert marker not in text, (where, marker)


class _EngineLogFile:
    """`logs/decoy_engine.log` under the working directory, as the engine's file handler writes it."""

    def __init__(self, root: Path) -> None:
        self.path = root / Path("logs", "decoy_engine").with_suffix(".log")
        self.path.parent.mkdir(parents=True)
        self.handler = logging.FileHandler(self.path, encoding="utf-8")
        self.handler.setLevel(logging.DEBUG)
        self.handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    def __enter__(self) -> _EngineLogFile:
        logging.getLogger("decoy_engine").addHandler(self.handler)
        return self

    def __exit__(self, *_exc: Any) -> None:
        logging.getLogger("decoy_engine").removeHandler(self.handler)
        self.handler.close()

    def text(self) -> str:
        return self.path.read_text(encoding="utf-8")


@needs_ner
def test_run_post_mask_leaves_no_canary_anywhere(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    source, out = _frames()
    _assert_markers_in(list(out["notes"]) + list(out["email"]))
    cfg = s.config(
        [s.table_config("t", [])], post_scan={"detectors": ["regex", "classifier", "ner"]}
    )
    detectors = s.detector_set("classifier", "ner")
    with _EngineLogFile(tmp_path) as log, caplog.at_level(logging.DEBUG):
        report = s.run_post_mask({"t": source}, {"t": out}, cfg, detectors)
    assert s.rows(report, "ner", column="notes", detector_id="person_name"), report
    assert any(
        r.name == _LOG and r.getMessage().startswith("post_scan.models ") for r in caplog.records
    ), "no post_scan.models record"
    _assert_clean("report", json.dumps(report))
    _assert_clean("records", "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records))
    _assert_clean("log file", log.text())


@needs_ner
def test_a_blocked_run_leaves_no_canary_anywhere(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from decoy_engine.config import PipelineConfig
    from decoy_engine.execution import run_pipeline

    source, _ = _frames()
    _assert_markers_in(list(source["notes"]))
    path = tmp_path / "t.csv"
    source.to_csv(path, index=False)
    raw = {
        "version": 2,
        "global_settings": {"seed": 3},
        "post_scan": {
            "integrity": True,
            "detectors": ["regex", "classifier", "ner"],
            "ner_findings": "fail",
        },
        "sources": {"t": {"type": "file", "format": "csv", "has_header": True, "path": str(path)}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "notes", "strategy": "text_redact"},
                    {"name": "email", "strategy": "passthrough"},
                ],
            }
        ],
        "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "o.csv")}},
    }
    cfg = PipelineConfig.model_validate(raw).model_dump()
    events: list[str] = []

    class Sink:
        def on_event(self, event: Any) -> None:
            events.append(repr(event))

    error_cls = s.blocked_error()
    table = pa.Table.from_pandas(source, preserve_index=False)
    with _EngineLogFile(tmp_path) as log, caplog.at_level(logging.DEBUG):
        with pytest.raises(error_cls) as caught:
            run_pipeline(cfg, sources={"t": table}, engine_version="m4", progress=Sink())
    err = caught.value
    assert s.rows(err.report, "ner", column="notes", severity="fail"), err.report
    assert events, "no progress event"
    _assert_clean("report", json.dumps(err.report))
    _assert_clean("error", f"{err} {err!r} {err.message} {err.args}")
    clone = pickle.loads(pickle.dumps(err))  # noqa: S301 - our own bytes
    _assert_clean("pickled error", repr(clone.__dict__))
    _assert_clean("events", "\n".join(events))
    _assert_clean("records", "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records))
    _assert_clean("log file", log.text())
