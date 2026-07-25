"""profile_source: orchestrate profile generation from a pipeline config.

Reads `config["sources"]`, builds each table's `ProfileSource` reader
(`profile._readers.build_profile_source`), and -- under the default
`residency="bounded"` -- profiles it from cheap `row_count()` metadata
plus a `<= sample_rows` `sample_frame()` instead of a full-frame
materialization, then walks the bounded frame via `walk_dataframe` and
composes a Profile. This is the F1/F2 fix (consultant-2026-07-09):
profiling no longer full-reads a large source before the route decision
runs, so the admission/reject sequence in `run_pipeline` becomes
genuinely pre-expensive-read.

The caller (CLI or platform runner) hands in a config dict that has
already been validated through `PipelineConfig.model_validate(...).model_dump()`.
profile_source does NOT re-validate; the choke-point pattern means
validation happens once, upstream.

PK / FK metadata is derived from `config["relationships"]`:
- A column listed in any relationship's `parent.columns` is `declared_pk`.
- A column listed in any relationship's `children[].columns` has
  `is_fk=True` and `fk_target = (parent_table, parent_column)` matched
  positionally for composite FKs.

S3 (Determinism Layer) replaces the RNG seeding pattern; for now,
`seed=None` uses a non-deterministic Random instance and `seed=<int>`
uses `random.Random(seed)`.
"""

from __future__ import annotations

import random
import warnings
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from decoy_engine.profile._readers import (
    BOUNDED_FALLBACK_ROWS,
    ProfileSource,
    build_profile_source,
)
from decoy_engine.profile._types import Profile, Relationship, TableProfile
from decoy_engine.profile._walk import walk_dataframe


def profile_source(
    config: dict[str, Any],
    *,
    sample_rows: int | None = 10_000,
    seed: int | None = None,
    residency: str = "bounded",
) -> Profile:
    """Profile every source declared in `config["sources"]`.

    Args:
        config: a validated pipeline-config dict (must be the output of
            `PipelineConfig.model_validate(...).model_dump()`; profile_source
            does not re-validate).
        sample_rows: cap on cardinality work. None means full scan; default
            10k caps cardinality work on large tables. Under
            residency="bounded", None degrades to a bounded scan with a loud
            warning (a genuine whole-column scan requires residency="full").
        seed: passed through to the RNG that drives reservoir sampling.
            None means non-deterministic sampling (test mode + interactive
            use); explicit int means cross-run reproducibility.
        residency: SC7a source-read mode.
            - "bounded" (default): read cheap `row_count()` metadata (exact for
              Parquet/fixed_width, an estimate for CSV) plus a `<= sample_rows`
              `sample_frame()`, never materializing the whole source. This is
              what makes admission genuinely pre-expensive-read: profiling no
              longer full-reads a large table before the route decision.
            - "full": the historical whole-source read (`to_frame()` per
              source), for callers that pass `sample_rows=None` and genuinely
              need whole-column distincts on a source small enough to hold.

    Returns:
        A frozen `Profile` covering every table declared in
        `config["sources"]`. The `tables` tuple order mirrors
        `config["sources"]` iteration order (Python 3.7+ dict order).
    """
    if residency not in ("bounded", "full"):
        raise ValueError(
            f"profile_source: residency must be 'bounded' or 'full', got {residency!r}."
        )

    from decoy_engine import __version__ as engine_version

    # Q15 fix (Option B, defensive fallback): if the caller did not pass
    # seed=, fall back to the config's global_settings.seed before
    # surrendering to OS entropy. Some platform callers set the seed in
    # cfg["global_settings"]["seed"] and forget the kwarg; without this
    # fallback the profile RNG was non-deterministic for any table
    # exceeding sample_rows, silently breaking the "same seed -> same
    # output" invariant. The fix at the call site (Option A) is also
    # applied in platform v2_runner.py; both belt + suspenders.
    if seed is None:
        config_seed = (config.get("global_settings") or {}).get("seed")
        # F5 (2026-06-26): exclude bool. isinstance(True, int) is True, so
        # without this guard `seed: true` would seed random.Random(True),
        # coercing `seed: true` to be identical to `seed: 1`.
        if isinstance(config_seed, int) and not isinstance(config_seed, bool):
            seed = config_seed

    # QA-7 F3 (2026-06-01): warn loud when seed is still None after
    # the Q15 fallback. The reservoir sampling becomes
    # non-deterministic, which transitively makes plan compilation
    # non-deterministic (sampled distinct counts drive cardinality
    # mode decisions, which drive masking output). The v2_runner
    # path always passes an explicit seed; this warning catches
    # any future regression at the call site.
    if seed is None:
        warnings.warn(
            "profile_source called without a seed: reservoir sampling "
            "will be non-deterministic. Pass seed= explicitly OR set "
            "global_settings.seed in config for reproducible profiles.",
            stacklevel=2,
        )

    rng = random.Random(seed) if seed is not None else random.Random()

    relationships_config = config.get("relationships", []) or []
    pk_cols_per_table = _derive_pk_cols(relationships_config)
    fk_specs_per_table = _derive_fk_specs(relationships_config)
    relationships = tuple(_build_relationships(relationships_config))

    sources = config.get("sources", {}) or {}
    tables: list[TableProfile] = []
    for table_name, source_descriptor in sources.items():
        reader = build_profile_source(source_descriptor)
        table = _profile_one_source(
            reader,
            table_name=table_name,
            declared_pk_cols=pk_cols_per_table.get(table_name, frozenset()),
            fk_specs=fk_specs_per_table.get(table_name, {}),
            sample_rows=sample_rows,
            residency=residency,
            rng=rng,
        )
        tables.append(table)

    return Profile(
        schema_version=1,
        tables=tuple(tables),
        relationships=relationships,
        # QA-7 F10 (2026-06-01): UTC-aware timestamp. Pre-fix the
        # naive local-time datetime made cross-machine timestamp
        # comparisons meaningless (e.g. UTC worker vs Eastern dev box).
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version=engine_version,
        profile_seed=seed,
    )


