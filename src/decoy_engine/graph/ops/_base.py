"""Op protocol shared by every kind in graph/ops/.

Each op module exposes:
    KIND: str                    - matches the YAML `kind:` value
    NATIVE_ENGINE: str           - 'pandas' | 'polars' | 'duckdb' | 'arrow'
                                   the substrate the op wants to run on. The
                                   runner converts the cached pyarrow.Table
                                   to this type before calling apply().
                                   Default: 'pandas'.
    INPUT_ARITY: tuple[int, int | None]
                                 - (min, max). max=None means unbounded.
    OUTPUT_KIND: 'stream' | 'sink' | 'split'
                                 - 'stream': op returns a table downstream.
                                   'sink': op writes to external storage and
                                   returns an empty stub. 'split': op routes
                                   rows to named output ports (requires
                                   OUTPUT_PORTS).
    OUTPUT_PORTS: tuple[str, ...]
                                 - Required when OUTPUT_KIND='split'. Names
                                   the downstream port labels (e.g.
                                   ('pass', 'fail')). Omit on stream/sink ops.
    HAS_SIDE_EFFECTS: bool       - True if the op writes to external storage
                                   (files, databases, cloud buckets, SFTP).
                                   The preview policy uses this to skip ops
                                   that must not run during a canvas preview.
                                   Required True on all ops with
                                   OUTPUT_KIND='sink'. Default: False.
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
    OUTPUT_KIND: str  # 'stream', 'sink', or 'split'
    HAS_SIDE_EFFECTS: bool  # True = writes external storage; required True on all sinks

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


def is_polars_frame(df: Any) -> bool:
    """True if df is a polars.DataFrame / LazyFrame, without importing polars
    when it isn't installed. Used by ops that ship both pandas and polars
    implementations to dispatch on input type."""
    return type(df).__module__.startswith("polars.")
