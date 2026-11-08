"""The ner extra plus the pinned model wheel yield a working spaCy pipeline.

Marked `ner`: the ner-gate CI job installs only `dev ner` and the pinned model wheel, so a
missing transitive import (spaCy needs click) or a bad model pin fails here. Where spaCy or the
model is absent (the default gate) the module skips; DECOY_REQUIRE_NER=1, set by ner-gate, turns
that skip into a failure so an empty install cannot pass. spaCy is imported directly, not through
importorskip, because importorskip also swallows a ModuleNotFoundError raised from inside a
broken install, which is exactly the failure this gate exists to surface.
"""

from __future__ import annotations

import pytest

from decoy_engine.detection.ner import DEFAULT_NER_MODEL, iter_ner_spans
from tests._ner import SKIP_REASON, should_skip_ner

pytestmark = pytest.mark.ner

if should_skip_ner():
    pytest.skip(SKIP_REASON, allow_module_level=True)


def test_spacy_imports() -> None:
    import spacy

    assert spacy.__version__


def test_pinned_model_loads() -> None:
    import spacy

    nlp = spacy.load(DEFAULT_NER_MODEL)
    assert "ner" in nlp.pipe_names


def test_ner_finds_person_and_location() -> None:
    spans = iter_ner_spans("Jane Doe lives in Paris", model=DEFAULT_NER_MODEL)
    found = {(s.detector_id, s.matched_text) for s in spans}
    assert ("person_name", "Jane Doe") in found
    assert ("location", "Paris") in found
