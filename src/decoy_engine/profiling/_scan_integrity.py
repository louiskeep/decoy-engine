"""Declared-relationship integrity facts for `scan_sources`.

For each relationship the user declared, count (never show) the parent rows, the parent rows
with a null key part, the parent duplicate excess, and per child the null-key rows and the
orphan rows. A child key that is null is not an orphan (SQL `MATCH SIMPLE`, the dbt
`relationships` test rule); parent duplicates are counted over complete non-null keys only.

Only key columns are read, and only from local files: Parquet through `LazySource` batches, CSV
through `read_csv_source` with a column projection (this module never opens a CSV or calls
pandas to read one), fixed width whole (no bounded iterator exists, so `bounded` is false).
The keys are appended batch by batch to tables in a DuckDB connection with a memory limit and a
spill directory, and the anti-join runs there, so key volume is bounded by disk, not memory.
The spill holds key values, which are row data, so it lives in a directory from the one
protected-directory allocator and is removed on every exit.

CSV key columns are read as text, as `decoy run` reads them; other formats keep their declared
types. Parent and child key types are compared as declared and never cast: a difference gives
`key_type_mismatch` and no counts. A relationship that names a cloud table is `skipped_cloud`
before any read, because the cloud CSV readers download the whole object.

Nothing returned or raised here carries a key value: only counts, table names and positions.
The DuckDB statements are built from fixed column labels (`k0..kn`), never from user text.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Literal

import pyarrow as pa

from decoy_engine import fileaccess, reasons
from decoy_engine.config import PipelineConfigError, RelationshipConfig, parse_relationship_config

# Small enough that the keys of a large table never sit in memory together.
MEMORY_LIMIT: Final = "512MB"
_BATCH_ROWS: Final = 100_000

Status = Literal["checked", "key_type_mismatch", "skipped_cloud"]


@dataclass(frozen=True)
class ChildFacts:
    """Counts for one child of a relationship; `None` when the relationship was not checked."""

    table: str
    columns: tuple[str, ...]
    rows: int | None
    null_key_rows: int | None
    orphan_rows: int | None
    orphan_distinct_keys: int | None


@dataclass(frozen=True)
class RelationshipFacts:
    """Integrity counts for one declared relationship. Counts only: no key value is held."""

    parent: str
    parent_columns: tuple[str, ...]
    children: tuple[ChildFacts, ...]
    status: Status
    parent_rows: int | None
    parent_null_key_rows: int | None
    parent_duplicate_excess: int | None
    exact: bool
    bounded: bool
    warnings: tuple[reasons.ReasonRef, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent": self.parent,
            "parent_columns": list(self.parent_columns),
            "children": [
                {
                    "table": c.table,
                    "columns": list(c.columns),
                    "rows": c.rows,
                    "null_key_rows": c.null_key_rows,
                    "orphan_rows": c.orphan_rows,
                    "orphan_distinct_keys": c.orphan_distinct_keys,
                }
                for c in self.children
            ],
            "status": self.status,
            "parent_rows": self.parent_rows,
            "parent_null_key_rows": self.parent_null_key_rows,
            "parent_duplicate_excess": self.parent_duplicate_excess,
            "exact": self.exact,
            "bounded": self.bounded,
            "warnings": [asdict(w) for w in self.warnings],
        }


@dataclass(frozen=True)
class DeclaredRelationship:
    """A validated relationship: its tables exist in `sources` and its key widths agree."""

    position: int
    config: RelationshipConfig


@dataclass(frozen=True)
class _Keys:
    """The key columns of one table as Arrow batches labelled `k0..kn`."""

    schema: pa.Schema
    batches: Callable[[], Iterator[pa.RecordBatch]]
    bounded: bool = True


def declare(
    sources: Mapping[str, Mapping[str, Any]], relationships: Sequence[Mapping[str, Any]]
) -> tuple[DeclaredRelationship, ...]:
    """Validate each relationship against `RelationshipConfig` and `sources`. Reads no data."""
    declared = []
    for position, raw in enumerate(relationships, 1):
        config = parse_relationship_config(dict(raw), prefix=f"relationship {position} is invalid")
        for end in (config.parent, *config.children):
            if end.table not in sources:
                raise PipelineConfigError(
                    f"relationship {position} names table {end.table!r}, which is not in sources"
                )
            if len(set(end.columns)) != len(end.columns):
                raise PipelineConfigError(
                    f"relationship {position} repeats a key column of table {end.table!r}"
                )
        if any(len(c.columns) != len(config.parent.columns) for c in config.children):
            raise PipelineConfigError(f"relationship {position} has key widths that differ")
        declared.append(DeclaredRelationship(position, config))
    return tuple(declared)


def relationship_facts(
    sources: Mapping[str, Mapping[str, Any]],
    declared: Sequence[DeclaredRelationship],
    *,
    spill_dir: Path | None = None,
    memory_limit: str | None = None,
) -> tuple[RelationshipFacts, ...]:
    """The facts for every relationship in `declared`, in order."""
    results: dict[int, RelationshipFacts] = {}
    local = []
    for rel in declared:
        if _is_cloud(sources, rel):
            results[rel.position] = _unchecked(rel.config, "skipped_cloud", True)
        else:
            local.append(rel)
    if local:
        # Allocated before any key column is read: when the protection cannot be applied the
        # scan stops here, with nothing written.
        with fileaccess.protected_scratch_dir(spill_dir, prefix="decoy-scan-integrity-") as scratch:
            for rel in local:
                results[rel.position] = _check(
                    sources, rel, scratch / f"r{rel.position}", memory_limit or MEMORY_LIMIT
                )
    return tuple(results[rel.position] for rel in declared)


def _is_cloud(sources: Mapping[str, Mapping[str, Any]], rel: DeclaredRelationship) -> bool:
    ends = [rel.config.parent, *rel.config.children]
    return any(sources[e.table]["type"] != "file" for e in ends)


def _unchecked(
    config: RelationshipConfig, status: Status, bounded: bool, *warnings: reasons.ReasonRef
) -> RelationshipFacts:
    """A relationship with no counts: skipped, or refused before any row was compared."""
    return RelationshipFacts(
        parent=config.parent.table,
        parent_columns=tuple(config.parent.columns),
        children=tuple(
            ChildFacts(c.table, tuple(c.columns), None, None, None, None) for c in config.children
        ),
        status=status,
        parent_rows=None,
        parent_null_key_rows=None,
        parent_duplicate_excess=None,
        exact=False,
        bounded=bounded,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Reading key columns
# ---------------------------------------------------------------------------


def _labels(count: int) -> list[str]:
    return [f"k{n}" for n in range(count)]


def _missing(position: int, table: str) -> PipelineConfigError:
    return PipelineConfigError(
        f"relationship {position} names a key column that table {table!r} does not have"
    )


def _open_keys(
    position: int, name: str, descriptor: Mapping[str, Any], columns: Sequence[str]
) -> _Keys:
    # Imported here because decoy_engine.profile imports the profiling package.
    from decoy_engine import profile as src

    key = f"sources.{name}.path"
    labels = _labels(len(columns))
    fmt = descriptor["format"]
    if fmt == "csv":
        dialect = src.resolve_csv_dialect(descriptor)
        path = descriptor["path"]
        header = src.read_csv_source(path, dialect, table=name, dtype=str, nrows=0, field_key=key)
        if any(c not in header.columns for c in columns):
            raise _missing(position, name)
        text_schema = pa.schema([(label, pa.string()) for label in labels])

        def csv_batches() -> Iterator[pa.RecordBatch]:
            chunks = src.read_csv_source(
                path,
                dialect,
                table=name,
                dtype=str,
                chunksize=_BATCH_ROWS,
                columns=columns,
                field_key=key,
            )
            for chunk in chunks:
                chunk.columns = labels
                yield pa.RecordBatch.from_pandas(chunk, schema=text_schema, preserve_index=False)

        return _Keys(text_schema, csv_batches)
    if fmt == "parquet":
        lazy = src.LazySource(Path(descriptor["path"]), field_key=key)
        file_schema = lazy.schema
        if any(c not in file_schema.names for c in columns):
            raise _missing(position, name)
        typed = pa.schema(
            [(lb, file_schema.field(c).type) for lb, c in zip(labels, columns, strict=True)]
        )

        def parquet_batches() -> Iterator[pa.RecordBatch]:
            for batch in lazy.iter_batches(_BATCH_ROWS, columns=list(columns)):
                arrays = [batch.column(batch.schema.get_field_index(c)) for c in columns]
                yield pa.RecordBatch.from_arrays(arrays, schema=typed)

        return _Keys(typed, parquet_batches)
    # Fixed width: no bounded iterator exists, so the key columns are read whole.
    frame = src.build_declared_source(name, dict(descriptor)).to_frame()
    if any(c not in frame.columns for c in columns):
        raise _missing(position, name)
    table = pa.Table.from_pandas(frame[list(columns)], preserve_index=False)
    del frame
    table = table.rename_columns(labels)
    return _Keys(
        table.schema, lambda: iter(table.to_batches(max_chunksize=_BATCH_ROWS)), bounded=False
    )


def _comparable(schema: pa.Schema) -> list[str]:
    """Declared types as text; string and large_string are one type to a reader."""
    out = []
    for field in schema:
        t = field.type
        if pa.types.is_large_string(t):
            t = pa.string()
        elif pa.types.is_large_binary(t):
            t = pa.binary()
        out.append(str(t))
    return out


# ---------------------------------------------------------------------------
# DuckDB counts
# ---------------------------------------------------------------------------


def _before_fetch(conn: Any) -> None:
    """Runs after the orphan query executes and before its result is read. A test seam: it
    lets a test see the spill files while the loaded tables still exist."""


def _load(conn: Any, table: str, keys: _Keys) -> None:
    # The empty first table fixes the column types; each batch is then appended.
    conn.register("incoming", pa.Table.from_batches([], schema=keys.schema))
    conn.execute(f"CREATE TABLE {table} AS SELECT * FROM incoming")
    for batch in keys.batches():
        conn.register("incoming", pa.Table.from_batches([batch]))
        conn.execute(f"INSERT INTO {table} SELECT * FROM incoming")
    conn.unregister("incoming")


def _count(conn: Any, sql: str, *, probe: bool = False) -> int:
    cursor = conn.execute(sql)
    if probe:
        _before_fetch(conn)
    return int(cursor.fetchone()[0])


def _complete(alias: str, width: int) -> str:
    return " AND ".join(f"{alias}.{label} IS NOT NULL" for label in _labels(width))


def _check(
    sources: Mapping[str, Mapping[str, Any]],
    rel: DeclaredRelationship,
    duck_dir: Path,
    memory_limit: str,
) -> RelationshipFacts:
    # Imported here because decoy_engine.execution imports the profiling package.
    from decoy_engine.execution.out_of_core._duckdb import connect_duckdb

    config, parent = rel.config, rel.config.parent
    width = len(parent.columns)
    parent_keys = _open_keys(rel.position, parent.table, sources[parent.table], parent.columns)
    child_keys = [
        _open_keys(rel.position, c.table, sources[c.table], c.columns) for c in config.children
    ]
    bounded = parent_keys.bounded and all(k.bounded for k in child_keys)
    wanted = _comparable(parent_keys.schema)
    if any(_comparable(k.schema) != wanted for k in child_keys):
        return _unchecked(
            config, "key_type_mismatch", bounded, reasons.warning.SCAN_KEY_TYPE_MISMATCH.spec.ref
        )

    labels = _labels(width)
    cols = ", ".join(labels)
    on = " AND ".join(f"p.{label} = c.{label}" for label in labels)
    conn = connect_duckdb(temp_dir=duck_dir, memory_limit=memory_limit)
    try:
        _load(conn, "parent", parent_keys)
        complete = _complete("p", width)
        parent_rows = _count(conn, "SELECT count(*) FROM parent")
        complete_rows = _count(conn, f"SELECT count(*) FROM parent p WHERE {complete}")
        distinct = _count(
            conn, f"SELECT count(*) FROM (SELECT DISTINCT {cols} FROM parent p WHERE {complete})"
        )
        children = []
        for child, keys in zip(config.children, child_keys, strict=True):
            _load(conn, "child", keys)
            children.append(_child_facts(conn, child.table, tuple(child.columns), width, on))
            conn.execute("DROP TABLE child")
    finally:
        conn.close()
    return RelationshipFacts(
        parent=parent.table,
        parent_columns=tuple(parent.columns),
        children=tuple(children),
        status="checked",
        parent_rows=parent_rows,
        parent_null_key_rows=parent_rows - complete_rows,
        parent_duplicate_excess=complete_rows - distinct,
        exact=True,
        bounded=bounded,
    )


def _child_facts(
    conn: Any, table: str, columns: tuple[str, ...], width: int, on: str
) -> ChildFacts:
    complete = _complete("c", width)
    rows = _count(conn, "SELECT count(*) FROM child")
    complete_rows = _count(conn, f"SELECT count(*) FROM child c WHERE {complete}")
    orphans = f"FROM child c WHERE {complete} AND NOT EXISTS (SELECT 1 FROM parent p WHERE {on})"
    orphan_rows = _count(conn, f"SELECT count(*) {orphans}", probe=True)
    keys = ", ".join(f"c.{label}" for label in _labels(width))
    orphan_keys = _count(conn, f"SELECT count(*) FROM (SELECT DISTINCT {keys} {orphans})")
    return ChildFacts(table, columns, rows, rows - complete_rows, orphan_rows, orphan_keys)
