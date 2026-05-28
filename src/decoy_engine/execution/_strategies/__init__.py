"""Strategy handlers for the pandas execution adapter (engine-v2 S9).

`SCALAR_HANDLERS` maps a strategy name to a handler instance. Slice 2a ships the
three no-backend strategies (passthrough, redact, truncate); later slices add
the keyed/backend strategies (faker, hash, date_shift, bucketize, categorical,
shuffle, formula, fpe) re-keyed onto S3's `derive`/`derive_index` + S5's
`PoolSampler`.
"""

from __future__ import annotations

from decoy_engine.execution._adapter import StrategyHandler
from decoy_engine.execution._strategies._passthrough import PassthroughHandler
from decoy_engine.execution._strategies._redact import RedactHandler
from decoy_engine.execution._strategies._truncate import TruncateHandler

SCALAR_HANDLERS: dict[str, StrategyHandler] = {
    handler.name: handler for handler in (PassthroughHandler(), RedactHandler(), TruncateHandler())
}

__all__ = [
    "SCALAR_HANDLERS",
    "PassthroughHandler",
    "RedactHandler",
    "TruncateHandler",
]
