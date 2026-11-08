"""NER span detection for text_redact (capability-gaps WS2, 2026-06-12).

Pattern: spaCy NER (en_core_web_sm). See: https://spacy.io/models/en#en_core_web_sm

`iter_spans`'s regex catalog deliberately omits person_name/address:
names and places in free prose have no regex shape (the catalog's own
docstring). This module fills exactly that hole with spaCy NER, mapped
onto the SAME `Span` contract so the text_redact handler can merge NER
spans into the regular leftmost-longest overlap resolution.

Optional dependency: spaCy ships in the `ner` extra
(`pip install decoy-engine[ner]`); the language model is NOT
pip-resolvable and is installed separately
(install the wheel pinned in `NER_MODEL_WHEELS`). Both absences raise typed
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

import importlib.metadata
import importlib.util
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, NamedTuple

from decoy_engine import reasons
from decoy_engine.detection.detectors import Span
from decoy_engine.detection.types import validate_ner_model_name
from decoy_engine.errors import DecoyError

DEFAULT_NER_MODEL = "en_core_web_sm"
# The version of DEFAULT_NER_MODEL that NER_MODEL_WHEELS pins and CI installs.
DEFAULT_NER_MODEL_VERSION = "3.8.0"


class NerModelWheel(NamedTuple):
    """A model wheel pinned by URL and content hash, with the spaCy range it loads under."""

    url: str
    sha256: str
    spacy_range: str


# The wheel a deployment or CI job installs for each supported (model, version).
# The model is not on PyPI, so `pip install` cannot resolve it; a URL alone would
# trust whatever the host serves, hence the hash. `spacy_range` is the model's own
# meta.json `spacy_version`: a spaCy outside it refuses to load the model.
NER_MODEL_WHEELS: Mapping[tuple[str, str], NerModelWheel] = MappingProxyType(
    {
        (DEFAULT_NER_MODEL, DEFAULT_NER_MODEL_VERSION): NerModelWheel(
            url=(
                "https://github.com/explosion/spacy-models/releases/download/"
                "en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
            ),
            sha256="1932429db727d4bff3deed6b34cfc05df17794f4a52eeb26cf8928f7c1a0fb85",
            spacy_range=">=3.8.0,<3.9.0",
        ),
    }
)


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


class NerUnavailableError(DecoyError):
    """spaCy or the requested model is not installed. Machine-readable code."""

    def __init__(self, *, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


class NerModelNameError(NerUnavailableError):
    """`ner_model` is not a plain package name. Carries no part of the rejected value."""

    def __init__(self) -> None:
        super().__init__(
            code=reasons.config.NER_MODEL_INVALID,
            message="NER model must be a plain package name (lowercase letters, digits, _).",
        )


def spacy_installed() -> bool:
    return importlib.util.find_spec("spacy") is not None


def _distribution(model: str) -> importlib.metadata.Distribution | None:
    try:
        return importlib.metadata.distribution(model)
    except importlib.metadata.PackageNotFoundError:
        return None


def _model_meta_file(dist: importlib.metadata.Distribution, model: str) -> Path | None:
    """The `meta.json` a spaCy model wheel installs inside its package, found from the dist's RECORD."""
    for entry in dist.files or ():
        if entry.parts == (model, "meta.json"):
            return Path(str(dist.locate_file(entry)))
    return None


def installed_model_version(model: str = DEFAULT_NER_MODEL) -> str | None:
    """Installed pip version of the model package, or None when absent.

    importlib.metadata only: no spaCy import, no model load, safe in the
    extras-free CI environment. The plan compiler stamps this onto
    text_redact ColumnSeeds (deferred follow-up 8c) because NER output
    is deterministic only per model VERSION: cross-environment
    byte-stability needs the version pinned and recorded, not just the
    model named.
    """
    dist = _distribution(model)
    return (dist.version or None) if dist is not None else None


def model_installed(model: str = DEFAULT_NER_MODEL) -> bool:
    """True when `model` is an installed distribution that ships a spaCy `meta.json`.

    A module that merely imports (`os`, `json`, `this`) is not a model, and nothing is imported
    to decide: the answer comes from the distribution metadata alone.
    """
    dist = _distribution(model)
    return dist is not None and bool(dist.version) and _model_meta_file(dist, model) is not None


