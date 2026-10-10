"""Route-shared FPE adapter helpers: the fail-closed kill and the Python warnings (C6a).

Both native routes hand an `fpe` column to the compiled FF1 kernel through `run_kernel_step`,
then apply two things the kernel deliberately does NOT: the fail-closed `StrategyError` kill on
the first per-row failure (C6a plan §3d), and the residual-risk warnings computed in Python at
the oracle's own invocation scope (§3e). These two helpers live here so the unified route
(`physical/_shadow_operators`, `physical/_shadow_coordinator`) and the chunked route
(`_chunk_masking`) apply the identical rule and cannot drift.

The kill's `StrategyError` carries the FIRST failing row's code (source order), matching the
shipped handler's first-failure raise (`_strategies/_fpe.py`): the kernel reproduces the pinned
validation order, so the same value yields the same code on both the native kernel and the
oracle. Warnings reuse the reference's own `_warnings` method, which wraps the shipped
`FpeStrategyHandler._residual_risk_warnings` plus the join-group note, so the native route's
warnings are byte-identical to the oracle's by construction and never ride the output.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from decoy_engine.execution._errors import StrategyError
from decoy_engine.kernel._scalar import _array_to_pylist, _is_missing

from ._crypto_ext import FpeConfig, FpeRowError
from ._crypto_reference import _ROW_ERROR_MESSAGES, _ReferenceFpe

if TYPE_CHECKING:
    import pyarrow as pa

    from decoy_engine.generation.pool._events import QualityWarning

__all__ = [
    "fpe_config_from_params",
    "fpe_fail_closed_error",
    "fpe_residual_warnings",
]

# One reference instance for its shipped-handler-backed warning method (stateless; mirrors
# `_ReferenceFpe._warner`). The kernel computes no warnings; this is the Python scope §3e names.
_WARNER = _ReferenceFpe()


def fpe_fail_closed_error(errors: tuple[FpeRowError, ...], column: str) -> StrategyError:
    """The `StrategyError` an fpe column's non-empty per-row error set maps to (C6a §3d).

    The code is the FIRST failing row's (lowest source-order index), matching the shipped
    handler's first-`FpeUnencryptableError`/`FpeChecksumError` raise. A separate function, not
    inline at the two call sites, so mutation testing can grade the first-failure selection and
    the per-row code mapping independently of the routes."""
    if not errors:  # pragma: no cover - callers check non-empty before building the kill
        raise AssertionError("fpe_fail_closed_error called with no errors")
    first = min(errors, key=lambda e: e.row_index)
    return StrategyError(
        code=first.code,
        strategy="fpe",
        message=(
            f"column {column!r}: {_ROW_ERROR_MESSAGES[first.code]} "
            "The engine fails closed rather than emit unmaskable or non-round-trip output."
        ),
    )


def _non_missing_strings(values: pa.Array | pa.ChunkedArray | list[Any]) -> list[str]:
    """The non-null, non-empty values as `str`, the oracle's own warning denominator.

    Reproduces `_ReferenceFpe._run`'s extraction exactly (`_array_to_pylist` + `_is_missing` +
    `str`, empty string treated as missing), so the warning counts match the oracle's."""
    out: list[str] = []
    for value in _array_to_pylist(values):
        if _is_missing(value):
            continue
        text = str(value)
        if text == "":
            continue
        out.append(text)
    return out


def fpe_residual_warnings(
    values: pa.Array | pa.ChunkedArray | list[Any], *, config: FpeConfig, column: str
) -> tuple[QualityWarning, ...]:
    """The residual-risk warnings for `values` under `config`, at this call's scope (C6a §3e).

    `values` are the ORIGINAL (pre-mask) column values at the oracle's invocation scope: the
    whole column on the unified route, one oracle-equivalent chunk on the chunked route. Reuses
    the reference's shipped-handler-backed `_warnings`, so the output is byte-identical to the
    oracle's and the denominator is the whole scope's non-null, non-empty values."""
    charset, preserve_sep, _validate_luhn, _checksum = config._resolve()
    return tuple(
        _WARNER._warnings(
            _non_missing_strings(values),
            charset=charset,
            preserve_sep=preserve_sep,
            column=column,
            join_group=config.join_group,
        )
    )


def fpe_config_from_params(params: Any) -> FpeConfig:
    """The `FpeConfig` a bound `FpeParams` resolves to (one place both routes build it)."""
    return FpeConfig(
        charset=params.charset,
        preserve_separators=params.preserve_separators,
        validate_luhn=params.validate_luhn,
        checksum=params.checksum,
        join_group=params.join_group,
    )
