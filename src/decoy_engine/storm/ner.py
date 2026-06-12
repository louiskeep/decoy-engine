"""NER span detection for text_redact (capability-gaps WS2, 2026-06-12).

`iter_spans`'s regex catalog deliberately omits person_name/address:
names and places in free prose have no regex shape (the catalog's own
docstring). This module fills exactly that hole with spaCy NER, mapped
onto the SAME `Span` contract so the text_redact handler can merge NER
spans into the regular leftmost-longest overlap resolution.

Optional dependency: spaCy ships in the `ner` extra
(`pip install decoy-engine[ner]`); the language model is NOT
pip-resolvable and is installed separately
(`python -m spacy download en_core_web_sm`). Both absences raise typed
errors naming the fix; the plan compiler's `text_redact_ner_available`
check surfaces the same verdicts at validate time.

Determinism: spaCy NER inference is deterministic for a pinned model
version (greedy transition-based decoding, no sampling), so text_redact
output stays a pure function of (input, config, model version). A model
UPGRADE can change which spans are found -- pin the model package in
deployments that need byte-stable output across environments.

Entity mapping (v1): PERSON -> person_name; GPE/LOC/FAC -> location.
Other entity labels (ORG, DATE, MONEY, ...) are deliberately excluded:
redacting them shreds legitimate prose for little PII value, the same
rationale the regex catalog uses for name-hint-only detectors. Cells
longer than the model's max_length raise; multi-MB cells are outside
the text_redact contract (see _text_redact.py).
"""

from __future__ import annotations

import importlib.util
from typing import Any

from decoy_engine.storm.detectors import Span

DEFAULT_NER_MODEL = "en_core_web_sm"

# spaCy entity label -> decoy detector id. The values join the regex
# catalog's namespace, so [REDACTED:person_name] reads the same whether
# the span came from a regex or the model.
# Two label schemes share this map (deferred follow-up 8b, 2026-06-12):
# English models (en_core_web_*) emit OntoNotes labels (PERSON/GPE/LOC/
# FAC); most non-English and multilingual models (de_core_news_*,
# es_core_news_*, xx_ent_wiki_sm) emit WikiNER-style PER/LOC/ORG/MISC.
# Without the PER row, every person hit from a non-English model was
# silently dropped even though the `ner: {model: ...}` config accepted
# the model name.
NER_ENTITY_MAP: dict[str, str] = {
    "PERSON": "person_name",
    "PER": "person_name",
    "GPE": "location",
    "LOC": "location",
    "FAC": "location",
}


class NerUnavailableError(Exception):
    """spaCy or the requested model is not installed. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def spacy_installed() -> bool:
    return importlib.util.find_spec("spacy") is not None


def model_installed(model: str = DEFAULT_NER_MODEL) -> bool:
    """True when the model package is importable (cheap; no model load)."""
    return importlib.util.find_spec(model) is not None


def installed_model_version(model: str = DEFAULT_NER_MODEL) -> str | None:
    """Installed pip version of the model package, or None when absent.

    importlib.metadata only: no spaCy import, no model load, safe in the
    extras-free CI environment. The plan compiler stamps this onto
    text_redact ColumnSeeds (deferred follow-up 8c) because NER output
    is deterministic only per model VERSION: cross-environment
    byte-stability needs the version pinned and recorded, not just the
    model named.
    """
    import importlib.metadata

    try:
        return importlib.metadata.version(model)
    except importlib.metadata.PackageNotFoundError:
        return None


def ensure_ner_available(model: str = DEFAULT_NER_MODEL) -> None:
    """Raise the typed error a config-only caller (decoy validate) needs."""
    if not spacy_installed():
        raise NerUnavailableError(
            code="ner_spacy_not_installed",
            message=(
                "text_redact `ner` requires spaCy: pip install 'decoy-engine[ner]' "
                "(or pip install spacy)."
            ),
        )
    if not model_installed(model):
        raise NerUnavailableError(
            code="ner_model_not_installed",
            message=(
                f"text_redact `ner` model {model!r} is not installed: "
                f"python -m spacy download {model}"
            ),
        )


# One loaded pipeline per model name per process. Model load is ~1s; the
# handler calls iter_ner_spans per cell.
_PIPELINES: dict[str, Any] = {}


def _pipeline(model: str) -> Any:
    nlp = _PIPELINES.get(model)
    if nlp is None:
        ensure_ner_available(model)
        import spacy

        # Only the NER component (and its tok2vec) runs; tagging/parsing
        # cost would be paid per cell for nothing.
        nlp = spacy.load(model, exclude=["tagger", "parser", "lemmatizer", "attribute_ruler"])
        _PIPELINES[model] = nlp
    return nlp


def iter_ner_spans(
    text: str,
    *,
    model: str = DEFAULT_NER_MODEL,
    entities: list[str] | None = None,
) -> list[Span]:
    """Yield NER-found PII spans in `text` under the Span contract.

    `entities` filters by DETECTOR id (`person_name`, `location`);
    None means every mapped entity. Overlap resolution is the caller's
    job: pass the result to `iter_spans(..., extra_spans=...)` so NER
    and regex spans resolve together.
    """
    if not isinstance(text, str) or not text:
        return []
    wanted = set(entities) if entities is not None else set(NER_ENTITY_MAP.values())
    nlp = _pipeline(model)
    out: list[Span] = []
    for ent in nlp(text).ents:
        detector_id = NER_ENTITY_MAP.get(ent.label_)
        if detector_id is None or detector_id not in wanted:
            continue
        out.append(Span(detector_id, ent.start_char, ent.end_char, ent.text))
    return out
