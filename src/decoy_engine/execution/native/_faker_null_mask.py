"""The positional Faker's null mask, taken from the oracle's own conversion of the source.

Positional Faker reads only which source rows are missing, and pandas decides that from the
dtype it rebuilds for the column (a NumPy float treats NaN as missing, an Arrow-extension float
does not). So the mask is not Arrow validity: the source column is converted with the oracle's
`to_pandas_fk_safe`, the same protected-column set and schema metadata the adapter uses, and
`isna()` of the result is the mask. The result matches the oracle by construction for every
admitted family, with no per-type rule (the pattern of `native/_when_mask.py`).

On the chunked route the conversion reads the RAW chunk. Null-column normalization rebuilds the
table without schema metadata, and a mask taken after it would hide a valid NaN in an
Arrow-extension column. It runs inside the oracle leg's carry diagnosis, so a value pandas
cannot represent raises the same coded error with the same attribution.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution._operator_registry import POSITIONAL_FAKER_SOURCE_TYPES
from decoy_engine.execution.native._operator_params import is_positional_faker_seed

__all__ = [
    "POSITIONAL_FAKER_SOURCE_TYPES",
    "FakerNullMasks",
    "faker_missing_mask",
    "plan_faker_null_masks",
]


def faker_missing_mask(raw: pa.Table, column: str, protected: Collection[str] = ()) -> pa.Array:
    """Boolean array, True where the oracle's frame holds a missing value in `column`."""
    frame = to_pandas_fk_safe(raw.select([column]), set(protected) & {column})
    return pa.array(frame[column].isna().to_numpy(dtype=bool), type=pa.bool_())


@dataclass(frozen=True)
class FakerNullMasks:
    """The per-chunk missing masks of one table's positional Faker columns."""

    columns: tuple[str, ...]
    protected: frozenset[str]
    carry: Any
    table: str

    def for_chunk(self, raw_chunk: pa.Table, chunk_index: int) -> dict[str, pa.Array]:
        """Every positional Faker column's mask for one chunk, inside the carry diagnosis."""
        try:
            return {c: faker_missing_mask(raw_chunk, c, self.protected) for c in self.columns}
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


def plan_faker_null_masks(
    col_seed_by_name: Mapping[str, Any], state: Any, *, table: str
) -> FakerNullMasks:
    """The run's positional Faker columns and the protected set the adapter converts with."""
    from decoy_engine.execution._chunked_carry import adapter_fk_safe_columns

    columns = tuple(n for n, s in col_seed_by_name.items() if is_positional_faker_seed(s))
    protected = (
        frozenset(adapter_fk_safe_columns(state.plan, state.registry, state.graph, table))
        if columns
        else frozenset()
    )
    return FakerNullMasks(columns, protected, state.carry, table)
