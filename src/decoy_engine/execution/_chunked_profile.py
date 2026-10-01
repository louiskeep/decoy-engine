"""Profile construction for `_chunked.run_mask_pipeline_chunked`.

Extracted from `_chunked.py` to keep that module under the orchestration LOC
cap (`tests/sentry/test_module_size.py`); both functions here are
self-contained Profile builders with no state shared with the rest of
`_chunked.py` beyond their return value.
"""

from __future__ import annotations

import dataclasses
import importlib
from collections.abc import Collection
from datetime import datetime
from typing import Any

import pyarrow as pa

# Typed as Any: the compute kernels are registered at runtime, so stubs miss them.
pc: Any = importlib.import_module("pyarrow.compute")


def profile_input(first_chunk: pa.Table, carried: Collection[str] = frozenset()) -> pa.Table:
    """The table the pandas profile walk sees: each carried passthrough column is
    replaced, in place and under the same name, by an all-null column, so its real
    values never reach pandas (rule R3)."""
    for name in carried:
        if name in first_chunk.column_names:
            index = first_chunk.schema.get_field_index(name)
            first_chunk = first_chunk.set_column(index, name, pa.nulls(first_chunk.num_rows))
    return first_chunk


def walk_one_table(table: pa.Table, *, table_name: str) -> Any:
    """The pandas half of the first-chunk profile: one conversion and one walk."""
    import random

    from decoy_engine.profile._walk import walk_dataframe

    return walk_dataframe(
        table.to_pandas(),
        table_name=table_name,
        declared_pk_cols=frozenset(),
        fk_specs={},
        sample_rows=None,
        rng=random.Random(0),
    )


def first_chunk_profile(
    first_chunk: pa.Table,
    *,
    table: str,
    engine_version: str,
    carried: Collection[str] = frozenset(),
) -> Any:
    """Profile the FIRST chunk so compile_plan can build the seed envelope.

    The envelope iterates `profile.tables` (the table must exist there
    for its columns to mask at all), so a fully-empty --no-profile-style
    Profile silently masks nothing. The first chunk gives real dtypes;
    distinct counts and row_count describe only that chunk, which is
    fine -- admitted strategies consume nothing distribution-dependent.
    Faker pools size from the config-declared pool_size (the admission
    rule requires it explicitly), never from profile distinct counts,
    and the pool-capacity pre-flight lands in checks_skipped under
    no_profile=True, which is correct here: with admission restricted
    to deterministic REUSE, pool capacity is a collision-rate knob, not
    a correctness input. Epoch `profiled_at` keeps the 'not a real
    source profile' sentinel from the --no-profile path.

    `carried` names passthrough columns that must never enter pandas
    (`run_mask_chunked`, rule R3): the walk sees them as all-null columns, then
    each one's `ColumnProfile` is replaced by `arrow_column_profile`, computed
    from the real Arrow column, so the profile equals the full pandas one."""
    from decoy_engine.profile import Profile

    table_profile = walk_one_table(profile_input(first_chunk, carried), table_name=table)
    if carried:
        real = {
            name: arrow_column_profile(name, first_chunk.column(name))
            for name in carried
            if name in first_chunk.column_names
        }
        table_profile = dataclasses.replace(
            table_profile,
            columns=tuple(real.get(c.name, c) for c in table_profile.columns),
        )
    return Profile(
        schema_version=1,
        tables=(table_profile,),
        relationships=(),
        profiled_at=datetime(1970, 1, 1, 0, 0, 0),
        decoy_engine_version=engine_version,
        profile_seed=None,
    )


def empty_input_profile(config: dict[str, Any], *, table: str, engine_version: str) -> Any:
    """Placeholder Profile for a chunked source with ZERO chunks (no data at all).

    There is no real chunk to derive dtypes from, so this profiles an EMPTY
    frame built from the config's DECLARED column names only (object dtype
    placeholder). That is enough to `compile_plan(..., no_profile=True)` and
    run the DE-02 fail-closed gate (`require_mask_key`) before the
    empty-input short-circuit returns (Codex-found: the gate was skippable
    by handing a keyed job zero rows/batches). `no_profile=True` already
    treats profile-derived dtype/null-count checks as unreliable and skips
    them (see `check_null_bearing_int_unsupported`), so a placeholder here
    goes through the exact same classification path
    (`keyprovider.plan_has_keyed_strategy`) a real chunk would -- no
    duplicated keyed-strategy logic, and no behavior change to the non-empty
    path.
    """
    import random

    import pandas as pd

    from decoy_engine.profile import Profile
    from decoy_engine.profile._walk import walk_dataframe

    tables = config.get("tables") or []
    table_cfg = next((t for t in tables if isinstance(t, dict) and t.get("name") == table), None)
    columns = [
        col.get("name")
        for col in ((table_cfg or {}).get("columns") or [])
        if isinstance(col, dict) and col.get("name")
    ]
    empty_df = pd.DataFrame({name: pd.Series([], dtype="object") for name in columns})
    table_profile = walk_dataframe(
        empty_df,
        table_name=table,
        declared_pk_cols=frozenset(),
        fk_specs={},
        sample_rows=None,
        rng=random.Random(0),
    )
    return Profile(
        schema_version=1,
        tables=(table_profile,),
        relationships=(),
        profiled_at=datetime(1970, 1, 1, 0, 0, 0),
        decoy_engine_version=engine_version,
        profile_seed=None,
    )


