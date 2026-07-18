"""Walk a pandas DataFrame and build a TableProfile.

walk_dataframe is the pure-function core of the future profile_source
public API. It takes a DataFrame plus caller-supplied column metadata
(declared PK columns, declared FK targets) and returns a TableProfile.
No I/O, no config parsing. The orchestration layer that loads CSV files
and parses pipeline YAML lives in a later slice.

Sampling: when sample_rows is set and the DataFrame has more rows than
sample_rows, the function uses Python stdlib random.Random.sample over
row indices to select a sample without replacement, then computes
distinct_count over the sample. is_candidate_key_sampled is always
False under sampling (H6 invariant; enforced by ColumnProfile).
Full-scan distinct_count uses pandas Series.nunique on dropna'd values.

PII detection: opt-in via run_pii_detection=True. When enabled, the
walker calls STORM (decoy_engine.storm.run_storm) on the full DataFrame
once and maps high-confidence detector matches to the closed PIIClass
enum. Default off: pii_class stays None on every column. Slice 3 of S1.

The caller is responsible for seeding rng. profile_source (later slice)
derives a deterministic seed from source path + size + mtime when the
caller does not pass one explicitly (resolution of H5 in the S1 spec
review).
"""

from __future__ import annotations

import random

import pandas as pd

from decoy_engine.internal.pandas_compat import canonical_dtype_label
from decoy_engine.profile._pii import detect_pii_classes
from decoy_engine.profile._types import ColumnProfile, PIIClass, TableProfile


def walk_dataframe(
    df: pd.DataFrame,
    *,
    table_name: str,
    declared_pk_cols: frozenset[str],
    fk_specs: dict[str, tuple[str, str]],
    sample_rows: int | None,
    rng: random.Random,
    run_pii_detection: bool = False,
    total_row_count: int | None = None,
) -> TableProfile:
    """Return a TableProfile for the given DataFrame.

    Args:
        df: source data as a pandas DataFrame. Column order is preserved
            in the output TableProfile.
        table_name: name to record in TableProfile.name. Also passed to
            STORM as source_label when run_pii_detection=True.
        declared_pk_cols: columns the caller declared as PK in the config.
            Sets ColumnProfile.declared_pk for matching columns.
        fk_specs: mapping {column_name: (parent_table, parent_column)} for
            declared foreign keys. Sets ColumnProfile.is_fk and
            ColumnProfile.fk_target for matching columns.
        sample_rows: cap for cardinality work. None means full scan.
            When set and len(df) > sample_rows, distinct_count is
            computed over a stdlib-random sample and ColumnProfile.sampled
            is True; is_candidate_key_sampled is forced to False.
        rng: stdlib random.Random instance, already seeded by the caller.
            Used only when sampling is triggered.
        run_pii_detection: opt-in STORM PII tagging. Default False
            preserves the slice-2 behavior of every ColumnProfile carrying
            pii_class=None. When True, the walker runs STORM once against
            the full DataFrame and tags columns whose high-confidence
            built-in detector match maps to a PIIClass enum value.
        total_row_count: SC7a bounded-profiling hook. When set, `df` is a
            bounded sample of a larger source and this is the source's true
            total row count (from cheap metadata); row_count on the table and
            every column reports this total, and columns are flagged
            `sampled=True` because their null/distinct come from the sample,
            not the whole column. Default None means `df` IS the whole frame
            (the historical behavior): row_count is `len(df)` and internal
            reservoir sampling applies exactly as before.

    Returns:
        TableProfile with one ColumnProfile per DataFrame column.

    Raises:
        ValueError: if any ColumnProfile invariant fails (see
            ColumnProfile.__post_init__), or if the DataFrame has
            duplicate column names.
    """
    # Reject duplicate column names early (L1 from slice-2 review). pandas
    # auto-suffixes on read_csv but accepts duplicates on hand-constructed
    # DataFrames. Without this guard, df[duplicate_col] would return a
    # DataFrame instead of a Series and downstream .isna().sum() would
    # raise TypeError. Match the slice-1 invariant style (clean ValueError
    # at the boundary, named in the message).
    col_names = [str(c) for c in df.columns]
    if len(set(col_names)) != len(col_names):
        dupes = sorted({n for n in col_names if col_names.count(n) > 1})
        raise ValueError(
            f"walk_dataframe: DataFrame for table {table_name!r} has duplicate "
            f"column names {dupes!r}; column names must be unique."
        )

    # Resolve PII tags before walking columns (slice 3). STORM runs once
    # on the full DataFrame, not per-column. When run_pii_detection is
    # False (default), pii_tags stays empty and every ColumnProfile gets
    # pii_class=None, preserving slice-2 behavior exactly.
    pii_tags: dict[str, PIIClass] = detect_pii_classes(df, table_name) if run_pii_detection else {}

    frame_len = len(df)
    # The reported total: the source's true count when `df` is a bounded
    # sample (total_row_count set), else the frame length (df is the whole
    # table). Two independent sampling triggers:
    #   internal -- df is a full frame larger than sample_rows: reservoir-
    #     sample here, exactly as before.
    #   external -- df is already a bounded sample of a larger source
    #     (total_row_count > frame_len): treat df itself as the sample.
    row_count = total_row_count if total_row_count is not None else frame_len
    internal_sample = sample_rows is not None and frame_len > sample_rows
    external_sample = total_row_count is not None and total_row_count > frame_len
    will_sample = internal_sample or external_sample
    if sample_rows is not None and internal_sample:
        sample_indices = rng.sample(range(frame_len), sample_rows)
        sample_df = df.iloc[sample_indices]
    else:
        sample_df = df

    columns: list[ColumnProfile] = []
    for col_name in df.columns:
        col_name_str = str(col_name)
        column = _walk_column(
            series=df[col_name],
            sample_series=sample_df[col_name],
            name=col_name_str,
            row_count=row_count,
            sampled=will_sample,
            declared_pk_cols=declared_pk_cols,
            fk_specs=fk_specs,
            pii_class=pii_tags.get(col_name_str),
        )
        columns.append(column)

    return TableProfile(name=table_name, row_count=row_count, columns=tuple(columns))


