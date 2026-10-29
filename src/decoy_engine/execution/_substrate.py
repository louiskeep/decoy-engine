"""Execution-adapter selection.

Pandas is the only masking adapter and the parity oracle every native route is
graded against; which compute backend runs a strategy is decided by the routing
layers and the native operator registry, never by a caller-chosen "substrate"
(see docs/execution-backends.md). The former `substrate` / `DECOY_SUBSTRATE`
knob and the `max_workers` / `fallback_to_pandas` no-ops were removed pre-GA.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from decoy_engine.execution._errors import ExecutionError

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionAdapter

# The adapter label stamped into plans, telemetry and evidence. One value.
SUBSTRATE = "pandas"


def require_positive_int(name: str, value: int) -> None:
    """Fail-fast typed validation for runtime count knobs. bool is excluded
    explicitly because it passes an `isinstance(..., int)` check while being a
    config mistake (`fpe_chunk_count: true`), the same silent-coercion trap the
    job-seed normalizer closes."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExecutionError(
            code="invalid_execution_knob",
            message=f"{name} must be a positive int; got {value!r}.",
        )


def require_bool(name: str, value: bool) -> None:
    """Fail-fast typed validation for boolean knobs. A str like ``"false"`` is
    truthy, so an untyped job-payload value would silently invert the intended
    behavior; reject anything that is not a real bool."""
    if not isinstance(value, bool):
        raise ExecutionError(
            code="invalid_execution_knob",
            message=f"{name} must be a bool; got {value!r}.",
        )


def select_execution_adapter(*, fpe_chunk_count: int = 4) -> ExecutionAdapter:
    """Construct the pandas execution adapter.

    Raises:
        ExecutionError: ``code='invalid_execution_knob'`` for a non-positive-int
            `fpe_chunk_count`, BEFORE any adapter work so callers fail at
            selection time, not mid-job.
    """
    require_positive_int("fpe_chunk_count", fpe_chunk_count)
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

    return PandasExecutionAdapter(fpe_chunk_count=fpe_chunk_count)


__all__ = [
    "SUBSTRATE",
    "require_bool",
    "require_positive_int",
    "select_execution_adapter",
]
