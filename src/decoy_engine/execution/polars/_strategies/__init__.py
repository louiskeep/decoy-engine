"""Polars-native strategy handlers for the polars execution adapter (S12).

Mirrors the pandas `execution/_strategies/` layout. `POLARS_SCALAR_HANDLERS`
maps a strategy name to its Polars-native handler instance. S12 migrates the 11
mask strategies one band at a time (cheap -> medium -> expensive); a strategy is
polars-native once it appears here AND in
`PolarsExecutionAdapter._POLARS_NATIVE_STRATEGIES`. Until a strategy migrates,
the adapter routes its jobs through the pandas oracle (S11 fallback).

Landed so far (cheap band): passthrough, redact, truncate, shuffle.
"""

from __future__ import annotations

from typing import Protocol

import polars as pl

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._strategies._bucketize import BucketizeStrategyHandler
from decoy_engine.execution._strategies._date_shift import DateShiftStrategyHandler
from decoy_engine.execution._strategies._faker import FakerStrategyHandler
from decoy_engine.execution._strategies._formula import FormulaStrategyHandler
from decoy_engine.execution._strategies._fpe import FpeStrategyHandler
from decoy_engine.execution._strategies._text_redact import TextRedactHandler
from decoy_engine.execution.polars._strategies._categorical import PolarsCategoricalStrategyHandler
from decoy_engine.execution.polars._strategies._hash import PolarsHashStrategyHandler
from decoy_engine.execution.polars._strategies._pandas_port import PandasStrategyPort
from decoy_engine.execution.polars._strategies._passthrough import PolarsPassthroughHandler
from decoy_engine.execution.polars._strategies._redact import PolarsRedactHandler
from decoy_engine.execution.polars._strategies._shuffle import PolarsShuffleStrategyHandler
from decoy_engine.execution.polars._strategies._truncate import PolarsTruncateHandler
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PolarsStrategyHandler(Protocol):
    """A single scalar masking strategy that operates on a pl.DataFrame."""

    name: str

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        """Mask `frame[column]` per the plan; return (frame, warnings)."""
        ...


POLARS_SCALAR_HANDLERS: dict[str, PolarsStrategyHandler] = {
    handler.name: handler
    for handler in (
        PolarsPassthroughHandler(),
        PolarsRedactHandler(),
        PolarsTruncateHandler(),
        PolarsShuffleStrategyHandler(),
        PolarsHashStrategyHandler(),
        PolarsCategoricalStrategyHandler(),
        # Backend/pandas-bound: shared primitive, container-only port (parity by
        # construction). The perf win for these is from S5/S7/S9, not substrate.
        # FPE shares the one pandas _encrypt primitive (the S9 signature-pin
        # carry-forward is satisfied by construction: a single keyed primitive,
        # not a per-substrate reimplementation). FPE output is chunk-count
        # invariant, so the default-chunk handler is correct for any adapter knob.
        PandasStrategyPort(DateShiftStrategyHandler()),
        PandasStrategyPort(BucketizeStrategyHandler()),
        PandasStrategyPort(FpeStrategyHandler()),
        # faker: the pool-backed primitive (PoolBuilder + vectorized PoolSampler)
        # is shared; the perf win is S5/S7, not substrate. formula: V1 safe-eval is
        # reused (reimplementing a security-sensitive eval is exactly the risk the
        # reuse rule avoids). The native Polars-expression formula compiler (a perf
        # optimization, spec S12 section 6) is deferred to a follow-on; this port
        # is the parity-safe migration that satisfies the migration gate.
        PandasStrategyPort(FakerStrategyHandler()),
        PandasStrategyPort(FormulaStrategyHandler()),
        # text_redact (MG-2, 2026-05-31): regex-iterating span splice runs on
        # plain Python strings; no native Polars expression equivalent in V1.
        # The PandasStrategyPort wrapper gives byte-identical parity by
        # construction (same handler, just converted frame).
        PandasStrategyPort(TextRedactHandler()),
    )
}

__all__ = [
    "POLARS_SCALAR_HANDLERS",
    "PandasStrategyPort",
    "PolarsCategoricalStrategyHandler",
    "PolarsHashStrategyHandler",
    "PolarsPassthroughHandler",
    "PolarsRedactHandler",
    "PolarsShuffleStrategyHandler",
    "PolarsStrategyHandler",
    "PolarsTruncateHandler",
]
