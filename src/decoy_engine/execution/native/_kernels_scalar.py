"""Native array-to-array kernels for the non-keyed row-local strategies.

`passthrough`, `redact`, and `truncate` need no Rust: they are pure per-value
transforms over one Arrow array with no cross-row state and no keyed draw, so
lowering them means calling the existing `kernel/_scalar.py` functions
directly instead of round-tripping through a pandas column and `to_pylist()`
the way `_strategies/_redact.py` and `_strategies/_truncate.py` do. Reusing
those functions (rather than re-expressing their per-value logic here) is
what makes native output byte-identical to the shipped handlers: there is one
logic source, and this module is just its Arrow-native entry point.

`text_redact` is the one exception to "calls `kernel/_scalar.py`": its per-cell span logic lives
in the oracle handler and the pre-scan detectors, so `native_text_redact` calls `iter_spans` and
`_splice` from there. It runs the same Python per cell as the oracle, so the column's own speed
is unchanged; the gain is that its table stays native.

`truncate`'s fail-closed config validation lives in `TruncateHandler.run`,
not in `kernel.truncate_array` (the kernel trusts its caller). A native
caller bypasses the handler entirely, so this module re-raises the same
`StrategyError` codes (`truncate_length_invalid` / `truncate_keep_invalid` /
`truncate_mask_char_invalid`) the handler raises, ahead of calling the
kernel, so an invalid config still fails closed instead of running with a
meaningless length/keep/mask_char triple.

Output type: these kernels emit each strategy's natural, per-batch-stable Arrow
type -- `pa.string()` for truncate and for the admitted string-redact contract,
across value-bearing, all-null, and empty batches alike, so the out-of-core writer
can concatenate a column's batches under one schema. `truncate_array` already forces
`pa.string()`; `redact_array` infers (null for all-null/empty), so `native_redact`
pins the string case here.

The pandas oracle's `pa.Table.from_pandas` round-trip infers a DIFFERENT type in three
cases. Two are degenerate but realistic streaming batch shapes: an all-null column
becomes null-type, and an empty (zero-row) column becomes double (pandas' empty
default). There native emits the stable string a streaming column needs and the
oracle's type is the pandas inference artifact -- reconciling it is a route-integration
concern (Task 2.6/2.7). The third is genuinely out of the admitted contract: a
non-string `redact_with` (no shipped disguise uses one) stays inferred, so native keeps
the value's own type (int+null -> int64) while pandas promotes to double; eligibility
excludes it before production routing. See `tests/native/test_kernels_scalar.py` for the
pinned divergences and the batch-schema-stability test.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa

from decoy_engine.detection.detectors import Span, iter_spans
from decoy_engine.errors import FpeUnencryptableError
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._text_redact import _splice
from decoy_engine.execution.native._text_mask_native_fpe import (
    encrypt_spans,
    has_native_span,
    mask_cell_hybrid,
    plan_native_fpe,
)
from decoy_engine.execution.native._text_mask_native_splice import (
    load_splice_kernel,
    plan_native_splice,
    splice_batch,
)
from decoy_engine.execution.native._text_mask_route import text_mask_fail_closed_error
from decoy_engine.execution.native._text_redact_kernel import (
    load_text_redact_kernel,
    merge_text_redact_spans,
    requested_rust_ids,
)
from decoy_engine.kernel import passthrough_array, redact_array, truncate_array
from decoy_engine.transforms.text_mask import mask_cell, mask_cell_with_spans


def native_passthrough(array: pa.Array | pa.ChunkedArray) -> pa.Array:
    """Return `array` unchanged (the passthrough strategy is a no-op)."""
    return passthrough_array(array)


def native_redact(
    array: pa.Array | pa.ChunkedArray | list[Any],
    *,
    redact_with: Any = "REDACTED",
) -> pa.Array:
    """Replace every non-null value in `array` with `redact_with`; nulls stay null.

    `redact_array` lets Arrow infer the output type, which is null for an all-null or
    empty batch and string only once a value is present. That per-batch instability
    would break the out-of-core writer, which concatenates a column's batches under one
    schema. For the admitted string-redact contract (every shipped disguise), pin the
    output to `pa.string()` so every batch of a redact column carries the same type,
    matching `native_truncate` and the native keyed kernel. A non-string `redact_with`
    is outside the admitted set (enforced at eligibility); its inferred type is left
    untouched and characterized in the tests.
    """
    result = redact_array(array, redact_with=redact_with)
    if isinstance(redact_with, str) and result.type != pa.string():
        return result.cast(pa.string())
    return result


def native_truncate(
    array: pa.Array | pa.ChunkedArray | list[Any],
    *,
    length: int,
    keep: str = "head",
    mask_char: str | None = None,
) -> pa.Array:
    """Truncate `array` to `length` chars, keeping the head or tail; nulls stay null.

    Validation order matches `TruncateHandler.run` exactly (length, then
    keep, then mask_char) so the first violation in a doubly-invalid config
    raises the same code from both paths.
    """
    if not isinstance(length, int) or length < 1:
        raise StrategyError(
            code="truncate_length_invalid",
            strategy="truncate",
            message=(
                f"truncate requires an integer length >= 1, got {length!r} "
                f"({type(length).__name__})."
            ),
        )
    if keep not in ("head", "tail"):
        raise StrategyError(
            code="truncate_keep_invalid",
            strategy="truncate",
            message=f"truncate requires keep in ('head', 'tail'), got {keep!r}.",
        )
    if mask_char is not None and (not isinstance(mask_char, str) or len(mask_char) != 1):
        raise StrategyError(
            code="truncate_mask_char_invalid",
            strategy="truncate",
            message=(
                f"truncate requires mask_char to be a single character, got "
                f"{mask_char!r} ({type(mask_char).__name__})."
            ),
        )
    return truncate_array(array, length=length, keep=keep, mask_char=mask_char)


# Cells per kernel call: bounds the resident candidate-list memory independent of column size.
# Large enough that per-call overhead is negligible against the per-cell scan cost.
_TEXT_REDACT_KERNEL_BATCH = 65_536


def native_text_redact(
    array: pa.Array | pa.ChunkedArray,
    *,
    detectors: tuple[str, ...] | None,
    token: str,
    label_token: bool,
) -> pa.Array:
    """Replace PII spans in every non-null cell; nulls stay null.

    C6c-ii (plus the lookaround port) routes all eleven built-in detectors into the compiled
    companion for cells in the ASCII-safe domain and keeps the Python path for every other cell
    (`_text_redact_kernel`), so the spliced output is byte-identical to the oracle's `iter_spans` +
    `_splice` always. With no companion (or an older/catalog-skewed one) the loader returns `None`
    and the whole column runs the literal Python path, unchanged from C6c-i.

    Splicing has one implementation (`_splice`) and the empty-means-all rule is the resolver's: an
    empty tuple here runs zero detectors, as `iter_spans([])` does. Output is `pa.string()`; each
    route's assembly decides the type of an empty or all-null column.
    """
    detector_ids = list(detectors) if detectors is not None else None
    # C6c-i's stringification contract: a null stays null, every other cell is a string the kernel
    # and the splice both read, so their offsets land in the same text.
    texts: list[str | None] = [
        None if v is None else (v if isinstance(v, str) else str(v)) for v in array.to_pylist()
    ]

    kernel = load_text_redact_kernel()
    if kernel is None:
        out: list[str | None] = []
        for text in texts:
            if text is None:
                out.append(None)
                continue
            spans = iter_spans(text, detector_ids, extra_spans=None)
            out.append(text if not spans else _splice(text, spans, token, label_token))
        return pa.array(out, type=pa.string())

    from decoy_engine.execution.native._text_redact_kernel import CATALOG_VERSION

    rust_ids = requested_rust_ids(detector_ids)
    # Call the kernel in fixed sub-batches so only one batch of candidate lists is resident at
    # once, not the whole column's. The full-frame route hands this the entire column (text_redact
    # is admitted there, not only chunked), so an unbatched call grows peak RSS with row count and
    # breaks the C6c-ii RSS budget on large text columns; per-cell results are unchanged.
    routed: list[str | None] = []
    for base in range(0, len(texts), _TEXT_REDACT_KERNEL_BATCH):
        batch = texts[base : base + _TEXT_REDACT_KERNEL_BATCH]
        per_cell = kernel.text_redact_candidates(batch, rust_ids, CATALOG_VERSION)
        for text, cands in zip(batch, per_cell, strict=True):
            if text is None:
                routed.append(None)
                continue
            if cands is None:
                # Ineligible cell: the full Python path, byte-identical to the oracle.
                spans = iter_spans(text, detector_ids, extra_spans=None)
            else:
                spans = merge_text_redact_spans(text, detector_ids, rust_candidates=cands)
            routed.append(text if not spans else _splice(text, spans, token, label_token))
    return pa.array(routed, type=pa.string())


def native_text_mask(
    array: pa.Array | pa.ChunkedArray,
    *,
    mask_key: bytes | None,
    column: str,
    detectors: tuple[str, ...] | None,
    per_detector_strategy: Mapping[str, str] | None,
    unmatched_span_policy: str,
    token: str,
    date_shift_bounds: tuple[tuple[str, int | None], ...],
    sub_floor_span_policy: str | None,
) -> tuple[pa.Array, dict[str, int]]:
    """Mask PII spans in every non-null cell, reproducing `TextMaskHandler.run` per cell.

    Detection comes from the compiled span kernel (`merge_text_redact_spans`, the same seam
    `native_text_redact` uses); the per-span splice is still the Python `mask_cell_with_spans`.
    Kernel absent or catalog skew runs `mask_cell` itself, and an ineligible cell takes
    `iter_spans`, so every fallback is the oracle's own path.

    text_mask is KEYED and handler-rich, so this is not a bare `mask_cell` loop: it resolves and
    passes `mask_key` (text_mask keys its fpe/faker/date_shift spans off it), threads ONE
    `sub_floor_notices` dict across the whole call exactly as the handler does, and reproduces the
    handler's `except FpeUnencryptableError` branch so a fail-closed span raises the identical
    `StrategyError(code='fpe_unencryptable_domain', strategy='text_mask')`. It returns that notices
    dict beside the output so the route transports the handler's one aggregate sub-floor warning
    (built OUTSIDE `mask_cell`); the warning never rides the output.

    Output is `pa.string()` on every batch (nulls stay null), like `native_text_redact`; each
    route's assembly decides an empty or all-null column's type. The source is string-only by
    admission, so the `str()` coercion is the handler's own null-safe identity for strings. `ner`
    declines to the oracle at admission (3c), so this reproduces only the non-NER handler path.
    """
    if mask_key is None:  # pragma: no cover - the routes always thread the resolved mask key
        raise AssertionError(
            "native_text_mask reached with mask_key=None; text_mask is keyed and the routes "
            "resolve a concrete mask key (the job seed when no secret) before dispatch."
        )
    strategy_map = dict(per_detector_strategy) if per_detector_strategy else None
    # Mirror the handler's `for key in ("min_days","max_days"): if key in cfg` exactly: a bound
    # that is PRESENT-but-null passes through (so the per-span date_shift crashes identically to
    # the oracle) rather than silently defaulting when the key is absent (literal flag-on/off parity).
    extra: dict[str, Any] = dict(date_shift_bounds)
    detector_ids = list(detectors) if detectors is not None else None
    sub_floor_notices: dict[str, int] = {}
    texts = [
        None if v is None else (v if isinstance(v, str) else str(v)) for v in array.to_pylist()
    ]
    kernel = load_text_redact_kernel()
    out: list[Any] = []

    def mask(text: str, spans: list[Span] | None) -> str:
        # `spans is None` is the oracle's own path (kernel absent), so that branch is `mask_cell`
        # itself and stays byte-identical to it.
        if spans is None:
            return mask_cell(
                text,
                mask_key,
                detector_ids=detector_ids,
                extra_spans=None,
                strategy_map=strategy_map,
                unmatched_span_policy=unmatched_span_policy,
                token=token,
                cfg=extra or None,
                sub_floor_span_policy=sub_floor_span_policy,
                sub_floor_notices=sub_floor_notices,
            )
        return mask_cell_with_spans(
            text,
            spans,
            mask_key,
            strategy_map=strategy_map,
            unmatched_span_policy=unmatched_span_policy,
            token=token,
            cfg=extra or None,
            sub_floor_span_policy=sub_floor_span_policy,
            sub_floor_notices=sub_floor_notices,
        )

    try:
        if kernel is None:
            out = [None if t is None else mask(t, None) for t in texts]
        else:
            from decoy_engine.execution.native._text_redact_kernel import CATALOG_VERSION

            rust_ids = requested_rust_ids(detector_ids)
            splice = load_splice_kernel()
            plan = (
                plan_native_splice(detector_ids, strategy_map, unmatched_span_policy, token)
                if splice is not None
                else None
            )
            fpe_detectors = plan_native_fpe(detector_ids, strategy_map)
            # Fixed sub-batches bound the resident candidate lists, as in `native_text_redact`.
            for base in range(0, len(texts), _TEXT_REDACT_KERNEL_BATCH):
                batch = texts[base : base + _TEXT_REDACT_KERNEL_BATCH]
                if plan is not None and splice is not None:
                    # Slice b: redact/passthrough configs splice in Rust; a declined cell runs
                    # the same Python splice as below.
                    out.extend(
                        splice_batch(
                            batch,
                            plan=plan,
                            splice=splice,
                            kernel=kernel,
                            detector_ids=detector_ids,
                            rust_ids=rust_ids,
                            fallback=mask,
                        )
                    )
                    continue
                per_cell = kernel.text_redact_candidates(batch, rust_ids, CATALOG_VERSION)
                cell_spans: list[list[Span]] = []
                # Spans of kernel-eligible (ASCII-safe) cells only: a non-ASCII cell never
                # reaches the compiled FF1 path, so its fpe spans stay on `_mask_span`.
                native_cells: list[list[Span] | None] = []
                for text, cands in zip(batch, per_cell, strict=True):
                    if text is None:
                        cell_spans.append([])
                        native_cells.append(None)
                    elif cands is None:
                        # Ineligible cell (non-ASCII or a 0x1c-0x1f control): full Python.
                        cell_spans.append(iter_spans(text, detector_ids, extra_spans=None))
                        native_cells.append(None)
                    else:
                        merged = merge_text_redact_spans(text, detector_ids, rust_candidates=cands)
                        cell_spans.append(merged)
                        native_cells.append(merged)
                fpe_out = encrypt_spans(native_cells, fpe_detectors, mask_key)
                for text, spans, native in zip(batch, cell_spans, native_cells, strict=True):
                    if text is None:
                        out.append(None)
                    elif native is not None and text and has_native_span(native, fpe_out):
                        # Slice c: native FF1 for the plain-digit spans, `_mask_span` for the rest.
                        out.append(
                            mask_cell_hybrid(
                                text,
                                native,
                                fpe_out,
                                mask_key,
                                strategy_map=strategy_map,
                                unmatched_span_policy=unmatched_span_policy,
                                token=token,
                                cfg=extra or None,
                                sub_floor_span_policy=sub_floor_span_policy,
                                sub_floor_notices=sub_floor_notices,
                            )
                        )
                    else:
                        out.append(mask(text, spans))
    except FpeUnencryptableError as exc:
        raise text_mask_fail_closed_error(exc, column) from exc
    return pa.array(out, type=pa.string()), sub_floor_notices


__all__ = [
    "native_passthrough",
    "native_redact",
    "native_text_mask",
    "native_text_redact",
    "native_truncate",
]