def _import_spacy() -> tuple[Any, str | None]:
    """Import spaCy; on failure return the exception class name so the caller raises outside `except`."""
    try:
        import spacy
    except (
        Exception
    ) as exc:  # a broken install fails at import with any error, not just ImportError
        return None, type(exc).__name__
    return spacy, None


def _spacy_incompatible(spacy: Any, model: str) -> bool:
    """True when the installed spaCy is outside the range the installed model declares.

    spaCy's own helpers read the model's meta.json (`spacy_version`) and compare it. An absent,
    unreadable or unparseable range is not a refusal: spaCy then decides at load, as before.
    """
    import warnings

    from spacy.util import get_model_meta, is_compatible_version

    dist = _distribution(model)
    meta_file = _model_meta_file(dist, model) if dist is not None else None
    if meta_file is None:
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # get_model_meta warns on this very mismatch
            declared = get_model_meta(meta_file.parent).get("spacy_version")
    except Exception:
        return False
    if not isinstance(declared, str) or not declared:
        return False
    return is_compatible_version(spacy.about.__version__, declared, prereleases=True) is False


def ensure_ner_available(model: str = DEFAULT_NER_MODEL) -> None:
    """Raise the typed error a config-only caller (decoy validate) needs.

    The name check runs first, before any message that would carry the name is built. Nothing
    but spaCy itself is imported: an installed model is recognised from its metadata.
    """
    validate_ner_model_name(model)
    if not spacy_installed():
        raise NerUnavailableError(
            code=reasons.provider.NER_SPACY_NOT_INSTALLED,
            message=(
                "text_redact `ner` requires spaCy: pip install 'decoy-engine[ner]' "
                "(or pip install spacy)."
            ),
        )
    if not model_installed(model):
        raise NerUnavailableError(
            code=reasons.provider.NER_MODEL_NOT_INSTALLED,
            message=(
                f"text_redact `ner` model {model!r} is not installed: install its wheel "
                "(the pinned wheels are listed in decoy_engine.detection.ner.NER_MODEL_WHEELS)."
            ),
        )
    spacy, failure = _import_spacy()
    if spacy is None:
        raise NerUnavailableError(
            code=reasons.provider.NER_SPACY_NOT_INSTALLED,
            message=(
                f"spaCy is installed but cannot be imported ({failure}): reinstall it with "
                "pip install --force-reinstall 'decoy-engine[ner]'."
            ),
        )
    if _spacy_incompatible(spacy, model):
        raise NerUnavailableError(
            code=reasons.provider.NER_MODEL_INCOMPATIBLE,
            message=(
                f"NER model {model!r} {installed_model_version(model)} was built for another "
                f"spaCy minor than the installed spaCy {spacy.about.__version__}: "
                "install the model wheel pinned for this spaCy."
            ),
        )


# nlp.pipe's internal batch size (Phase 5, docs/plans/2026-09-08-p5-ner-batch-
# helper.md): spaCy's own docs recommend `Language.pipe` over per-call
# inference for throughput; this is the size of the C-level work unit `.pipe`
# hands the model, independent of how many texts a caller collects at once.
_NER_PIPE_BATCH_SIZE = 256
# A microbatch window: collecting/inferring/applying this many rows at a time
# bounds peak RSS instead of materializing every span list (and every
# collected cell string) before applying any of them. A multiple of
# `_NER_PIPE_BATCH_SIZE` so one window is an integer number of pipe batches.
# Used by both the FULL-FRAME callers (oracle text_mask/text_redact, which hold
# a whole column in memory) and the OUT-OF-CORE text_redact kernel
# (`out_of_core/_mask_group_b.text_redact_array`). An out-of-core batch is
# "batch-local", but `batch_rows` is sized by the memory budget and routinely
# far above this window, so batch-locality alone does not bound the NER working
# set; windowing does. The NER working set (the window's cell strings + span
# lists, plus one loaded spaCy pipeline per process) is Python/Arrow-side, not
# DuckDB buffer-manager memory, so it is covered by the out-of-core budget's
# reserve (`out_of_core/_budget.py`, `_OOC_RESERVE_*`) rather than by a
# per-strategy term in the row-linear build-floor model
# (`out_of_core/_memory_estimate.py`); this bounded window is what keeps that
# true. (Out-of-core text_mask, group c, does not window yet; tracked as a
# follow-up alongside sharing this config/version plumbing.)
_NER_APPLY_WINDOW = 2048

