"""DECOY_SUBSTRATE flag + execution-adapter selection (engine-v2 S11).

The flag picks which `ExecutionAdapter` the runner instantiates. Per PQ6
(PO-ratified 2026-05-28) the default was `pandas` through S12 and FLIPPED to
`polars` at S13 close, once all 11 strategies were polars-native and parity-green.
The flag mechanism shipped in S11; the DEFAULT flipped at S13 (this module).

The flip changes ONLY this default. FK + composite jobs are not yet polars-native
(deferred V2+), so the polars adapter keeps `fallback_to_pandas=True` and routes
them through the pandas oracle (byte-for-byte identical, recorded as such, not a
silent downgrade). See `polars/_polars_adapter.py` for that disposition.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from decoy_engine.execution._errors import ExecutionError

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionAdapter

VALID_SUBSTRATES = ("pandas", "polars")
_DEFAULT_SUBSTRATE = "polars"


def resolve_substrate(override: str | None = None) -> str:
    """Validate `override` when given, else read the DECOY_SUBSTRATE env var.

    Raises:
        ExecutionError: ``code='invalid_substrate'`` when the resolved
            value is not one of ``VALID_SUBSTRATES``.
    """
    if override is not None and not isinstance(override, str):
        raise ExecutionError(
            code="invalid_substrate",
            message=f"substrate override must be a str or None; got {override!r}.",
        )
    raw = override if override is not None else os.environ.get("DECOY_SUBSTRATE")
    value = (raw if raw is not None else _DEFAULT_SUBSTRATE).strip().lower()
    if value not in VALID_SUBSTRATES:
        source = "substrate override" if override is not None else "DECOY_SUBSTRATE"
        raise ExecutionError(
            code="invalid_substrate",
            message=f"{source} must be one of {VALID_SUBSTRATES}; got {value!r}.",
        )
    return value


def _require_positive_int(name: str, value: int) -> None:
    """Fail-fast typed validation for runtime count knobs, mirroring
    `resolve_substrate`'s coded-error contract. bool is excluded explicitly
    because it passes an `isinstance(..., int)` check while being a config
    mistake (`fpe_chunk_count: true`), the same silent-coercion trap the
    job-seed normalizer closes."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExecutionError(
            code="invalid_execution_knob",
            message=f"{name} must be a positive int; got {value!r}.",
        )


def _require_bool(name: str, value: bool) -> None:
    """Fail-fast typed validation for boolean knobs. A str like ``"false"`` is
    truthy, so an untyped job-payload value would silently invert the intended
    behavior (a caller wanting fail-closed would get fallback enabled); reject
    anything that is not a real bool, mirroring `_require_positive_int`."""
    if not isinstance(value, bool):
        raise ExecutionError(
            code="invalid_execution_knob",
            message=f"{name} must be a bool; got {value!r}.",
        )


def select_execution_adapter(
    *,
    substrate: str | None = None,
    fpe_chunk_count: int = 4,
    max_workers: int = 4,
    fallback_to_pandas: bool = True,
) -> ExecutionAdapter:
    """Construct the execution adapter for `substrate` (default: DECOY_SUBSTRATE).

    `max_workers` + `fallback_to_pandas` apply to the polars adapter only; the
    pandas adapter ignores them (it has no fallback and no runner-level
    parallelism knob at S11). An explicit `substrate` overrides the env var;
    None keeps the env-resolved behavior unchanged.

    Raises:
        ExecutionError: ``code='invalid_substrate'`` for an unknown or non-str
            substrate; ``code='invalid_execution_knob'`` for a non-positive-int
            count knob or a non-bool `fallback_to_pandas`. All raise BEFORE any
            adapter work so callers fail at selection time, not mid-job.
    """
    _require_positive_int("fpe_chunk_count", fpe_chunk_count)
    _require_positive_int("max_workers", max_workers)
    _require_bool("fallback_to_pandas", fallback_to_pandas)
    substrate = resolve_substrate(substrate)
    if substrate == "polars":
        from decoy_engine.execution.polars._polars_adapter import PolarsExecutionAdapter

        return PolarsExecutionAdapter(
            max_workers=max_workers,
            fpe_chunk_count=fpe_chunk_count,
            fallback_to_pandas=fallback_to_pandas,
        )
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

    return PandasExecutionAdapter(fpe_chunk_count=fpe_chunk_count)


__all__ = ["VALID_SUBSTRATES", "resolve_substrate", "select_execution_adapter"]
