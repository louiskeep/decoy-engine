"""C6c-ii: per-cell ASCII-domain routing for the native `text_redact` span detection.

The oracle's `iter_spans` runs eleven detectors in Python per cell. C6c-ii moves the eight
lookaround-free detectors (`email`, `us_phone`, `pan`, `iban`, `ipv4`, `icd10`, `npi`, `url`)
into the compiled companion for cells in the ASCII-safe domain, where Python's `\\d`/`\\s`/ASCII
case-fold classes and the Rust `(?-u)` classes coincide code-point-for-code-point (plan 2.1/2.2).
Every other cell (non-ASCII, a 0x1c-0x1f separator, a lone surrogate, a non-string) and the three
lookaround detectors (`ssn`, `us_zip`, `street_address`) stay on the unchanged Python path, so the
spliced output is byte-identical to the oracle always.

This module owns three pieces the wrapper in `_kernels_scalar.py` builds on:

- The versioned detector catalog (ids, labels, Rust-supported flag) mirrored in Rust under
  `CATALOG_VERSION`. The loader rejects a version or supported-set skew and falls back to full
  Python (plan 4.6); a detector known to the oracle but not in the eight runs in Python, never a
  silent no-op.
- `load_text_redact_kernel()`, which returns a thin kernel wrapper or `None` (companion absent,
  the symbol absent on an older companion, or a catalog skew), each resolving to the literal
  Python path.
- `merge_text_redact_spans(...)`, which assembles one cell's final span list in the EXACT order
  `iter_spans` builds it (extras, then built-ins in requested order with the eight interleaved
  with the three Python lookaround detectors, then custom), so Python's stable sort resolves ties
  identically. The eight's candidates come from the Rust call; the rest run `finditer` here.

Keeping `finditer` out of `_kernels_scalar.py` is deliberate: that module's "no regex of its own"
contract (C6c-i) stays intact because every regex call for the lookaround/custom path lives here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from decoy_engine.storm.detectors import _SPAN_DETECTORS, Span

if TYPE_CHECKING:
    from collections.abc import Sequence

# The eight lookaround-free detector ids, in catalog order. The index returned by the Rust kernel
# is a position in this tuple; it maps back to the id (and its `[REDACTED:<id>]` label) here. Keep
# in sync with `decoy-engine-native/src/text_redact.rs::SUPPORTED_IDS`.
SUPPORTED_IDS: tuple[str, ...] = (
    "email",
    "us_phone",
    "pan",
    "iban",
    "ipv4",
    "icd10",
    "npi",
    "url",
)
_SUPPORTED_SET = frozenset(SUPPORTED_IDS)
_INDEX_OF: dict[str, int] = {det_id: i for i, det_id in enumerate(SUPPORTED_IDS)}

# The detector-catalog contract id. Bump when ANY detector's id, order, label, pattern, validator,
# OR Rust-supported flag changes (plan 4.6). The Rust mirror (`text_redact.rs::CATALOG_VERSION`)
# must match this exactly, or the loader falls back to full Python.
CATALOG_VERSION = 1

# The label emitted for a detector under `label_token=True` is the detector id itself (the oracle's
# `_splice` reads `Span.detector_id`), so the catalog's label column equals its id column. An
# agreement test pins that the Rust-supported set, order, labels and version all match.
CATALOG_LABELS: dict[str, str] = {det_id: det_id for det_id in SUPPORTED_IDS}


def is_ascii_safe(text: str) -> bool:
    """The routing predicate (plan 2.1): every code point is ASCII and none is in 0x1c-0x1f.

    This Python mirror of the Rust `is_eligible` exists for the boundary tests; the live routing
    decision is the kernel's own per-cell `None` return. A lone surrogate is not ASCII, so it is
    ineligible here and cannot be decoded to a Rust `str` there -- both route it to Python.
    """
    return text.isascii() and not any("\x1c" <= ch <= "\x1f" for ch in text)


class TextRedactKernel(Protocol):
    """The compiled span kernel as this module calls it."""

    def text_redact_candidates(
        self,
        values: Sequence[object],
        detector_ids: Sequence[str],
        catalog_version: int,
    ) -> list[list[tuple[int, int, int]] | None]: ...


class _CompiledTextRedactKernel:
    """Thin wrapper over the companion's `_kernel.text_redact_candidates`."""

    def __init__(self, candidates_fn: Any) -> None:
        self._candidates_fn = candidates_fn

    def text_redact_candidates(
        self,
        values: Sequence[object],
        detector_ids: Sequence[str],
        catalog_version: int,
    ) -> list[list[tuple[int, int, int]] | None]:
        return self._candidates_fn(values, list(detector_ids), catalog_version)