# ---------------------------------------------------------------------
# Per-source bounded profiling
# ---------------------------------------------------------------------


def _profile_one_source(
    reader: ProfileSource,
    *,
    table_name: str,
    declared_pk_cols: frozenset[str],
    fk_specs: dict[str, tuple[str, str]],
    sample_rows: int | None,
    residency: str,
    rng: random.Random,
) -> TableProfile:
    """Profile one source into a TableProfile via its `ProfileSource` reader.

    residency="full" reads the whole source (`to_frame()`) and profiles it as
    the historical code did -- exact whole-column distincts, `row_count_exact`
    True. residency="bounded" reads only cheap `row_count()` metadata plus a
    `<= sample_rows` `sample_frame()`: `TableProfile.row_count` is the true
    total (exact for Parquet/fixed_width, estimated for CSV), while
    null/distinct come from the bounded sample and the columns are flagged
    `sampled`.
    """
    if residency == "full":
        df = reader.to_frame()
        # A full read yields an exact count regardless of source type.
        return walk_dataframe(
            df,
            table_name=table_name,
            declared_pk_cols=declared_pk_cols,
            fk_specs=fk_specs,
            sample_rows=sample_rows,
            rng=rng,
        )

    if sample_rows is None:
        # GATE-F #5: a whole-column scan on a source too big to hold is exactly
        # what bounded profiling removes; degrade to a bounded scan loudly
        # rather than OOM. residency="full" remains the honest whole-scan path.
        warnings.warn(
            "profile_source(sample_rows=None, residency='bounded'): a full "
            "column scan would materialize the whole source, which bounded "
            f"profiling avoids. Degrading to a bounded {BOUNDED_FALLBACK_ROWS}-row "
            "scan. Pass residency='full' for a genuine whole-column scan on a "
            "source small enough to hold.",
            stacklevel=2,
        )
        effective_sample_rows = BOUNDED_FALLBACK_ROWS
    else:
        effective_sample_rows = sample_rows

    row_count = reader.row_count()
    sample_df = reader.sample_frame(effective_sample_rows)
    # Clamp the reported total to at least the rows actually sampled so a
    # coarse CSV estimate can never fall below the sample and break the
    # ColumnProfile `distinct_count <= row_count` invariant. Exact counts
    # (Parquet/fixed_width) already dominate the sample, so this is a no-op
    # for them.
    effective_total = max(row_count.value, len(sample_df))
    table = walk_dataframe(
        sample_df,
        table_name=table_name,
        declared_pk_cols=declared_pk_cols,
        fk_specs=fk_specs,
        sample_rows=effective_sample_rows,
        rng=rng,
        total_row_count=effective_total,
    )
    return replace(table, row_count_exact=row_count.exact)