# One loaded pipeline per (model, installed version) per process. Model load is ~1s. Handlers
# call iter_ner_spans_batch (nlp.pipe over a batch of cells); iter_ner_spans is
# the single-cell path. The two reproduce the same per-doc span logic
# independently (iter_ner_spans is kept as the batch helper's differential
# oracle), so a change to one must be mirrored in the other. Keyed by version so a model
# upgrade inside a long-lived worker loads the new package instead of serving the old one.
_PIPELINES: dict[tuple[str, str], Any] = {}


def _pipeline(model: str) -> Any:
    validate_ner_model_name(model)
    nlp = _PIPELINES.get((model, installed_model_version(model) or ""))
    if nlp is None:
        ensure_ner_available(model)
        import spacy

        key = (model, installed_model_version(model) or "")

        # Only the NER component (and its tok2vec) runs; tagging/parsing
        # cost would be paid per cell for nothing.
        nlp = spacy.load(model, exclude=["tagger", "parser", "lemmatizer", "attribute_ruler"])
        _PIPELINES[key] = nlp
    return nlp


def pipeline_version(model: str) -> str:
    """The version of the pipeline that runs for `model`: its own `meta["version"]`, not the key."""
    return str(_pipeline(model).meta["version"])


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


def iter_ner_spans_batch(
    texts: Sequence[object],
    *,
    model: str = DEFAULT_NER_MODEL,
    entities: Collection[str] | None = None,
    batch_size: int = _NER_PIPE_BATCH_SIZE,
) -> list[list[Span]]:
    """Batched `iter_ner_spans`, one `Doc` per input, via `nlp.pipe`.

    Returns one `list[Span]` per element of `texts`, in input order, each
    element exactly equal to `iter_ner_spans(texts[i], model=model,
    entities=entities)` -- this is a throughput optimization over spaCy's
    `Language.pipe` (spaCy's own docs recommend it over per-call inference),
    never a change to what gets found. `iter_ner_spans`'s single-cell body is
    left untouched and is NOT reused here on purpose: the two implementations
    stay independent so this helper's differential test
    (`iter_ner_spans_batch(corpus) == [iter_ner_spans(t) for t in corpus]`) can
    catch a mapping/offset/filter regression in either one. A shared extractor
    would make that regression common-mode instead.

    `batch_size` must be positive: `nlp.pipe([...], batch_size=0)` silently
    yields zero documents rather than raising, and a naive scatter would turn
    that into an all-empty result -- unmasked PII with no error. Guarding here
    turns that failure mode into an immediate `ValueError`.

    Each element is guarded exactly like `iter_ner_spans`: only a non-string or
    the empty string is excluded (`[]`, no model call). A whitespace-only
    string is non-empty, so it reaches `.pipe` exactly as it reaches `nlp()`
    in the single-cell path -- stripping it here would change the oversized-
    whitespace raise contract the single-cell path already has.

    `.pipe` is required to yield exactly one `Doc` per passing text, in order
    (spaCy guarantees ordering for `n_process=1`); `zip(..., strict=True)`
    turns any other count into a `ValueError` instead of a silently truncated
    or padded result. A per-doc exception (e.g. a cell over the model's
    `max_length`) propagates from the generator during that zip, matching the
    single-cell raise contract -- no partial column is ever produced.
    """
    if batch_size <= 0:
        raise ValueError(
            f"iter_ner_spans_batch requires a positive batch_size, got {batch_size!r}: "
            "nlp.pipe(..., batch_size<=0) yields zero documents without raising, which "
            "would silently drop every NER span in the batch (fail-open PII leak)."
        )
    wanted = set(entities) if entities is not None else set(NER_ENTITY_MAP.values())

    passing_indices: list[int] = []
    passing_texts: list[str] = []
    for i, text in enumerate(texts):
        if not isinstance(text, str) or not text:
            continue
        passing_indices.append(i)
        passing_texts.append(text)

    results: list[list[Span]] = [[] for _ in range(len(texts))]
    if not passing_texts:
        return results

    nlp = _pipeline(model)
    docs = nlp.pipe(passing_texts, batch_size=batch_size, n_process=1)
    for idx, doc in zip(passing_indices, docs, strict=True):
        out: list[Span] = []
        for ent in doc.ents:
            detector_id = NER_ENTITY_MAP.get(ent.label_)
            if detector_id is None or detector_id not in wanted:
                continue
            out.append(Span(detector_id, ent.start_char, ent.end_char, ent.text))
        results[idx] = out
    return results
