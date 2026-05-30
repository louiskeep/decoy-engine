"""TransformOp: discriminated union of the V2 narrow transform surface.

S17-TX-NARROW (Endpoint A, locked 2026-05-30): V2 grows a narrow,
auditable transform surface that runs between source-read and the
strategy loop. Six ops: filter, sort, limit, dedupe, derive,
drop_column. These are the 80/20 of customer pipelines that the V1
graph runner's expansive transform palette covered; the remaining nine
ops (sql_run, sub_pipeline, iterate_*, if_router, flag_gate, join,
select_column, convert_file_type, run_storm) are retired, power-user,
or platform-owned and stay on the V1 graph runner (S22-CL-V1GRAPHRUNNER
removes that runner once its dependencies clear).

Per-op design:

- ``FilterOp``: row-predicate via pandas ``DataFrame.eval`` (the same
  expression engine V1's filter_op used; auditable, sandboxed at the
  pandas/NumPy boundary).
- ``SortOp``: stable sort by one or more columns; ``ascending`` is a
  per-column list (broadcast to a single value if one is provided).
- ``LimitOp``: head() cap; ``n`` must be non-negative.
- ``DedupeOp``: ``drop_duplicates(subset=columns)``; ``columns=None``
  dedupes on all columns.
- ``DeriveOp``: compute a new column via pandas ``DataFrame.eval``; the
  derived column must not already exist (compile-time check).
- ``DropColumnOp``: drop one or more columns by name.

Source patterns drawn from the V1 reference implementations in
``decoy_engine.graph.ops.{filter_op,sort,limit,dedupe,derive,drop_column}``
(intentionally NOT copied; the V2 shape is leaner + the union is the
audit boundary, not a per-op file).

ISO/IEC 25010 §5.2.5 (security via safe boundary): pandas eval / safe_eval
is the security boundary, not Python eval. NIST SP 800-188 §4 +
ISO/IEC 20889 (de-identification): filter + derive are recognized
transformation primitives in the standards' taxonomy.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FilterOp(BaseModel):
    """Drop rows where the predicate is False.

    ``expression`` runs through pandas ``DataFrame.eval`` against the
    table's columns. Reference rows by their column name; arithmetic
    + comparison + logical operators are supported. Example:
    ``age >= 18 and country == 'US'``.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["filter"]
    expression: str = Field(..., min_length=1)


class SortOp(BaseModel):
    """Sort rows by one or more columns.

    ``ascending`` is per-column; if a single bool is provided it
    applies to all ``by`` entries. Default is True for all columns.
    Sort is stable -- equal-key rows retain their input order.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["sort"]
    by: list[str] = Field(..., min_length=1)
    ascending: list[bool] | bool = True


class LimitOp(BaseModel):
    """Cap the row count via ``df.head(n)``; rows after position ``n`` are dropped."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["limit"]
    n: int = Field(..., ge=0)


class DedupeOp(BaseModel):
    """Drop duplicate rows.

    ``columns=None`` -- dedupe on ALL columns (a "fully equal" row).
    ``columns=['a','b']`` -- dedupe on the named subset; the first row
    of each subset-equal group is kept.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["dedupe"]
    columns: list[str] | None = None


class DeriveOp(BaseModel):
    """Compute a new column from an expression.

    ``column`` is the name of the new column; it must not already exist
    on the table at the point this op runs (compile-time check). The
    ``expression`` evaluates through pandas ``DataFrame.eval`` against
    the existing columns. Example: ``revenue / users`` produces a
    per-row ARPU column.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["derive"]
    column: str = Field(..., min_length=1)
    expression: str = Field(..., min_length=1)


class DropColumnOp(BaseModel):
    """Drop one or more columns by name."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["drop_column"]
    columns: list[str] = Field(..., min_length=1)


TransformOp = Annotated[
    FilterOp | SortOp | LimitOp | DedupeOp | DeriveOp | DropColumnOp,
    Field(discriminator="op"),
]
