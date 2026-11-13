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
types. Key ends match by the FK join's own key identity (`decoy_engine.internal.fk_tokens`),
never by an Arrow cast. When every end of a key label has one type, or the ends differ only in
integer width or string encoding (plain, large, dictionary), the values compare by type in DuckDB
(BIGINT or HUGEINT, VARCHAR), which agrees with the tokens. Any other mix (an integer against a
float or a string, a date against a timestamp, and so on) maps each batch's values to the join
token as a VARCHAR before loading, so the gate counts the orphans the join sees. A value the token
functions refuse gives `key_type_mismatch` and no counts, as the join refuses it. A relationship
that names a cloud table is `skipped_cloud` before any read, because the cloud CSV readers
download the whole object.

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
from decoy_engine.profiling._key_loading import (
    JOIN_KEY_KIND,
    cast_batch,
    duck_types,
    table_type,
    value_type,
)

# Small enough that the keys of a large table never sit in memory together.
MEMORY_LIMIT: Final = "512MB"
KEY_BATCH_ROWS: Final = 100_000

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
class KeyBatches:
    """The key columns of one table as Arrow batches labelled `k0..kn`, in declared order.

    `batches` can be called again: each call starts a new read. Every batch's column types are
    the declared schema's types or narrower in the same family; a wider batch is refused before
    it is loaded (`_check_widths`)."""

    schema: pa.Schema
    batches: Callable[[], Iterator[pa.RecordBatch]]
    bounded: bool = True


@dataclass(frozen=True)
class ChildKeyFacts:
    """Counts for one child of `key_facts`: rows, null-key rows and orphans."""

    rows: int
    null_key_rows: int
    orphan_rows: int
    orphan_distinct_keys: int


@dataclass(frozen=True)
class KeyFacts:
    """Counts for one parent key and its children, in the order the children were passed.

    A `key_type_mismatch` carries zero counts and no children."""

    status: Literal["checked", "key_type_mismatch"]
    parent_rows: int
    parent_null_key_rows: int
    parent_duplicate_excess: int
    children: tuple[ChildKeyFacts, ...]
    bounded: bool


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
) -> KeyBatches:
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
                chunksize=KEY_BATCH_ROWS,
                columns=columns,
                field_key=key,
            )
            for chunk in chunks:
                chunk.columns = labels
                yield pa.RecordBatch.from_pandas(chunk, schema=text_schema, preserve_index=False)

        return KeyBatches(text_schema, csv_batches)
    if fmt == "parquet":
        lazy = src.LazySource(Path(descriptor["path"]), field_key=key)
        file_schema = lazy.schema
        if any(c not in file_schema.names for c in columns):
            raise _missing(position, name)
        typed = pa.schema(
            [(lb, file_schema.field(c).type) for lb, c in zip(labels, columns, strict=True)]
        )

        def parquet_batches() -> Iterator[pa.RecordBatch]:
            for batch in lazy.iter_batches(KEY_BATCH_ROWS, columns=list(columns)):
                arrays = [batch.column(batch.schema.get_field_index(c)) for c in columns]
                yield pa.RecordBatch.from_arrays(arrays, schema=typed)

        return KeyBatches(typed, parquet_batches)
    # Fixed width: no bounded iterator exists, so the key columns are read whole.
    frame = src.build_declared_source(name, dict(descriptor)).to_frame()
    if any(c not in frame.columns for c in columns):
        raise _missing(position, name)
    table = pa.Table.from_pandas(frame[list(columns)], preserve_index=False)
    del frame
    table = table.rename_columns(labels)
    return KeyBatches(
        table.schema, lambda: iter(table.to_batches(max_chunksize=KEY_BATCH_ROWS)), bounded=False
    )


# ---------------------------------------------------------------------------
# DuckDB counts
# ---------------------------------------------------------------------------


def _before_fetch(conn: Any) -> None:
    """Runs after the orphan query executes and before its result is read. A test seam: it
    lets a test see the spill files while the loaded tables still exist."""


