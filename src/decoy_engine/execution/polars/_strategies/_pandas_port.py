"""PandasStrategyPort: run a pandas StrategyHandler against a pl.DataFrame (S12).

Some strategies are not cleanly vectorizable in Polars expressions because their
masking primitive is backend- or pandas-bound: date parsing/`strftime`
(date_shift), pandas numeric->string bucket formatting (bucketize), and the
Faker/FPE backends. For these the substrate-decision doc is explicit that the
perf win comes from S5/S7/S9, not the substrate: "the migration is just accept
Polars input + return Polars output; internal logic unchanged."

This port realizes exactly that: it extracts the target column (plus any
sibling columns the handler declares via `required_sibling_columns`, HC-3a)
to pandas, runs the EXISTING pandas handler (so the masked column is
identical to a direct pandas-adapter run, parity by construction), and
writes only the target column back into the polars frame. The keyed/format
primitive is shared, not reimplemented per substrate, which is what keeps
the parity gate byte-exact.
"""

from __future__ import annotations

import polars as pl

from decoy_engine.execution._adapter import StrategyContext, StrategyHandler
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed


class PandasStrategyPort:
    """Wrap a pandas `StrategyHandler` so it runs column-wise on a pl.DataFrame.

    Codex round-6 P2 FAIL-CLOSED PARITY remediation: the wrapped pandas
    handler's optional `preflight(plan, ctx)` hook (see `_adapter.
    StrategyHandler`'s docstring) is duck-typed via `getattr(handler,
    "preflight", None)` by `execution._when_gate.run_with_when_gate_polars`.
    Pre-fix this port did not expose a `preflight` attribute at all, so that
    `getattr` always returned `None` for every polars-native handler routed
    through this wrapper (e.g. `NestedStrategyHandler`, whose own `preflight`
    forwards to its child's -- see `_strategies/_nested.py`), even though the
    polars when-gate now calls `preflight` unconditionally like the pandas
    one does. This method closes that: it forwards to the wrapped handler's
    `preflight` when the wrapped handler defines one, and is itself absent
    (not defined) when the wrapped handler has no `preflight`, preserving the
    same duck-typed "define it only if you need it" contract one level down.
    """

    def __init__(self, pandas_handler: StrategyHandler) -> None:
        self.name = pandas_handler.name
        self._pandas = pandas_handler
        pandas_preflight = getattr(pandas_handler, "preflight", None)
        if pandas_preflight is not None:
            self.preflight = pandas_preflight  # type: ignore[method-assign]

    def run(
        self,
        frame: pl.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pl.DataFrame, list[QualityWarning]]:
        # HC-3a: an optional, duck-typed hook -- most handlers don't define
        # it, so `getattr` with a None default keeps every other strategy's
        # port byte-identical to the pre-hook single-column select.
        sibling_hook = getattr(self._pandas, "required_sibling_columns", None)
        siblings = sibling_hook(plan) if sibling_hook is not None else []
        select_columns = [column]
        for sibling in siblings:
            if sibling in frame.columns and sibling not in select_columns:
                select_columns.append(sibling)
        pandas_frame = frame.select(select_columns).to_pandas()
        pandas_frame, warnings = self._pandas.run(pandas_frame, column, plan, ctx)
        # Convert the masked column back through Arrow (pl.from_pandas), the same
        # path the pandas adapter uses for its outputs, so a mixed object column
        # (e.g. bucketize's NaN-fallback rows) maps NaN -> null identically rather
        # than choking pl.Series on a mixed Python list.
        masked_frame = pl.from_pandas(pandas_frame)
        return frame.with_columns(masked_frame.get_column(column)), warnings
