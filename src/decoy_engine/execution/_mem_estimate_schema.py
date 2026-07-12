"""Adapters: build a `TableSizeSpec` (`_mem_estimate.py`) from the engine's
real schema representations -- `profile/_types.py` for mask tables,
`config/_tables.py` for generate tables (OOM-avoidance routing redesign,
Sprint B1a). Split out of `_mem_estimate.py` to keep that module under the
600-LOC orchestration cap (CLAUDE.md "Engineering best practices"); the pure
arithmetic (`raw_data_bytes`, `estimate_peak_bytes`, `fits`) lives there and
does not need to know which side a `TableSizeSpec` came from.

Mask jobs read real source data at profiling time, so a variable-width
column CAN be priced from a genuine sample (`sample_average_string_bytes`)
-- generation jobs have no input to sample before they run (§3.2b of the
design doc, corrected per §11), so their variable-width columns are priced
from provider/strategy metadata (`generate_column_width_bytes`) instead, and
marked UNPRICEABLE when no such metadata exists rather than guessed (§3.5).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from decoy_engine.execution._mem_estimate import (
    ColumnSizeSpec,
    TableSizeSpec,
    is_fixed_width_dtype,
)

if TYPE_CHECKING:
    import pyarrow as pa

    from decoy_engine.config._tables import GenerateColumnConfig, TableConfig
    from decoy_engine.profile._types import TableProfile


def sample_average_string_bytes(column: pa.Array | pa.ChunkedArray) -> float:
    """Mean UTF-8-encoded byte length across `column`'s non-null values.

    The genuine "sample" half of §3.2/§11: a mask job DOES have resident
    (or profiled-sample) source data, so its string columns get a real
    measured average rather than a guess -- generation jobs have no such
    data to sample (`generate_column_width_bytes` covers that case
    instead). UTF-8 byte length, not `len()`, matches what ends up on an
    Arrow/Parquet buffer for the common ASCII/latin1 case and is the
    correct proxy either way for non-ASCII text. Returns 0.0 for an
    empty/all-null column -- its cost then comes entirely from the fixed
    per-object overhead `_mem_estimate` adds on top.
    """
    non_null = column.drop_null()
    if len(non_null) == 0:
        return 0.0
    total_bytes = sum(len(v.as_py().encode("utf-8")) for v in non_null)
    return total_bytes / len(non_null)


def table_size_spec_from_profile(
    profile_table: TableProfile,
    *,
    declared_widths: Mapping[str, float] | None = None,
    sample: Mapping[str, pa.Array | pa.ChunkedArray] | None = None,
) -> TableSizeSpec:
    """Build a `TableSizeSpec` for a MASK table from its `TableProfile`.

    Fixed-width columns price directly off `ColumnProfile.dtype`. A
    variable-width column's width comes from `declared_widths` (a caller-
    supplied override) if present, else a genuine sample
    (`sample_average_string_bytes` over `sample[column]`) if the caller
    supplied resident/sampled data for it, else the column is UNPRICEABLE --
    `ColumnProfile` does not yet carry a string-length statistic itself
    (open work for a later profiling sprint), so guessing one here would be
    exactly the silent-invention this redesign forbids.
    """
    declared_widths = declared_widths or {}
    sample = sample or {}
    columns: list[ColumnSizeSpec] = []
    for col in profile_table.columns:
        if is_fixed_width_dtype(col.dtype):
            columns.append(ColumnSizeSpec(name=col.name, dtype=col.dtype))
            continue
        if col.name in declared_widths:
            width = float(declared_widths[col.name])
            columns.append(ColumnSizeSpec(name=col.name, dtype=col.dtype, string_width_bytes=width))
        elif col.name in sample:
            width = sample_average_string_bytes(sample[col.name])
            columns.append(ColumnSizeSpec(name=col.name, dtype=col.dtype, string_width_bytes=width))
        else:
            columns.append(ColumnSizeSpec(name=col.name, dtype=col.dtype, unpriceable=True))
    return TableSizeSpec(
        name=profile_table.name, row_count=profile_table.row_count, columns=tuple(columns)
    )


# Typical/declared output byte-widths for closed-set `faker` generation types
# whose shape does not depend on input data (§3.2b/§11: generation has
# nothing to sample, so provider metadata is the only legitimate source of a
# width -- not a guess, but also deliberately NOT exhaustive: any faker_type
# not listed here is UNPRICEABLE rather than assigned an invented number.
# Widths are conservative (rounded up) typical lengths for the Faker
# provider of the same name; a later sprint can extend this table (or wire
# real per-provider max-length metadata, §11's "hidden B1 work") without
# changing the estimator's shape.
_GENERATE_FAKER_TYPE_WIDTH_BYTES: dict[str, float] = {
    "email": 24.0,
    "name": 16.0,
    "first_name": 8.0,
    "last_name": 9.0,
    "phone_number": 14.0,
    "address": 32.0,
    "city": 12.0,
    "state": 12.0,
    "zipcode": 5.0,
    "country": 16.0,
    "company": 18.0,
    "job": 20.0,
    "uuid4": 36.0,
    "ipv4": 13.0,
    "url": 32.0,
    "date": 10.0,
    "iso8601": 26.0,
    "word": 8.0,
}


def generate_column_width_bytes(gen_col: GenerateColumnConfig) -> float | None:
    """Typical output byte-width for a generation column, or `None`
    (UNPRICEABLE) when no declared/typical width is known.

    Covers `faker` (via `_GENERATE_FAKER_TYPE_WIDTH_BYTES`, keyed on
    `faker_type`) and `categorical` (the widest declared category, an exact
    upper bound rather than a typical-case estimate, since the category set
    is fully known at config time). Every other generate type --
    `formula`/`derived`/`derived_aggregate`/`statistical`/`windowed_date`/
    `grouped_series`/`reference`/`group_key` -- is UNPRICEABLE here: each
    depends on a snapshot file, an expression, or a parent column not
    resolvable without running the pipeline. That is the documented "do not
    guess" default (§3.5), not an omission; a later sprint can extend this
    function as real per-type metadata becomes available.
    """
    extras = gen_col.model_extra or {}
    if gen_col.type == "categorical":
        categories = extras.get("categories") or []
        str_categories = [c for c in categories if isinstance(c, str)]
        if not str_categories:
            return None
        return float(max(len(c.encode("utf-8")) for c in str_categories))
    if gen_col.type == "faker":
        faker_type = extras.get("faker_type")
        if isinstance(faker_type, str):
            return _GENERATE_FAKER_TYPE_WIDTH_BYTES.get(faker_type)
        return None
    return None


# Generate types whose output is a fixed-width numeric dtype rather than a
# string, so they price like a mask table's numeric column instead of going
# through `generate_column_width_bytes` at all.
_GENERATE_NUMERIC_DTYPE: dict[str, str] = {
    "sequence": "int64",
    "group_key": "int64",
}


def table_size_spec_from_generate_table(table: TableConfig) -> TableSizeSpec:
    """Build a `TableSizeSpec` for a GENERATE table from its `TableConfig`.

    `row_count` comes straight off the config (a generate table declares it
    up front, unlike a mask table which needs a profile). Each
    `generate_columns` entry is priced by its declared numeric dtype
    (`_GENERATE_NUMERIC_DTYPE`) or `generate_column_width_bytes`; anything
    that function returns `None` for is UNPRICEABLE.
    """
    if table.row_count is None:
        raise ValueError(
            f"table {table.name!r}: table_size_spec_from_generate_table requires a "
            "generate table with row_count set (see TableConfig._mask_xor_generate)."
        )
    columns: list[ColumnSizeSpec] = []
    for gen_col in table.generate_columns:
        numeric_dtype = _GENERATE_NUMERIC_DTYPE.get(gen_col.type)
        if numeric_dtype is not None:
            columns.append(ColumnSizeSpec(name=gen_col.name, dtype=numeric_dtype))
            continue
        width = generate_column_width_bytes(gen_col)
        if width is None:
            columns.append(ColumnSizeSpec(name=gen_col.name, dtype="object", unpriceable=True))
        else:
            columns.append(
                ColumnSizeSpec(name=gen_col.name, dtype="object", string_width_bytes=width)
            )
    return TableSizeSpec(name=table.name, row_count=table.row_count, columns=tuple(columns))


__all__ = [
    "generate_column_width_bytes",
    "sample_average_string_bytes",
    "table_size_spec_from_generate_table",
    "table_size_spec_from_profile",
]
