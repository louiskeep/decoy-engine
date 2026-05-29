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


def resolve_substrate() -> str:
    """Read + validate the DECOY_SUBSTRATE env var. Raises on an invalid value."""
    value = os.environ.get("DECOY_SUBSTRATE", _DEFAULT_SUBSTRATE).strip().lower()
    if value not in VALID_SUBSTRATES:
        raise ExecutionError(
            code="invalid_substrate",
            message=f"DECOY_SUBSTRATE must be one of {VALID_SUBSTRATES}; got {value!r}.",
        )
    return value


def select_execution_adapter(
    *,
    fpe_chunk_count: int = 4,
    max_workers: int = 4,
    fallback_to_pandas: bool = True,
) -> ExecutionAdapter:
    """Construct the execution adapter named by DECOY_SUBSTRATE.

    `max_workers` + `fallback_to_pandas` apply to the polars adapter only; the
    pandas adapter ignores them (it has no fallback and no runner-level
    parallelism knob at S11).
    """
    substrate = resolve_substrate()
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
