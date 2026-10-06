"""The chunked native route's `when:` row mask, taken from the oracle's own predicate function.

There is no second predicate evaluator here. For each chunk the predicate's referenced
columns are converted with the oracle's `to_pandas_fk_safe` (the same protected-column set the
adapter uses) and handed to the oracle's `_eval_predicate` (numexpr, empty scopes, the same
error codes), so the native mask equals the chunked oracle leg's selection for any column
type by construction. The conversion runs inside the oracle leg's carry diagnosis, so a value
pandas cannot represent raises the same coded error with the same attribution.

Selection rule: a null in a nullable boolean mask is NOT selected, exactly as
`df.loc[mask]` treats it on the oracle leg.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution._when_gate import _eval_predicate
from decoy_engine.expressions._when_parser import parse_when, when_column_refs


@dataclass(frozen=True)
class WhenSpec:
    """One `when:` column: the compiled predicate the oracle evaluates, and what it reads."""

    column: str
    strategy: str
    expression: str
    refs: tuple[str, ...]


def when_specs(col_seed_by_name: Mapping[str, Any]) -> dict[str, WhenSpec]:
    """The `when:` columns of the compiled table plan, keyed by column.

    The expression is the compiled `ColumnSeed.when`, the exact string the oracle's gate
    evaluates. Admission already proved it parses; a failure here is a wiring bug.
    """
    return {
        name: WhenSpec(name, seed.strategy, seed.when, when_column_refs(parse_when(seed.when)))
        for name, seed in col_seed_by_name.items()
        if seed.when is not None
    }


def when_mask(spec: WhenSpec, raw_chunk: pa.Table, protected: Collection[str]) -> pa.Array:
    """The rows of `raw_chunk` the predicate selects, as a non-null boolean array.

    `raw_chunk` is the chunk as the source produced it, before null-typed columns are cast:
    the oracle converts that table. A referenced column the chunk lacks is left out, and the
    oracle's own eval error for an undefined name is raised.
    """
    present = [r for r in spec.refs if r in raw_chunk.column_names]
    frame = to_pandas_fk_safe(raw_chunk.select(present), set(protected) & set(present))
    mask = _eval_predicate(frame, spec.expression, spec.strategy, column=spec.column)
    return pa.array(mask.to_numpy(dtype=bool, na_value=False), type=pa.bool_())


@dataclass(frozen=True)
class WhenMasks:
    """The per-chunk masks of one table's `when:` columns, planned once for the run."""

    specs: Mapping[str, WhenSpec]
    protected: frozenset[str]
    carry: Any
    table: str

    def for_chunk(self, raw_chunk: pa.Table, chunk_index: int) -> dict[str, pa.Array]:
        """Every `when:` column's mask for one chunk, inside the oracle leg's carry diagnosis."""
        try:
            return {n: when_mask(spec, raw_chunk, self.protected) for n, spec in self.specs.items()}
        except Exception as exc:
            if self.carry is not None:
                self.carry.diagnose_adapter(
                    exc,
                    raw_chunk,
                    table=self.table,
                    chunk_index=chunk_index,
                    fk_safe=lambda: self.protected,
                )
            raise


def plan_when_masks(col_seed_by_name: Mapping[str, Any], state: Any, *, table: str) -> WhenMasks:
    """The run's `when:` columns and the protected-column set the adapter converts with."""
    from decoy_engine.execution._chunked_carry import adapter_fk_safe_columns

    specs = when_specs(col_seed_by_name)
    protected = (
        frozenset(adapter_fk_safe_columns(state.plan, state.registry, state.graph, table))
        if specs
        else frozenset()
    )
    return WhenMasks(specs, protected, state.carry, table)


__all__ = ["WhenMasks", "WhenSpec", "plan_when_masks", "when_mask", "when_specs"]