def arrow_column_profile(name: str, column: pa.Array | pa.ChunkedArray) -> Any:
    """The `ColumnProfile` `walk_dataframe` would produce for `column`, computed
    from Arrow without pandas (rule R3, pinned field by field by test 20).

    Missing values are Arrow nulls, NaN in floating columns and the int64 minimum
    in timestamp and duration columns (pandas reads it as NaT). `dtype` follows the
    pandas dtype the column converts to; `distinct_count` counts the non-missing
    values after decoding dictionaries, widening an integer column that holds a
    missing value to float64 as pandas does, and folding `-0.0` into `0.0`; the
    string lengths are set only where pandas would call the column a string
    column. The chunked walk passes no PK, FK or PII inputs, so those fields are
    their defaults."""
    from decoy_engine.internal.pandas_compat import canonical_dtype_label
    from decoy_engine.profile._types import ColumnProfile

    arr = column.combine_chunks() if isinstance(column, pa.ChunkedArray) else column
    t = arr.type
    missing = _missing_mask(arr)
    valid = arr.filter(pc.invert(missing))
    values = valid.dictionary_decode() if pa.types.is_dictionary(t) else valid
    null_count = int(pc.sum(missing.cast(pa.int64())).as_py() or 0)
    if pa.types.is_integer(t) and null_count > 0:
        # pandas widens an integer column with a missing value to float64 first.
        values = values.cast(pa.float64(), safe=False)
    if pa.types.is_floating(values.type):
        values = pc.if_else(pc.equal(values, 0.0), pa.scalar(0.0, values.type), values)
    try:
        distinct: int | None = int(pc.count_distinct(values, mode="only_valid").as_py())
    except pa.ArrowNotImplementedError:
        distinct = None
    avg_length = max_length = None
    if _is_pandas_string(arr, null_count) and len(valid):
        lengths = pc.utf8_length(values).cast(pa.int64())
        avg_length = float(pc.sum(lengths).as_py()) / len(valid)
        max_length = int(pc.max(lengths).as_py())
    rows = len(arr)
    return ColumnProfile(
        name=name,
        dtype=_dtype_label(t, null_count > 0, canonical_dtype_label),
        row_count=rows,
        null_count=null_count,
        distinct_count=distinct,
        sampled=False,
        is_candidate_key_sampled=rows > 0 and distinct == rows,
        declared_pk=False,
        is_fk=False,
        fk_target=None,
        pii_class=None,
        avg_length=avg_length,
        max_length=max_length,
    )


_INT64_MIN = -(2**63)


def _missing_mask(arr: pa.Array) -> pa.Array:
    mask = pc.is_null(arr)
    if pa.types.is_floating(arr.type):
        mask = pc.or_(mask, pc.is_nan(arr))
    if pa.types.is_timestamp(arr.type) or pa.types.is_duration(arr.type):
        mask = pc.or_kleene(mask, pc.equal(arr.cast(pa.int64()), _INT64_MIN))
    return pc.fill_null(mask, True)


def _is_string_type(t: pa.DataType) -> bool:
    return pa.types.is_string(t) or pa.types.is_large_string(t)


def _is_pandas_string(arr: pa.Array, null_count: int) -> bool:
    """pandas' `is_string_dtype` for the converted column: text with no missing value
    (a missing value makes a plain column object-typed with None), or a dictionary
    of text with a non-empty dictionary."""
    t = arr.type
    if pa.types.is_dictionary(t):
        return _is_string_type(t.value_type) and len(arr.dictionary) > 0
    return _is_string_type(t) and null_count == 0


def _dtype_label(t: pa.DataType, has_missing: bool, canonical: Any) -> str:
    if pa.types.is_integer(t):
        return "float64" if has_missing else str(t)
    if pa.types.is_boolean(t):
        return "object" if has_missing else "bool"
    if pa.types.is_floating(t):
        return {16: "float16", 32: "float32", 64: "float64"}[t.bit_width]
    if pa.types.is_timestamp(t):
        # The pandas dtype names a zone through its tzinfo (`pytz.FixedOffset(330)` for
        # "+05:30"), so ask pyarrow for the dtype it converts to instead of formatting `tz`.
        return str(canonical(str(t.to_pandas_dtype())))
    if pa.types.is_duration(t):
        return f"timedelta64[{t.unit}]"
    if pa.types.is_dictionary(t):
        return "category"
    return "object"


__all__ = [
    "arrow_column_profile",
    "empty_input_profile",
    "first_chunk_profile",
    "profile_input",
    "walk_one_table",
]