def _span(t: pa.DataType) -> tuple[int, int]:
    bits = t.bit_width
    if pa.types.is_unsigned_integer(t):
        return 0, 2**bits - 1
    return -(2 ** (bits - 1)), 2 ** (bits - 1) - 1


_SPANS: Final = {"BIGINT": _span(pa.int64()), "HUGEINT": (-(2**127), 2**127 - 1)}


def _check_widths(
    batch: pa.RecordBatch, keys: KeyBatches, duck: Sequence[str | pa.DataType | None], role: str
) -> None:
    """Refuse a batch whose integer key column does not fit the type its table was created with.
    DuckDB would raise its own overflow error, whose text can carry the value."""
    columns = zip(_labels(len(duck)), keys.schema.types, duck, batch.schema.types, strict=False)
    for label, declared, kind, actual in columns:
        if kind is None:
            span = _span(declared) if pa.types.is_integer(declared) else None
        else:
            span = _SPANS.get(kind)
        if span is None:
            continue
        actual = value_type(actual)
        if pa.types.is_null(actual):
            continue
        if (
            pa.types.is_integer(actual)
            and span[0] <= _span(actual)[0]
            and _span(actual)[1] <= span[1]
        ):
            continue
        raise PipelineConfigError(f"key {label} of the {role} is wider than its declared type")


def _load(
    conn: Any, table: str, keys: KeyBatches, duck: Sequence[str | pa.DataType | None]
) -> bool:
    """Create `table` from the empty schema, then append every batch. False if a value has no token."""
    schema = pa.schema(
        [
            (
                f.name,
                pa.string()
                if isinstance(kind, str) and kind == JOIN_KEY_KIND
                else table_type(kind)
                if isinstance(kind, pa.DataType)
                else value_type(f.type),
            )
            for f, kind in zip(keys.schema, duck, strict=True)
        ]
    )
    conn.register("incoming", pa.Table.from_batches([], schema=schema))
    picks = ", ".join(
        f"{label}::{kind} AS {label}" if isinstance(kind, str) and kind != JOIN_KEY_KIND else label
        for label, kind in zip(_labels(len(duck)), duck, strict=True)
    )
    conn.execute(f"CREATE TABLE {table} AS SELECT {picks} FROM incoming")
    for batch in keys.batches():
        _check_widths(batch, keys, duck, table)
        loaded = cast_batch(batch, duck)
        if loaded is None:
            return False
        conn.register("incoming", pa.Table.from_batches([loaded]))
        conn.execute(f"INSERT INTO {table} SELECT * FROM incoming")
    conn.unregister("incoming")
    return True


def _count(conn: Any, sql: str, *, probe: bool = False) -> int:
    cursor = conn.execute(sql)
    if probe:
        _before_fetch(conn)
    return int(cursor.fetchone()[0])


def _complete(alias: str, width: int) -> str:
    return " AND ".join(f"{alias}.{label} IS NOT NULL" for label in _labels(width))


def key_facts(
    parent: KeyBatches,
    children: Sequence[KeyBatches] = (),
    *,
    scratch: Path,
    memory_limit: str,
) -> KeyFacts:
    """Counts for `parent` and each of `children`, computed in a DuckDB connection that spills
    under `scratch` (caller-owned) and is closed on every exit. Exceptions propagate."""
    return _key_facts(
        parent, children, scratch, memory_limit, parent.bounded and all(k.bounded for k in children)
    )