# ---------------------------------------------------------------------
# Relationship metadata derivation
# ---------------------------------------------------------------------


def _derive_pk_cols(
    relationships_config: list[dict[str, Any]],
) -> dict[str, frozenset[str]]:
    """Build {table_name: frozenset(pk_column_names)} from relationships.

    Every column listed in any relationship's `parent.columns` is
    treated as `declared_pk` on its table. For composite PKs, every
    member column carries the flag.
    """
    pk_cols: dict[str, set[str]] = {}
    for rel in relationships_config:
        if not isinstance(rel, dict):
            continue
        parent = rel.get("parent", {})
        if not isinstance(parent, dict):
            continue
        parent_table = parent.get("table")
        parent_columns = parent.get("columns", [])
        if not isinstance(parent_table, str) or not isinstance(parent_columns, list):
            continue
        pk_cols.setdefault(parent_table, set()).update(parent_columns)
    return {t: frozenset(cols) for t, cols in pk_cols.items()}


def _derive_fk_specs(
    relationships_config: list[dict[str, Any]],
) -> dict[str, dict[str, tuple[str, str]]]:
    """Build {child_table: {child_column: (parent_table, parent_column)}}.

    For composite FKs, member columns map positionally: child_columns[i]
    -> (parent_table, parent_columns[i]).
    """
    fk_specs: dict[str, dict[str, tuple[str, str]]] = {}
    for rel in relationships_config:
        if not isinstance(rel, dict):
            continue
        parent = rel.get("parent", {})
        children = rel.get("children", [])
        if not isinstance(parent, dict) or not isinstance(children, list):
            continue
        parent_table = parent.get("table")
        parent_columns = parent.get("columns", [])
        if not isinstance(parent_table, str) or not isinstance(parent_columns, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            child_table = child.get("table")
            child_columns = child.get("columns", [])
            if not isinstance(child_table, str) or not isinstance(child_columns, list):
                continue
            # Positional mapping; lengths should match (S2's composite_columns_length_match).
            if len(child_columns) != len(parent_columns):
                # Profile-layer Relationship.__post_init__ will catch this when we
                # build the Relationship tuple; here we silently skip to avoid a
                # cascade of confusing errors.
                continue
            table_fk_specs = fk_specs.setdefault(child_table, {})
            for child_col, parent_col in zip(child_columns, parent_columns, strict=True):
                if isinstance(child_col, str) and isinstance(parent_col, str):
                    table_fk_specs[child_col] = (parent_table, parent_col)
    return fk_specs


def _build_relationships(
    relationships_config: list[dict[str, Any]],
) -> list[Relationship]:
    """Convert config relationships into Profile-layer Relationship tuples.

    Each config relationship may have multiple children; each child
    becomes one Relationship instance in the Profile. The Relationship
    dataclass `__post_init__` enforces composite_columns_length_match.
    """
    out: list[Relationship] = []
    for rel in relationships_config:
        if not isinstance(rel, dict):
            continue
        parent = rel.get("parent", {})
        children = rel.get("children", [])
        if not isinstance(parent, dict) or not isinstance(children, list):
            continue
        parent_table = parent.get("table")
        parent_columns = parent.get("columns", [])
        namespace = rel.get("namespace")
        if not isinstance(parent_table, str) or not isinstance(parent_columns, list):
            continue
        for child in children:
            if not isinstance(child, dict):
                continue
            child_table = child.get("table")
            child_columns = child.get("columns", [])
            if not isinstance(child_table, str) or not isinstance(child_columns, list):
                continue
            out.append(
                Relationship(
                    parent_table=parent_table,
                    parent_columns=tuple(parent_columns),
                    child_table=child_table,
                    child_columns=tuple(child_columns),
                    namespace=namespace if isinstance(namespace, str) else None,
                )
            )
    return out
