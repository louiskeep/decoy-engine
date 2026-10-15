"""C6b-ii slice c: run the plain-digit `fpe` text_mask spans through the compiled FF1 kernel.

`text_mask`'s `fpe` span strategy is `transforms.text_mask._mask_fpe`: FF1 over a per-detector key
and tweak, digits charset, separators preserved. Pure-Python FF1 is the cost of an `fpe` cell, so
this module encrypts the eligible spans of a whole batch in the companion and lets the Python
splice consume the results; every other span still runs `_mask_span` exactly as before.

Which spans are native (per SPAN, so one cell can mix native and Python spans):
- detector in `NATIVE_FPE_DETECTORS` (digits charset, no checksum scheme) with strategy `fpe`.
  Checksum detectors (pan, npi) never reach the kernel: `fpe_transform_batch` has no checksum
  contract, so the exclusion is enforced here by deriving eligibility from `_FPE_CONFIG`.
- the kernel accepted the value. A span it rejects (sub-floor domain such as a 5-digit ZIP, or
  any other per-row status) is DECLINED to `_mask_span`, which then raises or applies the
  `sub_floor_span` policy and counts the notice exactly once, in the oracle's order. The kernel
  produces no side effect for a declined span, and a whole-batch kernel error declines every span
  for the same reason.

Recipe, identical to `_mask_fpe` (the matched-text HMAC `_span_key` never enters FF1):
`derive(mask_key, f"text.{detector_id}", FF1_KEY_LABEL)` is done inside the kernel from the
namespace; the tweak is `build_ff1_tweak(FF1_TWEAK_SCOPE_TEXT_SPAN, detector_id)`; digits
charset; `preserve_separators=True`; `validate_luhn=False`.

Established methodology: NIST SP 800-38G FF1 over AES-256 via the `fpe` crate, reached through the
existing `fpe_transform_batch` entry point (`decoy-engine-native/src/fpe.rs`, proven against the
Python oracle by its KAT gate and re-checked by `load_compiled_fpe_kernel` at load). Nothing new
is implemented in Rust.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pyarrow as pa

from decoy_engine.storm.detectors import Span
from decoy_engine.transforms._text_mask_splice import splice_spans, warn_passthrough_unmatched
from decoy_engine.transforms.fpe import FF1_TWEAK_SCOPE_TEXT_SPAN, build_ff1_tweak
from decoy_engine.transforms.text_mask import _CHARSETS, _FPE_CONFIG, DETECTOR_DEFAULTS, _mask_span

_TEXT_MASK_LOG = logging.getLogger("decoy_engine.transforms.text_mask")

# Digits, no checksum scheme: the only `_FPE_CONFIG` shape the kernel reproduces. fax_number
# satisfies this too but no built-in span detector emits it, so listing it would be dead weight.
NATIVE_FPE_DETECTORS: frozenset[str] = frozenset(
    d
    for d, (charset, checksum) in _FPE_CONFIG.items()
    if checksum is None and charset == "digits" and d != "fax_number"
)

FpeOutputs = Mapping[tuple[str, str], str]  # (detector_id, matched_text) -> encrypted text


@functools.lru_cache(maxsize=1)
def load_fpe_batch() -> Callable[..., Any] | None:
    """The companion's `fpe_transform_batch`, or None (absent / skewed / failed its load-time KAT).

    `load_compiled_fpe_kernel` runs the ABI check and a known-answer vector, so a stale or
    mis-built binary yields None here and every span stays on the Python path.
    """
    try:
        from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel

        load_compiled_fpe_kernel()
        from decoy_engine_native import _kernel

        return _kernel.fpe_transform_batch
    except Exception:
        return None


def plan_native_fpe(
    detector_ids: Sequence[str] | None, strategy_map: Mapping[str, str] | None
) -> frozenset[str]:
    """Detectors whose spans are candidates for the kernel under this config (possibly empty).

    `detector_ids=None` means every detector. Mirrors `mask_cell_with_spans`: the strategy map
    overlays `DETECTOR_DEFAULTS`, and only an exact `"fpe"` string selects FF1.
    """
    effective: dict[str, Any] = dict(DETECTOR_DEFAULTS)
    if strategy_map:
        effective.update(strategy_map)
    requested = NATIVE_FPE_DETECTORS if detector_ids is None else set(detector_ids)
    return frozenset(
        d
        for d in NATIVE_FPE_DETECTORS & requested
        if isinstance(effective.get(d), str) and effective[d] == "fpe"
    )


def encrypt_spans(
    spans_by_cell: Sequence[Sequence[Span] | None],
    detectors: frozenset[str],
    mask_key: bytes,
) -> dict[tuple[str, str], str]:
    """FF1-encrypt every distinct eligible matched text in the batch, one kernel call per detector.

    Returns only the spans the kernel accepted; a rejected value is simply absent, which sends it
    to `_mask_span`. Equal matched texts share one result because FF1 here depends only on
    (key, tweak, text).
    """
    fpe_fn = load_fpe_batch() if detectors else None
    if fpe_fn is None:
        return {}
    wanted: dict[str, dict[str, None]] = {}  # detector -> ordered distinct texts
    for spans in spans_by_cell:
        for span in spans or ():
            if span.detector_id in detectors and span.matched_text.isascii():
                wanted.setdefault(span.detector_id, {})[span.matched_text] = None
    out: dict[tuple[str, str], str] = {}
    for det_id, texts_map in wanted.items():
        texts = list(texts_map)
        charset = _CHARSETS[_FPE_CONFIG[det_id][0]]
        try:
            enc, errors = fpe_fn(
                pa.array(texts, type=pa.string()),
                mask_key=mask_key,
                namespace=f"text.{det_id}",
                tweak=build_ff1_tweak(FF1_TWEAK_SCOPE_TEXT_SPAN, det_id),
                charset=charset,
                preserve_separators=True,
                validate_luhn=False,
                forward=True,
                native_threads=None,
            )
        except Exception:
            # Any kernel failure -- a coded ValueError (bad seed length, ...) or an unexpected
            # companion-side error -- declines the whole detector to the proven oracle, which
            # either reproduces the value byte-identically or raises/handles sub-floor from its
            # own call site in its own order. Fail safe to the byte-exact Python path.
            continue
        rejected = {int(i) for i, _ in errors}
        for i, (text, value) in enumerate(zip(texts, enc.to_pylist(), strict=True)):
            if i not in rejected and value is not None:
                out[(det_id, text)] = value
    return out


def has_native_span(spans: Sequence[Span], outputs: FpeOutputs) -> bool:
    return any((s.detector_id, s.matched_text) in outputs for s in spans)


def mask_cell_hybrid(
    text: str,
    spans: Sequence[Span],
    outputs: FpeOutputs,
    mask_key: bytes,
    *,
    strategy_map: Mapping[str, str] | None,
    unmatched_span_policy: str,
    token: str,
    cfg: Mapping[str, Any] | None,
    sub_floor_span_policy: str | None,
    sub_floor_notices: dict[str, int] | None,
) -> str:
    """`mask_cell_with_spans` with the kernel's FF1 results substituted for the native spans.

    Same prelude and splice as the oracle for a non-empty string cell; a span absent from
    `outputs` goes through `_mask_span` unchanged, so it raises, counts notices, and logs in span
    order exactly as in the all-Python run.
    """
    if unmatched_span_policy == "passthrough":
        warn_passthrough_unmatched(_TEXT_MASK_LOG)
    effective_map: dict[str, str] = dict(DETECTOR_DEFAULTS)
    if strategy_map:
        effective_map.update(strategy_map)
    extra_cfg: dict[str, Any] = dict(cfg or {})
    extra_cfg.setdefault("token", token)

    def mask_span(span: Span) -> str:
        strategy = effective_map.get(span.detector_id, "redact")
        if strategy == "fpe":
            native = outputs.get((span.detector_id, span.matched_text))
            if native is not None:
                return native
        return _mask_span(
            span,
            mask_key,
            strategy,
            extra_cfg,
            sub_floor_span_policy=sub_floor_span_policy,
            sub_floor_notices=sub_floor_notices,
        )

    return splice_spans(
        text,
        spans,
        mask_span=mask_span,
        unmatched_span_policy=unmatched_span_policy,
        token=token,
    )


__all__ = [
    "NATIVE_FPE_DETECTORS",
    "encrypt_spans",
    "has_native_span",
    "load_fpe_batch",
    "mask_cell_hybrid",
    "plan_native_fpe",
]