def load_text_redact_kernel() -> TextRedactKernel | None:
    """Return the compiled span kernel, or `None` to run the full Python path.

    `None` (never an exception) is returned for each of the three fallback modes in plan 4.6:
    the companion package is absent or fails to import; it is an older build without the
    `text_redact_candidates` symbol (or its agreement helpers); or its catalog version / supported
    set does not match this core's. Each case resolves to the literal C6c-i Python detection path,
    so output is unchanged. A silent per-cell `None` from the kernel (ineligible cell) is a
    different mechanism and is handled by the caller, not here.
    """
    try:
        from decoy_engine_native import _kernel
    except Exception:
        # Absent, or `sys.modules["decoy_engine_native"] = None` (the tests' companion-absent
        # simulation), or any import-time failure: run the full Python path.
        return None

    candidates_fn = getattr(_kernel, "text_redact_candidates", None)
    version_fn = getattr(_kernel, "text_redact_catalog_version", None)
    supported_fn = getattr(_kernel, "text_redact_supported_ids", None)
    if candidates_fn is None or version_fn is None or supported_fn is None:
        return None

    try:
        if int(version_fn()) != CATALOG_VERSION:
            return None
        if tuple(supported_fn()) != SUPPORTED_IDS:
            return None
    except Exception:
        return None

    return _CompiledTextRedactKernel(candidates_fn)


def requested_rust_ids(detector_ids: list[str] | None) -> list[str]:
    """The Rust-supported subset of `detector_ids`, in the requested order (plan 2.3).

    `None` means the oracle's "all built-in detectors", whose Rust-supported members are
    `SUPPORTED_IDS` in catalog order. Duplicates and unknown ids are preserved/dropped exactly as
    the merge replays them: a duplicate supported id re-emits the same spans (the overlap sweep
    collapses them), so sending it once is byte-equivalent and the kernel receives each requested
    occurrence filtered to the supported set.
    """
    if detector_ids is None:
        return list(SUPPORTED_IDS)
    # Dedupe, preserving first-seen order: the kernel need only scan each detector once. The merge
    # replays each requested occurrence against that one scan, so a repeated supported id still
    # emits its spans once per occurrence, exactly as the oracle's repeated `finditer` pass does.
    seen: set[str] = set()
    ids: list[str] = []
    for det_id in detector_ids:
        if det_id in _SUPPORTED_SET and det_id not in seen:
            seen.add(det_id)
            ids.append(det_id)
    return ids


def merge_text_redact_spans(
    text: str,
    detector_ids: list[str] | None,
    *,
    rust_candidates: list[tuple[int, int, int]] | None,
    custom: list[dict[str, Any]] | None = None,
    extra_spans: list[Span] | None = None,
) -> list[Span]:
    """Assemble one cell's resolved spans, reproducing `iter_spans` exactly (plan 4.3).

    `rust_candidates` is the kernel's per-cell list `[(detector_index, start, end), ...]` for an
    eligible cell, or `None` to run every detector in Python (an ineligible cell, equivalent to
    `iter_spans`). The candidate order is the oracle's: `extra_spans` (NER, supplied order), then
    the built-ins in `detector_ids` order -- each eligible supported detector's spans taken from
    `rust_candidates`, every other detector run through its Python regex + validator in `finditer`
    order -- then custom (supplied spec then `finditer` order). A stable sort by
    `(start, -(end - start))` then a leftmost-then-longest sweep (`start >= last_end`, zero-length
    custom spans included) resolves overlaps. `_splice` is the caller's.
    """
    if not text:
        return []

    candidates: list[Span] = list(extra_spans or [])

    rust_by_index: dict[int, list[tuple[int, int, int]]] = {}
    if rust_candidates is not None:
        for triple in rust_candidates:
            rust_by_index.setdefault(triple[0], []).append(triple)

    # `None` is the oracle's "all built-in detectors" in `_SPAN_DETECTORS` insertion order, which
    # is the order its stable sort breaks ties by; preserve it exactly (never supported-first).
    ids = list(_SPAN_DETECTORS.keys()) if detector_ids is None else detector_ids

    for det_id in ids:
        if rust_candidates is not None and det_id in _SUPPORTED_SET:
            for _idx, start, end in rust_by_index.get(_INDEX_OF[det_id], ()):
                candidates.append(Span(det_id, start, end, text[start:end]))
            continue
        entry = _SPAN_DETECTORS.get(det_id)
        if entry is None:
            continue
        regex, validator = entry
        for m in regex.finditer(text):
            matched = m.group(0)
            if validator is not None and not validator(matched):
                continue
            candidates.append(Span(det_id, m.start(), m.end(), matched))

    for spec in custom or []:
        pattern = spec["pattern"]
        validator = spec.get("validator")
        det_id = spec["detector_id"]
        for m in pattern.finditer(text):
            matched = m.group(0)
            if validator is not None and not validator(matched):
                continue
            candidates.append(Span(det_id, m.start(), m.end(), matched))

    candidates.sort(key=lambda s: (s.start, -(s.end - s.start)))
    out: list[Span] = []
    last_end = -1
    for s in candidates:
        if s.start >= last_end:
            out.append(s)
            last_end = s.end
    return out


__all__ = [
    "CATALOG_LABELS",
    "CATALOG_VERSION",
    "SUPPORTED_IDS",
    "TextRedactKernel",
    "is_ascii_safe",
    "load_text_redact_kernel",
    "merge_text_redact_spans",
    "requested_rust_ids",
]