def _walk_column(
    *,
    series: pd.Series,
    sample_series: pd.Series,
    name: str,
    row_count: int,
    sampled: bool,
    declared_pk_cols: frozenset[str],
    fk_specs: dict[str, tuple[str, str]],
    pii_class: PIIClass | None,
) -> ColumnProfile:
    """Build a ColumnProfile for one column.

    null_count comes from `series` (the frame walk_dataframe was given: the
    whole table under residency="full", or already just the bounded sample
    under residency="bounded" -- see GATE-F #5 in
    docs/plans/2026-07-09-consultant-f1-f2-bounded-profiling.md). distinct_count
    comes from sample_series, which equals series when not further sampling
    within that frame. pii_class is resolved by the caller (walk_dataframe)
    from a STORM scan when run_pii_detection=True; otherwise None. avg_length /
    max_length (HC-7) also come from sample_series, for the same reason as
    distinct_count.
    """
    null_count = int(series.isna().sum())
    distinct_count_raw = sample_series.dropna().nunique()
    distinct_count = int(distinct_count_raw) if not pd.isna(distinct_count_raw) else None

    # HC-7: string-length stats, from the same sample population as
    # distinct_count above (so avg_length and distinct_count are mutually
    # consistent -- both measured over sample_series). The free-text
    # advisory's distinctness RATIO, however, divides this sampled
    # distinct_count by the full-frame non-null count (see
    # plan/_checks_freetext_advisory.py), which under active sampling makes
    # the ratio a conservative lower bound -- intentional for a warn-only
    # advisory. Mirrors storm/profiler.py's FieldStats.avg_length computation.
    # None for non-string dtypes; None when the sample has no non-null
    # values (an all-null or empty column).
    avg_length: float | None = None
    max_length: int | None = None
    if pd.api.types.is_string_dtype(series):
        non_null_sample = sample_series.dropna()
        if len(non_null_sample) > 0:
            str_lens = non_null_sample.astype(str).str.len()
            avg_length = float(str_lens.mean())
            max_length = int(str_lens.max())

    declared_pk = name in declared_pk_cols
    is_fk = name in fk_specs
    fk_target = fk_specs.get(name)

    # is_candidate_key_sampled is True only when full-scan AND distinct == row_count
    # AND there is at least one row. H6 invariant; the row_count > 0 guard
    # avoids the vacuous-truth case where an empty table would otherwise be
    # marked candidate-key (0 distinct == 0 rows is not a useful signal for
    # the planner). ColumnProfile.__post_init__ also rejects (sampled=True
    # AND is_candidate_key_sampled=True), so this and-chain is the only path
    # that can return True.
    is_candidate_key_sampled = (
        not sampled and row_count > 0 and distinct_count is not None and distinct_count == row_count
    )

    return ColumnProfile(
        name=name,
        # Audit M5: stable label across pandas majors (pandas-3 'str'
        # normalizes to the historical 'object'); see internal.pandas_compat.
        dtype=canonical_dtype_label(series.dtype),
        row_count=row_count,
        null_count=null_count,
        distinct_count=distinct_count,
        sampled=sampled,
        is_candidate_key_sampled=is_candidate_key_sampled,
        declared_pk=declared_pk,
        is_fk=is_fk,
        fk_target=fk_target,
        pii_class=pii_class,
        avg_length=avg_length,
        max_length=max_length,
    )
