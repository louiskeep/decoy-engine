"""PandasStrategyPort: run a pandas StrategyHandler against a pl.DataFrame (S12).

Some strategies are not cleanly vectorizable in Polars expressions because their
masking primitive is backend- or pandas-bound: date parsing/`strftime`
(date_shift), pandas numeric->string bucket formatting (bucketize), and the
Faker/FPE backends. For these the substrate-decision doc is explicit that the
perf win comes from S5/S7/S9, not the substrate: "the migration is just accept
Polars input + return Polars output; internal logic unchanged."

This port realizes exactly that: it extracts the single target column to pandas,
runs the EXISTING pandas handler (so the masked column is identical to a direct
pandas-adapter run, parity by construction), and writes the result back into the
polars frame. The keyed/format primitive is shared, not reimplemented per
substrate, which is what keeps the parity gate byte-exact.
"""

from __future__ import annotations

import polars as pl

from decoy_engine.execution._adapter import StrategyContext, StrategyHandler
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PandasStrategyPort:
    """Wrap a pandas `StrategyHandler` so it runs column-wise on a pl.DataFrame."""

    def __init__(self, pandas_handler: StrategyHandler) -> None:
        self.name = pandas_handler.name
        self._pandas = pandas_handler

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        pandas_frame = frame.select(column).to_pandas()
        pandas_frame, warnings = self._pandas.run(pandas_frame, column, plan, ctx)
        # Convert the masked column back through Arrow (pl.from_pandas), the same
        # path the pandas adapter uses for its outputs, so a mixed object column
        # (e.g. bucketize's NaN-fallback rows) maps NaN -> null identically rather
        # than choking pl.Series on a mixed Python list.
        masked_frame = pl.from_pandas(pandas_frame)
        return frame.with_columns(masked_frame.get_column(column)), warnings
