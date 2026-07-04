"""SS1: FK-validity preflight, a fail-closed pre-selection pass over Parquet schemas + keys.

`run_fk_validity` (`validation.post._checks._fk_validity`) cannot run
pre-selection: its `ScanContext` requires a compiled `Plan`, masked
`outputs: dict[str, pa.Table]`, and a `ProviderRegistry` -- none of which
exist before a row has even been selected. This module is the adapter the
implementation guide specifies (section 5.4): it re-implements
`run_fk_validity`'s per-edge classification semantics (null keys neither
match nor orphan; a non-null child key absent from the full parent key set is
a source orphan; FAIL -> hard fail, WARN -> warning, PRESERVE/REMAP -> pass)
directly on schema-only + key-only reads, rather than dragging in
`ScanContext`. `FkPreflightEdgeReport` mirrors `FkValidityReport`'s field
shape for that reason.

Order of checks (stop classifying an EDGE after its first schema-level
failure; keep checking OTHER edges so the report is complete):

0. Parquet-only gate + unknown-table gate (GATE-1 #1).
1. Dangling target column / duplicate column / reserved column name.
2. (Half-declared composite key: handled one layer up, in
   `_edges.relationships_from_config`; see that module's docstring for why
   this preflight cannot see it once a `PlanRelationship` already exists.)
3. Column-type mismatch (incl. float key rejection).
4. Key-level source-orphan pre-scan -- runs only when 0-3 produced zero
   failures across every edge (schemas are sane, so key frames are loadable).
"""

from __future__ import annotations

from collections.abc import Mapping

import polars as pl

from decoy_engine.plan._types import PlanRelationship
from decoy_engine.subset._edges import build_subset_edges
from decoy_engine.subset._keys import RI
from decoy_engine.subset._types import (
    FkPreflightEdgeReport,
    FkPreflightReport,
    PreflightFailure,
    SeedSpec,
    SubsetEdge,
    SubsetSource,
)


def _key_dtypes_compatible(pdt: pl.DataType, cdt: pl.DataType) -> bool:
    if pdt == cdt:
        return True
    # Verified: polars supertypes int widths in joins (Int32 vs Int64 joins fine).
    return pdt.is_integer() and cdt.is_integer()


