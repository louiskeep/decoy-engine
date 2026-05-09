"""Op protocol shared by every kind in graph/ops/.

Each op module exposes:
    KIND: str                    - matches the YAML `kind:` value
    NATIVE_ENGINE: str           - 'pandas' | 'polars' | 'duckdb' | 'arrow'
                                   the substrate the op wants to run on. The
                                   runner converts the cached pyarrow.Table
                                   to this type before calling apply().
                                   Default: 'pandas'.
    INPUT_ARITY: tuple[int, int] - (min, max). max=None means unbounded.
    OUTPUT_KIND: 'stream' | 'sink'
    validate_config(config)      - raise ValidationError on bad config
    apply(inputs, config, ctx)   - return data (DataFrame / pa.Table) in the
                                   declared NATIVE_ENGINE's type. Sources /
                                   transforms return data; sinks return an
                                   empty value of the same type after the
                                   side effect.
"""

from typing import Any, Literal, Protocol

import pandas as pd

from decoy_engine.context import ExecutionContext

NativeEngine = Literal["pandas", "polars", "duckdb", "arrow"]


class GraphOp(Protocol):
    KIND: str
    NATIVE_ENGINE: NativeEngine
    INPUT_ARITY: tuple[int, int | None]
    OUTPUT_KIND: str  # 'stream' or 'sink'

    def validate_config(self, config: dict[str, Any]) -> None: ...
    def apply(
        self,
        inputs: list[Any],
        config: dict[str, Any],
        ctx: ExecutionContext | None,
    ) -> Any: ...


class OpError(RuntimeError):
    """Raised by an op when its work fails. Caught by the runner and turned
    into a NodeRunRecord with status='error'."""
