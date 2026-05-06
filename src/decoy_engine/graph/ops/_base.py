"""Op protocol shared by every kind in graph/ops/.

Each op module exposes:
    KIND: str                    - matches the YAML `kind:` value
    INPUT_ARITY: tuple[int, int] - (min, max). max=None means unbounded.
    OUTPUT_KIND: 'stream' | 'sink'
    validate_config(config)      - raise ValidationError on bad config
    apply(inputs, config, ctx)   - return a DataFrame (sources/transforms) or
                                   empty DataFrame after side effect (targets)
"""

from typing import Any, Protocol

import pandas as pd

from decoy_engine.context import ExecutionContext


class GraphOp(Protocol):
    KIND: str
    INPUT_ARITY: tuple[int, int | None]
    OUTPUT_KIND: str  # 'stream' or 'sink'

    def validate_config(self, config: dict[str, Any]) -> None: ...
    def apply(
        self,
        inputs: list[pd.DataFrame],
        config: dict[str, Any],
        ctx: ExecutionContext | None,
    ) -> pd.DataFrame: ...


class OpError(RuntimeError):
    """Raised by an op when its work fails. Caught by the runner and turned
    into a NodeRunRecord with status='error'."""