def run_subset_preflight(
    *,
    sources: Mapping[str, SubsetSource],
    relationships: tuple[PlanRelationship, ...],
    seeds: tuple[SeedSpec, ...] = (),
) -> FkPreflightReport:
    """Run every SS1 fail-closed check. Never raises on a check failure.

    Raises `SubsetConfigError`-family exceptions only on malformed inputs to
    this function itself (there are none today; relationship-construction
    errors are raised by the caller before this function sees them, per
    `_edges.relationships_from_config`). An unknown table named by a
    relationship end is a preflight FAILURE, not a raised error.

    `seeds` is optional so existing edge-only callers are unaffected, but
    `_api._compute` always passes it: a seed table with no relationship edge
    (LOW-1, dennis review) must still be format-checked here, otherwise
    `_keys.load_key_frames` unconditionally `scan_parquet`s it and a
    direct-API caller passing a non-Parquet format for a disconnected seed
    table gets a raw polars error instead of the clean
    `subset_requires_parquet` guidance.
    """
    edges = build_subset_edges(relationships)
    failures: list[PreflightFailure] = []
    warnings: list[str] = []

    tables_referenced: set[str] = set()
    for edge in edges:
        tables_referenced.add(edge.parent_table)
        tables_referenced.add(edge.child_table)
    for seed in seeds:
        tables_referenced.add(seed.table)

    # 5.0: Parquet-only gate + unknown-table gate.
    unknown_table = False
    for table in sorted(tables_referenced):
        if table not in sources:
            unknown_table = True
            failures.append(
                PreflightFailure(
                    code="subset_unknown_table",
                    relationship=f"<{table}>",
                    message=f"table {table!r} is referenced by a relationship but has no entry "
                    "in sources",
                )
            )
            continue
        fmt = sources[table].format
        if fmt != "parquet":
            failures.append(
                PreflightFailure(
                    code="subset_requires_parquet",
                    relationship=f"<{table}>",
                    message=f"table {table!r} source is {fmt!r}; FK-aware subsetting operates "
                    "on Parquet datasets - convert to Parquet for subsetting",
                )
            )

    if unknown_table or any(f.code == "subset_requires_parquet" for f in failures):
        # Schemas cannot be safely read for every edge; stop here.
        return FkPreflightReport(
            passed=False, failures=tuple(failures), warnings=tuple(warnings), edges=()
        )

    schemas: dict[str, pl.Schema] = {
        table: pl.scan_parquet(sources[table].path).collect_schema() for table in tables_referenced
    }

    # 5.1: dangling column / duplicate column / reserved column, per edge end.
    schema_ok_edges: list[SubsetEdge] = []
    for edge in edges:
        edge_ok = True
        for table, columns in (
            (edge.parent_table, edge.parent_columns),
            (edge.child_table, edge.child_columns),
        ):
            schema = schemas[table]
            if len(set(columns)) != len(columns):
                failures.append(
                    PreflightFailure(
                        code="subset_relationship_duplicate_column",
                        relationship=edge.edge_id,
                        message=f"{edge.edge_id}: duplicate column in the declared tuple for "
                        f"table {table!r}: {columns!r}",
                    )
                )
                edge_ok = False
            for col in columns:
                if col == RI:
                    failures.append(
                        PreflightFailure(
                            code="subset_reserved_column",
                            relationship=edge.edge_id,
                            message=f"{edge.edge_id}: column name {RI!r} is reserved by the "
                            "subsetting engine and cannot be a key column",
                        )
                    )
                    edge_ok = False
                elif col not in schema.names():
                    failures.append(
                        PreflightFailure(
                            code="subset_relationship_column_missing",
                            relationship=edge.edge_id,
                            message=f"{edge.edge_id}: column {col!r} not found in table "
                            f"{table!r} (available: {sorted(schema.names())})",
                        )
                    )
                    edge_ok = False
        if edge_ok:
            schema_ok_edges.append(edge)

    # 5.3: column-type mismatch (positional per column pair), incl. float rejection.
    compat_edges: list[SubsetEdge] = []
    for edge in schema_ok_edges:
        pschema, cschema = schemas[edge.parent_table], schemas[edge.child_table]
        edge_ok = True
        for pcol, ccol in zip(edge.parent_columns, edge.child_columns, strict=True):
            pdt, cdt = pschema[pcol], cschema[ccol]
            if pdt.is_float() or cdt.is_float():
                failures.append(
                    PreflightFailure(
                        code="subset_relationship_key_float_unsupported",
                        relationship=edge.edge_id,
                        message=f"{edge.edge_id}: parent {edge.parent_table}.{pcol} ({pdt}) or "
                        f"child {edge.child_table}.{ccol} ({cdt}) is a float dtype; float FK "
                        "keys are unsupported (determinism envelope hard-errors on float; "
                        "float join keys are a correctness hazard)",
                    )
                )
                edge_ok = False
                continue
            if not _key_dtypes_compatible(pdt, cdt):
                failures.append(
                    PreflightFailure(
                        code="subset_relationship_type_mismatch",
                        relationship=edge.edge_id,
                        message=f"{edge.edge_id}: parent {edge.parent_table}.{pcol} is {pdt} but "
                        f"child {edge.child_table}.{ccol} is {cdt}; cast the columns to a common "
                        "type upstream (e.g. '007' vs 7 never joins)",
                    )
                )
                edge_ok = False
        if edge_ok:
            compat_edges.append(edge)

    if failures:
        # 5.0-5.3 found at least one failure: schemas are not uniformly sane,
        # so the key-level orphan scan (5.4) does not run.
        return FkPreflightReport(
            passed=False, failures=tuple(failures), warnings=tuple(warnings), edges=()
        )

    # 5.4: key-level source-orphan pre-scan. The `run_fk_validity` reuse.
    edge_reports: list[FkPreflightEdgeReport] = []
    for edge in compat_edges:
        parent_frame = pl.scan_parquet(sources[edge.parent_table].path).select(
            list(edge.parent_columns)
        )
        child_frame = pl.scan_parquet(sources[edge.child_table].path).select(
            list(edge.child_columns)
        )
        child_keys = child_frame.collect()
        child_row_count = child_keys.height
        non_null = child_keys.drop_nulls()  # any-null component = null key, per P3.
        parent_keys = parent_frame.collect().drop_nulls().unique()
        orphans = non_null.join(
            parent_keys,
            left_on=list(edge.child_columns),
            right_on=list(edge.parent_columns),
            how="anti",
        )
        source_orphan_count = orphans.height
        parent_match_count = non_null.height - source_orphan_count
        invalid_count = source_orphan_count if edge.orphan_policy == "fail" else 0

        edge_reports.append(
            FkPreflightEdgeReport(
                relationship=edge.edge_id,
                namespace=edge.namespace,
                orphan_policy=edge.orphan_policy,
                child_row_count=child_row_count,
                non_null_child_key_count=non_null.height,
                parent_match_count=parent_match_count,
                source_orphan_count=source_orphan_count,
                invalid_count=invalid_count,
            )
        )

        if edge.orphan_policy == "fail" and source_orphan_count > 0:
            failures.append(
                PreflightFailure(
                    code="subset_source_orphans",
                    relationship=edge.edge_id,
                    message=f"{edge.edge_id}: {source_orphan_count} child row(s) reference a "
                    "parent key absent from the source parent table, under orphan_policy=fail",
                )
            )
        elif edge.orphan_policy == "warn" and source_orphan_count > 0:
            warnings.append(
                f"{edge.edge_id}: {source_orphan_count} source-orphan child row(s) under "
                "orphan_policy=warn"
            )

    edge_reports.sort(key=lambda r: r.relationship)
    return FkPreflightReport(
        passed=not failures,
        failures=tuple(failures),
        warnings=tuple(warnings),
        edges=tuple(edge_reports),
    )
