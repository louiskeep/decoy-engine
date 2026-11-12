"""Shared helpers for the model post-scan tests: generated notes columns, the canary corpus,
detector sets loaded the strict way, and a recording spy on the spaCy pipeline.

The notes columns are seeded and generated, never hand-picked: neutral sentences that name no
person or place, with a seeded Faker person name appended to every `every`-th cell. A `redacted`
column is the same cells with each name replaced by the token `text_redact` writes, so it holds no
name at all. The canary corpus seeds marker strings into cells; a test first proves the markers are
in the input and that a model finding was made, then proves no marker left the pass.
"""

from __future__ import annotations

import inspect
import random
from collections.abc import Iterator, Sequence
from typing import Any

import pandas as pd
import pytest

SEED = 20261112
CELLS = 400
REDACTED = "[REDACTED]"

# Markers for the canary corpus. The person name is one spaCy tags as PERSON, so a span that
# leaked would carry it; the other two are plain cell content.
CANARY_PERSON = "Zorvath Quillenbeck"
CANARY_TOKEN = "CANARY-NAME-7781"
CANARY_EMAIL = "canary.7781@example.invalid"
CANARIES = (CANARY_PERSON, "Zorvath", "Quillenbeck", CANARY_TOKEN, CANARY_EMAIL)

_OPENERS = (
    "The invoice for order {n} was paid on time.",
    "Payment {n} cleared this morning.",
    "The ticket {n} was closed after the second review.",
    "Please update the billing record for account {n}.",
    "The refund for return {n} has been approved.",
    "The claim {n} needs one more document.",
    "The renewal notice for policy {n} was sent today.",
    "The replacement part for unit {n} is on back order.",
)


def _names(rng: random.Random) -> Iterator[str]:
    from faker.providers.person.en_US import Provider as Person

    firsts, lasts = sorted(Person.first_names), sorted(Person.last_names)
    while True:
        yield f"{rng.choice(firsts)} {rng.choice(lasts)}"


def notes_column(
    every: int, *, n: int = CELLS, redacted: bool = False, seed: int = SEED
) -> list[str]:
    """`n` free-text cells; every `every`-th one asks a named person to confirm (0: none do)."""
    rng = random.Random(seed)
    names = _names(random.Random(seed + 1))
    out: list[str] = []
    for i in range(n):
        cell = rng.choice(_OPENERS).format(n=rng.randint(1000, 9999))
        if every and i % every == 0:
            cell = f"{cell} Ask {REDACTED if redacted else next(names)} to confirm."
        out.append(cell)
    return out


def short_names(n: int = CELLS, *, seed: int = SEED) -> list[str]:
    """Person names alone, two tokens each: a column that is not free text."""
    names = _names(random.Random(seed + 2))
    return [next(names) for _ in range(n)]


def canary_notes(n: int = CELLS) -> list[str]:
    """A notes column that names the canary person in one cell in two, beside the other markers."""
    cells = notes_column(0, n=n)
    for i in range(0, n, 2):
        cells[i] = f"{cells[i]} Ask {CANARY_PERSON} to confirm order {CANARY_TOKEN}."
    for i in range(1, n, 4):
        cells[i] = f"{cells[i]} Reply to {CANARY_EMAIL} today."
    return cells


def table_config(name: str, columns: Sequence[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "columns": [dict(c) for c in columns]}


def config(
    tables: Sequence[dict[str, Any]], post_scan: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The two blocks `run_post_mask` reads, shaped like a `PipelineConfig` dump."""
    block = {"integrity": True, **(post_scan or {})}
    return {"version": 2, "tables": list(tables), "post_scan": block}


def detector_set(*sources: str) -> Any:
    from decoy_engine import load_detectors

    return load_detectors(("regex", *sources), ner_model="en_core_web_sm")


def run_post_mask(
    source_frames: dict[str, pd.DataFrame],
    output_frames: dict[str, pd.DataFrame],
    cfg: dict[str, Any],
    detectors: Any,
) -> dict[str, Any]:
    from decoy_engine.verification.postmask import runner

    params = inspect.signature(runner.run_post_mask).parameters
    assert "detectors" in params, "run_post_mask takes no detectors"
    return runner.run_post_mask(source_frames, output_frames, config=cfg, detectors=detectors)


def rows(report: dict[str, Any], source: str | None = None, **match: Any) -> list[dict[str, Any]]:
    """Residual-PII findings of one detector source (all when `source` is None) matching `match`."""
    out = []
    for finding in report["residual_pii"]:
        assert "source" in finding, "residual-PII findings carry no source"
        if source is not None and finding["source"] != source:
            continue
        if all(finding.get(k) == v for k, v in match.items()):
            out.append(finding)
    return out


def blocked_error() -> type[Exception]:
    import decoy_engine

    cls = getattr(decoy_engine, "PostScanBlockedError", None)
    assert cls is not None, "decoy_engine exports no PostScanBlockedError"
    return cls


class PipelineSpy:
    """Records every text the loaded spaCy pipeline is given, through `pipe` or a direct call."""

    def __init__(self, nlp: Any) -> None:
        self._nlp = nlp
        self.texts: list[str] = []

    def pipe(self, texts: Any, *args: Any, **kwargs: Any) -> Any:
        texts = list(texts)
        self.texts.extend(texts)
        return self._nlp.pipe(texts, *args, **kwargs)

    def __call__(self, text: str) -> Any:
        self.texts.append(text)
        return self._nlp(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._nlp, name)


class PipelineRecorder:
    def __init__(self) -> None:
        self.spies: dict[str, PipelineSpy] = {}

    @property
    def texts(self) -> list[str]:
        return [t for spy in self.spies.values() for t in spy.texts]


def spy_pipeline(monkeypatch: pytest.MonkeyPatch) -> PipelineRecorder:
    """Wrap the one spaCy load seam, so a scorer that bypasses `ner_rates` is seen too."""
    from decoy_engine.detection import ner

    real = ner._pipeline
    recorder = PipelineRecorder()

    def wrapped(model: str) -> PipelineSpy:
        if model not in recorder.spies:
            recorder.spies[model] = PipelineSpy(real(model))
        return recorder.spies[model]

    monkeypatch.setattr(ner, "_pipeline", wrapped)
    return recorder
