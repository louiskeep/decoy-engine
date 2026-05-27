"""Walk a pandas DataFrame and build a TableProfile.

walk_dataframe is the pure-function core of the future profile_source
public API. It takes a DataFrame plus caller-supplied column metadata
(declared PK columns, declared FK targets) and returns a TableProfile.
No I/O, no config parsing, no STORM wiring. The orchestration layer that
loads CSV files and parses pipeline YAML lives in a later slice.

Sampling: when sample_rows is set and the DataFrame has more rows than
sample_rows, the function uses Python stdlib random.Random.sample over
row indices to select a sample without replacement, then computes
distinct_count over the sample. is_candidate_key_sampled is always
False under sampling (H6 invariant; enforced by ColumnProfile).
Full-scan distinct_count uses pandas Series.nunique on dropna'd values.

The caller is responsible for seeding rng. profile_source (later slice)
derives a deterministic seed from source path + size + mtime when the
caller does not pass one explicitly (resolution of H5 in the S1 spec
review).
"""

from __future__ import annotations

import random

import pandas as pd

from decoy_engine.profile._types import ColumnProfile, TableProfile


def walk_dataframe(
    df: pd.DataFrame,
    *,
    table_name: str,
    declared_pk_cols: frozenset[str],
    fk_specs: dict[str, tuple[str, str]],
    sample_rows: int | None,
    rng: random.Random,
) -> TableProfile:
    """Return a TableProfile for the given DataFrame.

    Args:
        df: source data as a pandas DataFrame. Column order is preserved
            in the output TableProfile.
        table_name: name to record in TableProfile.name.
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

    Returns:
        TableProfile with one ColumnProfile per DataFrame column.

    Raises:
        ValueError: if any ColumnProfile invariant fails (see
            ColumnProfile.__post_init__).
    """
    row_count = len(df)
    if sample_rows is not None and row_count > sample_rows:
        will_sample = True
        sample_indices = rng.sample(range(row_count), sample_rows)
        sample_df = df.iloc[sample_indices]
    else:
        will_sample = False
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
) -> ColumnProfile:
    """Build a ColumnProfile for one column.

    null_count comes from the full series (always). distinct_count comes
    from sample_series, which equals series when not sampling.
    """
    null_count = int(series.isna().sum())
    distinct_count_raw = sample_series.dropna().nunique()
    distinct_count = int(distinct_count_raw) if not pd.isna(distinct_count_raw) else None

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
        dtype=str(series.dtype),
        row_count=row_count,
        null_count=null_count,
        distinct_count=distinct_count,
        sampled=sampled,
        is_candidate_key_sampled=is_candidate_key_sampled,
        declared_pk=declared_pk,
        is_fk=is_fk,
        fk_target=fk_target,
        pii_class=None,  # STORM wiring lands in a later slice
    )