def _key_facts(
    parent: KeyBatches,
    children: Sequence[KeyBatches],
    scratch: Path,
    memory_limit: str,
    bounded: bool,
) -> KeyFacts:
    # Imported here because decoy_engine.execution imports the profiling package.
    from decoy_engine.execution.out_of_core._duckdb import connect_duckdb

    width = len(parent.schema)
    labels = _labels(width)
    cols = ", ".join(labels)
    on = " AND ".join(f"p.{label} = c.{label}" for label in labels)
    duck = duck_types([parent.schema, *(k.schema for k in children)])
    conn = connect_duckdb(temp_dir=scratch, memory_limit=memory_limit)
    mismatch = KeyFacts("key_type_mismatch", 0, 0, 0, (), bounded)
    try:
        if not _load(conn, "parent", parent, duck):
            return mismatch
        complete = _complete("p", width)
        parent_rows = _count(conn, "SELECT count(*) FROM parent")
        complete_rows = _count(conn, f"SELECT count(*) FROM parent p WHERE {complete}")
        distinct = _count(
            conn, f"SELECT count(*) FROM (SELECT DISTINCT {cols} FROM parent p WHERE {complete})"
        )
        counted = []
        for keys in children:
            if not _load(conn, "child", keys, duck):
                return mismatch
            counted.append(_child_facts(conn, width, on))
            conn.execute("DROP TABLE child")
    finally:
        conn.close()
    return KeyFacts(
        "checked",
        parent_rows,
        parent_rows - complete_rows,
        complete_rows - distinct,
        tuple(counted),
        bounded,
    )


def _check(
    sources: Mapping[str, Mapping[str, Any]],
    rel: DeclaredRelationship,
    duck_dir: Path,
    memory_limit: str,
) -> RelationshipFacts:
    config, parent = rel.config, rel.config.parent
    parent_keys = _open_keys(rel.position, parent.table, sources[parent.table], parent.columns)
    child_keys = [
        _open_keys(rel.position, c.table, sources[c.table], c.columns) for c in config.children
    ]
    facts = key_facts(parent_keys, child_keys, scratch=duck_dir, memory_limit=memory_limit)
    if facts.status == "key_type_mismatch":
        return _unchecked(
            config,
            "key_type_mismatch",
            facts.bounded,
            reasons.warning.SCAN_KEY_TYPE_MISMATCH.spec.ref,
        )
    return RelationshipFacts(
        parent=parent.table,
        parent_columns=tuple(parent.columns),
        children=tuple(
            ChildFacts(
                c.table,
                tuple(c.columns),
                f.rows,
                f.null_key_rows,
                f.orphan_rows,
                f.orphan_distinct_keys,
            )
            for c, f in zip(config.children, facts.children, strict=True)
        ),
        status="checked",
        parent_rows=facts.parent_rows,
        parent_null_key_rows=facts.parent_null_key_rows,
        parent_duplicate_excess=facts.parent_duplicate_excess,
        exact=True,
        bounded=facts.bounded,
    )


def _child_facts(conn: Any, width: int, on: str) -> ChildKeyFacts:
    complete = _complete("c", width)
    rows = _count(conn, "SELECT count(*) FROM child")
    complete_rows = _count(conn, f"SELECT count(*) FROM child c WHERE {complete}")
    orphans = f"FROM child c WHERE {complete} AND NOT EXISTS (SELECT 1 FROM parent p WHERE {on})"
    orphan_rows = _count(conn, f"SELECT count(*) {orphans}", probe=True)
    keys = ", ".join(f"c.{label}" for label in _labels(width))
    orphan_keys = _count(conn, f"SELECT count(*) FROM (SELECT DISTINCT {keys} {orphans})")
    return ChildKeyFacts(rows, rows - complete_rows, orphan_rows, orphan_keys)


def column_distinct(keys: KeyBatches, *, scratch: Path, memory_limit: str) -> int:
    """Exact count of distinct non-null values in the one column `keys` reads (labelled `k0`).

    A float NaN is not a value here, as in pandas; DuckDB would count it. The type decides, not
    the data. `scratch` is caller-owned; the connection spills under it and is closed on every
    exit."""
    # Imported here because decoy_engine.execution imports the profiling package.
    from decoy_engine.execution.out_of_core._duckdb import connect_duckdb

    if len(keys.schema) != 1:
        raise ValueError("column_distinct: the keys must hold exactly one column")
    skip_nan = " FILTER (WHERE NOT isnan(k0))" if pa.types.is_floating(keys.schema.types[0]) else ""
    conn = connect_duckdb(temp_dir=scratch, memory_limit=memory_limit)
    try:
        conn.register("incoming", pa.RecordBatchReader.from_batches(keys.schema, keys.batches()))
        return _count(conn, f"SELECT count(DISTINCT k0){skip_nan} FROM incoming")
    finally:
        conn.close()
