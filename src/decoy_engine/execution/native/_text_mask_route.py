"""Route-shared text_mask adapter helpers: the fail-closed kill and the handler's warning (C6b-i).

Both native routes run the shipped `TextMaskHandler` per cell through `native_text_mask`
(`_kernels_scalar.py`), then surface the two things the per-cell mask does not build itself: the
fail-closed `StrategyError` the handler raises when a span cannot be FF1-encrypted and no
`sub_floor_span` policy is set, and the one aggregate sub-floor warning the handler builds OUTSIDE
`mask_cell`. These helpers live here so the unified route (`physical/_shadow_coordinator`,
`physical/_shadow_assembly`) and the chunked route (`_chunk_masking`) build the identical
`StrategyError` and `QualityWarning` and cannot drift from the oracle's.
"""

from __future__ import annotations

from collections.abc import Mapping

from decoy_engine.errors import FpeUnencryptableError
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning

__all__ = [
    "text_mask_fail_closed_error",
    "text_mask_sub_floor_warning",
]

# The exact note `TextMaskHandler.run` attaches, so the native warning's detail is byte-identical
# to the oracle's (the warning is compared field for field against the oracle's on both routes).
_SUB_FLOOR_NOTE = (
    "these spans' domain was below the FF1 minimum admissible "
    "domain, or failed checksum validation, and could not be "
    "FF1-encrypted; they were handled under the configured "
    "sub_floor_span policy instead (non-reversible)."
)


def text_mask_fail_closed_error(exc: FpeUnencryptableError, column: str) -> StrategyError:
    """The `StrategyError` a sub-floor fpe span with no `sub_floor_span` policy maps to.

    Reproduces `TextMaskHandler.run`'s `except FpeUnencryptableError` branch exactly (code,
    strategy, message), so the native route raises the identical error the oracle handler does.
    A separate function so mutation testing can grade the code + message mapping on its own."""
    return StrategyError(
        code="fpe_unencryptable_domain",
        strategy="text_mask",
        message=(
            f"column {column!r}: {exc}. The engine fails closed rather than "
            "silently choose a sub_floor_span fallback."
        ),
    )


def text_mask_sub_floor_warning(
    notices: Mapping[str, int], *, policy: str | None, column: str
) -> QualityWarning:
    """The one aggregate `text_mask_sub_floor_span_handled` warning for `notices`.

    Byte-identical to the warning `TextMaskHandler.run` builds for the same per-detector counts
    and policy, at the oracle's scope: the whole column (unified, counts summed across batches) or
    one oracle-equivalent chunk (chunked)."""
    return QualityWarning(
        code="text_mask_sub_floor_span_handled",
        provider="text_mask",
        column=column,
        detail={
            "policy": policy,
            "by_detector": dict(notices),
            "total": sum(notices.values()),
            "note": _SUB_FLOOR_NOTE,
        },
    )
