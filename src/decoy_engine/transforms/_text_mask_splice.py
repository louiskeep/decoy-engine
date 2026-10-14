"""The span-reassembly half of `text_mask`: gap policy plus the per-span splice loop.

Split out of `text_mask.py` so a caller that already holds a cell's resolved spans (the native
route, which gets them from the compiled detection kernel via `merge_text_redact_spans`) can skip
`iter_spans` and reuse the identical reassembly. Detection and splicing both stay byte-identical
to the oracle: this module changes where the spans come from, not what is built from them.

`mask_span` is injected rather than imported. The per-span strategies (fpe, faker, date_shift)
live in `text_mask.py`, which imports this module, so a direct import back would be circular.

Established methodology: span detection itself is the `regex` crate kernel in
`decoy-engine-native/src/text_redact.rs`, reached through `execution/native/_text_redact_kernel.py`;
nothing here re-implements it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Any

from decoy_engine.storm.detectors import Span

UNMATCHED_TOKEN: str = "[UNMATCHED]"  # noqa: S105 - sentinel for replace_with_token policy

MaskSpan = Callable[[Span, str], str]


def apply_unmatched(text: str, policy: str, token: str) -> str:
    """Apply the unmatched_span_policy to a non-PII text segment.

    ``redact`` (default): replace with ``token`` -- treats the segment as potentially undetected
        PII; safe but destroys surrounding context.
    ``passthrough``: keep verbatim -- operator opt-in for columns where the non-PII context is
        known safe.
    ``replace_with_token``: replace with ``UNMATCHED_TOKEN`` -- a lighter marker that
        distinguishes unmatched segments from per-span redaction tokens.

    Empty segments are returned as-is regardless of policy so two adjacent spans never emit a
    spurious token between them.
    """
    if not text:
        return text
    if policy == "passthrough":
        return text
    if policy == "replace_with_token":
        return UNMATCHED_TOKEN
    # "redact" and any unknown policy: use token (fail-safe).
    return token


def warn_passthrough_unmatched(log: logging.Logger) -> None:
    """The per-cell caution for `unmatched_span_policy='passthrough'`.

    The caller passes `text_mask`'s own logger so the record keeps its name, and the native
    splice (which never enters `mask_cell_with_spans`) can emit the identical warning.
    """
    log.warning(
        "text_mask: unmatched_span_policy='passthrough' lets any text not covered by "
        "a detected span ride through unchanged. Built-in span detection covers only "
        "11 detectors (email, ssn, us_phone, us_zip, pan, iban, ipv4, icd10, npi, "
        "url, street_address). Names, addresses, and dates require NER (extra_spans=) "
        "or custom= patterns. Use the default 'redact' policy unless this column is "
        "known-safe."
    )


def splice_spans(
    text: str,
    spans: Sequence[Span],
    *,
    mask_span: Callable[[Span], str],
    unmatched_span_policy: str,
    token: str,
) -> str:
    """Reassemble `text` from resolved, non-overlapping, start-ordered `spans`.

    Gaps take the unmatched policy and spans take `mask_span`, so the gap token and the span
    token stay distinct. A cell with no spans is one gap.
    """
    if not spans:
        return apply_unmatched(text, unmatched_span_policy, token)

    parts: list[str] = []
    cursor = 0
    for span in spans:
        if span.start > cursor:
            parts.append(apply_unmatched(text[cursor : span.start], unmatched_span_policy, token))
        parts.append(mask_span(span))
        cursor = span.end

    if cursor < len(text):
        parts.append(apply_unmatched(text[cursor:], unmatched_span_policy, token))
    return "".join(parts)


__all__: list[Any] = [
    "UNMATCHED_TOKEN",
    "MaskSpan",
    "apply_unmatched",
    "splice_spans",
    "warn_passthrough_unmatched",
]
