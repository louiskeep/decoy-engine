"""Re-export shim: `LazySource` now lives in `decoy_engine.profile._readers`.

SC7a (consultant-2026-07-09 F1) promoted `LazySource` to the shared profile
readers module so profiling and the out-of-core runner use one lazy-Parquet
reader instead of two copies. This shim keeps the out-of-core package's
historical import path (`out_of_core._source.LazySource`) pointing at that
single definition. Pre-GA, an internal import path may move like this without
a compatibility shim owed; the shim is kept only to localize the move to one
line rather than churn every out-of-core module.
"""

from __future__ import annotations

from decoy_engine.profile._readers import LazySource

__all__ = ["LazySource"]
