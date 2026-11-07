"""Shared gating for tests that load the real spaCy model.

The ner-gate CI job installs the `ner` extra and the pinned model wheel and runs `pytest -m ner`;
everywhere else (the default gate, a dev venv) spaCy or the model is usually absent and these
tests skip. DECOY_REQUIRE_NER=1, set by ner-gate, turns that skip into a run, so an install that
lost spaCy or the model fails loudly instead of reporting an all-skipped green.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from decoy_engine.detection.ner import DEFAULT_NER_MODEL, model_installed, spacy_installed

REQUIRE_NER = os.environ.get("DECOY_REQUIRE_NER") == "1"
SKIP_REASON = f"spacy or {DEFAULT_NER_MODEL} not installed (ner extra)"


def ner_available() -> bool:
    return spacy_installed() and model_installed(DEFAULT_NER_MODEL)


def should_skip_ner() -> bool:
    return not REQUIRE_NER and not ner_available()


_skip_without_ner = pytest.mark.skipif(should_skip_ner(), reason=SKIP_REASON)


def needs_ner(obj: Any) -> Any:
    """Select the test under the `ner` marker and skip it where the model is unavailable."""
    return pytest.mark.ner(_skip_without_ner(obj))
