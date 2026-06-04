"""Type definitions for the walks engine.

All frozen dataclasses: snapshots are immutable input; results are
immutable output. Lets the runner cache them safely and lets tests
construct them inline without spinning up a real DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Column:
    name: str
    data_type: str
    nullable: bool
    is_primary_key: bool


@dataclass(frozen=True)
class Table:
    name: str
    schema: str
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class Edge:
    """A foreign-key relationship between two columns.

    `declared` distinguishes edges that came from `information_schema`
    (authoritative) from edges synthesized by the heuristic name-match
    inference (best-guess). The UI surfaces inferred edges with a
    dashed line so users know which is which.
    """

    source_table: str
    source_column: str
    target_table: str
    target_column: str
    declared: bool

    # True if this edge is part of a polymorphic-FK group (e.g. a
    # `comments.entity_id` column that points at multiple tables based
    # on a `comments.entity_type` discriminator. Surfaced separately
    # because a polymorphic FK can't have a real DB-side constraint.
    polymorphic: bool = False


@dataclass(frozen=True)
class SchemaSnapshot:
    """A point-in-time view of a single database schema.

    Produced by the platform's snapshotter (which reads
    `information_schema` for the connected database). Consumed by every
    function in this package: `infer_edges`, `detect_hazards`,
    `build_er_graph`, `compare`. No live DB connection is needed past
    the snapshotter.

    `declared_edges` are the FKs the database itself reports. Heuristic
    inference (`infer_edges`) takes this snapshot and adds best-guess
    edges where columns named `*_id` match a table's PK but no declared
    FK exists.
    """

    db_kind: str
    schema_name: str
    tables: tuple[Table, ...]
    declared_edges: tuple[Edge, ...]
    # Optional context for the platform; engine code ignores it.
    connector_id: int | None = None


@dataclass(frozen=True)
class Hazard:
    """A schema-shape concern flagged by `detect_hazards`.

    `kind` is one of:

      - "HUB"  : table referenced by many other tables (in-degree above threshold)
      - "SR"   : self-reference (a table FKs to itself)
      - "PE"   : parallel edges (multiple FKs from one table to the same target)
      - "PM"   : polymorphic FK (entity_type + entity_id pattern; no enforceable constraint)
      - "ALT"  : alternative parents (XOR: exactly one of N FK columns is set per row)
      - "CIR"  : cycle in the FK graph

    Each detector is a single pure function in `hazards.py`.
    """

    kind: str
    table: str | None
    description: str
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class WalkResult:
    """What a hazard-preset walk returns.

    Persisted as JSONB by the runner so the dashboard can show
    historical results without re-executing.
    """

    snapshot_summary: dict  # {table_count, column_count, declared_edge_count, inferred_edge_count}
    edges: tuple[Edge, ...]  # declared + inferred merged; UI distinguishes via Edge.declared
    hazards: tuple[Hazard, ...]


@dataclass(frozen=True)
class DriftResult:
    """What a drift-preset walk returns: the structural delta between
    two snapshots. Row-count comparison is explicitly out of scope:
    drift is structural only. Phase 3 may add a row-count toggle.
    """

    added_tables: tuple[str, ...]
    removed_tables: tuple[str, ...]
    # Each entry: {"table": str, "column": str, "change_kind": str}
    # where change_kind is one of: "added", "removed", "type_changed",
    # "nullability_changed", "pk_changed".
    changed_columns: tuple[dict, ...]
    # Reserved for future PII-detector integration; empty in v1.
    new_pii: tuple[dict, ...] = field(default_factory=tuple)
