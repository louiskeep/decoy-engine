"""C6b-ii slice b: route eligible `text_mask` cells through the compiled redact/passthrough splice.

Slice a moved span DETECTION into Rust but kept the per-span Python splice, which is where the
per-cell time goes. This module hands the splice itself to the companion for configs where every
span strategy is `redact` or `passthrough` and the unmatched policy is `redact` or `passthrough`.
Anything else (fpe, faker, date_shift spans, `replace_with_token`, a non-string token, an older
companion without the symbols, a non-ASCII cell) is declined and runs slice a's Python splice, so
output stays byte-identical to the oracle.

Two kernel calls, picked per config:
- every requested detector is one of the eight Rust detectors: `text_mask_detect_splice` does
  detection, the oracle's merge, and the splice in one call per batch.
- otherwise (a lookaround detector such as `ssn` is requested): detection and the merge stay in
  `merge_text_redact_spans`, which already holds the Python-detected spans, and
  `text_mask_splice_spans` splices those RESOLVED MERGED spans. Splicing raw Rust candidates here
  would miss the lookaround spans.

Established methodology: no new detection or splice algorithm. The kernels reuse the `regex` crate
detectors from `text_redact.rs` and the stable-sort plus leftmost-longest sweep of
`merge_text_redact_spans`; see `decoy-engine-native/src/text_mask.rs`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from decoy_engine.execution.native._text_redact_kernel import (
    CATALOG_VERSION,
    SUPPORTED_IDS,
    TextRedactKernel,
    merge_text_redact_spans,
)
from decoy_engine.storm.detectors import _SPAN_DETECTORS, Span, iter_spans
from decoy_engine.transforms._text_mask_splice import warn_passthrough_unmatched
from decoy_engine.transforms.text_mask import DETECTOR_DEFAULTS

_SUPPORTED_SET = frozenset(SUPPORTED_IDS)
_NATIVE_STRATEGIES = frozenset({"redact", "passthrough"})
_NATIVE_POLICIES = frozenset({"redact", "passthrough"})

# The same logger object `text_mask.mask_cell_with_spans` warns on, so a native cell's
# passthrough warning is indistinguishable from the oracle's.
_TEXT_MASK_LOG = logging.getLogger("decoy_engine.transforms.text_mask")


@dataclass(frozen=True)
class SpliceKernel:
    """The two compiled splice entry points."""

    splice_spans: Callable[..., list[str | None]]
    detect_splice: Callable[..., list[str | None]]


def load_splice_kernel() -> SpliceKernel | None:
    """The companion's splice symbols, or None (absent or an older build) to keep slice a."""
    try:
        from decoy_engine_native import _kernel
    except Exception:
        return None
    splice_spans = getattr(_kernel, "text_mask_splice_spans", None)
    detect_splice = getattr(_kernel, "text_mask_detect_splice", None)
    if splice_spans is None or detect_splice is None:
        return None
    return SpliceKernel(splice_spans, detect_splice)


@dataclass(frozen=True)
class SplicePlan:
    """A config the native splice can reproduce, resolved once per call."""

    token: str
    passthrough_unmatched: bool
    verbatim: Mapping[str, bool]  # requested span detector id -> passthrough?
    # Set only when every requested detector is Rust-supported; then detection runs in the splice.
    rust_ids: tuple[str, ...] | None
    rust_verbatim: tuple[bool, ...]


def plan_native_splice(
    detector_ids: Sequence[str] | None,
    strategy_map: Mapping[str, str] | None,
    unmatched_span_policy: str,
    token: Any,
) -> SplicePlan | None:
    """Resolve the config, or None when any span strategy / policy needs the Python splice.

    `detector_ids=None` means every built-in detector (never `()`), so the strategy check covers
    the COMPLETE set first. Ids that are not span detectors emit no spans and are ignored, as
    `iter_spans` ignores them.
    """
    if unmatched_span_policy not in _NATIVE_POLICIES or not isinstance(token, str):
        return None
    effective: dict[str, str] = dict(DETECTOR_DEFAULTS)
    if strategy_map:
        effective.update(strategy_map)
    requested = list(_SPAN_DETECTORS) if detector_ids is None else list(detector_ids)
    relevant = [d for d in requested if d in _SPAN_DETECTORS]
    verbatim: dict[str, bool] = {}
    for det_id in relevant:
        strategy = effective.get(det_id, "redact")
        if strategy not in _NATIVE_STRATEGIES:
            return None
        verbatim[det_id] = strategy == "passthrough"
    all_rust = all(d in _SUPPORTED_SET for d in relevant)
    return SplicePlan(
        token=token,
        passthrough_unmatched=unmatched_span_policy == "passthrough",
        verbatim=verbatim,
        rust_ids=tuple(relevant) if all_rust else None,
        rust_verbatim=tuple(verbatim[d] for d in relevant) if all_rust else (),
    )


def splice_batch(
    batch: Sequence[str | None],
    *,
    plan: SplicePlan,
    splice: SpliceKernel,
    kernel: TextRedactKernel,
    detector_ids: list[str] | None,
    rust_ids: list[str],
    fallback: Callable[[str, list[Span]], str],
) -> list[str | None]:
    """Mask one batch; a cell the kernel declines goes through `fallback` (slice a's splice).

    `fallback(text, spans)` receives the resolved spans for that cell, built by the oracle's own
    path, so a declined cell is byte-identical to the oracle.
    """
    if plan.rust_ids is not None:
        results = splice.detect_splice(
            batch,
            list(plan.rust_ids),
            list(plan.rust_verbatim),
            plan.token,
            plan.passthrough_unmatched,
            CATALOG_VERSION,
        )
        spans_for: list[list[Span] | None] = [None] * len(batch)
    else:
        per_cell = kernel.text_redact_candidates(batch, rust_ids, CATALOG_VERSION)
        spans_for = []
        tuples: list[list[tuple[int, int, bool]]] = []
        for text, cands in zip(batch, per_cell, strict=True):
            if text is None:
                spans_for.append(None)
                tuples.append([])
                continue
            spans = (
                iter_spans(text, detector_ids, extra_spans=None)
                if cands is None
                else merge_text_redact_spans(text, detector_ids, rust_candidates=cands)
            )
            spans_for.append(spans)
            tuples.append([(s.start, s.end, plan.verbatim[s.detector_id]) for s in spans])
        results = splice.splice_spans(batch, tuples, plan.token, plan.passthrough_unmatched)

    out: list[str | None] = []
    for text, res, cell_spans in zip(batch, results, spans_for, strict=True):
        if text is None:
            out.append(None)
        elif res is None:
            out.append(
                fallback(
                    text,
                    cell_spans if cell_spans is not None else iter_spans(text, detector_ids),
                )
            )
        else:
            if text and plan.passthrough_unmatched:
                warn_passthrough_unmatched(_TEXT_MASK_LOG)
            out.append(res)
    return out


__all__ = ["SpliceKernel", "SplicePlan", "load_splice_kernel", "plan_native_splice", "splice_batch"]
