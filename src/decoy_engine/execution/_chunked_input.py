"""Batch input for the auto-chunk lane (Rust engine program slice B6b).

B6a bounds the auto-chunk lane's output; this module bounds its input. A routed table that
is a `LazySource` is read as record batches and re-cut to the exact chunk boundaries a
resident table would be sliced at, so the lane masks the same chunks either way and a
caller that wants a bounded job passes `LazySource(path)` plus a `TransactionalSink`.
Plan: docs/plans/2026-10-02-b6b-lazy-batch-input.md.

The design follows the established readers rather than inventing one:

- Arrow's C stream interface (`RecordBatchReader`, `ArrowArrayStream`) is a pull-based
  stream of record batches under one schema fixed up front. B1's `run_mask_chunked` already
  takes any iterable of tables, so this module feeds it a pulled stream instead of slices.
- `pyarrow.parquet.ParquetFile.iter_batches` decodes a file one batch at a time. The reader
  options are frozen: `pre_buffer=False` (it coalesces reads into a pool that grows with the
  file on a whole-file scan) and an 8 MiB `buffer_size` (buffered column-chunk reads, so a
  large row group costs read buffers and not the whole group).
- Polars' streaming engine fails rather than silently going in memory when a node cannot
  stream. The input streams only when the output streams (`input_modes`), and a run that
  cannot stream says why in its `auto_chunk.input` block.
- DuckDB's Parquet scan treats the row group as the unit of scan work; the block records
  the footer's row-group count and largest row group so an operator can see the layout.

`rechunk` is the only new data-motion primitive. The rest is routing-fact capture, handle
lifecycle and evidence. Routing facts (row count, schema, footer null counts, row-group
layout) are read once per lazy table from one open handle (`capture_source_facts`), and the
same `SourceFacts` object reaches the classifier, the split decision, the lane and the
physical-plan hash; `open_input` compares them with the handle it actually reads from.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Iterator, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.types as pat

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.profile._readers import FooterFacts, LazySource, OpenedLazyBatches

if TYPE_CHECKING:
    from decoy_engine.execution._pipeline_multi_table import MultiTableSplit

__all__ = [
    "INPUT_BUFFER_BYTES",
    "REASON_RESIDENT_SOURCE",
    "InputChunks",
    "SourceFacts",
    "capture_source_facts",
    "complete_source_facts",
    "facts_for",
    "facts_match",
    "input_modes",
    "lazy_stream_candidates",
    "null_count_gap_reason",
    "null_count_gaps",
    "open_input",
    "plan_fact",
    "rechunk",
    "resident_block",
    "source_facts",
]

# 8 MiB of buffered column-chunk reads, the size the plan's probe used; frozen with the
# benchmark bars, not tuned after measuring.
INPUT_BUFFER_BYTES = 8 * 1024 * 1024

REASON_LAZY = "eligible"
REASON_RESIDENT_SOURCE = "resident_source"
MODE_NOT_DISPATCHED = "not_dispatched"


@dataclasses.dataclass(frozen=True, eq=False)
class SourceFacts:
    """What the planner reads about one source: row count, schema, per-column null counts.

    A lazy source's facts come from a Parquet footer and also carry its row-group layout;
    a resident table's null counts are read from the table when a gate asks, so building
    the facts of a table costs nothing. Compare two with `facts_match`."""

    num_rows: int
    schema: pa.Schema
    null_counts: Mapping[str, int | None] | None = None
    row_groups: int | None = None
    max_row_group_rows: int | None = None
    table: pa.Table | None = None

    def null_count(self, column: str) -> int | None:
        """The column's null count, or `None` when a footer has no complete statistic."""
        if self.table is not None:
            return int(self.table.column(column).null_count)
        return (self.null_counts or {}).get(column)


def resident_block(reason: str) -> dict[str, Any]:
    return {"mode": "resident", "reason": reason}


def facts_from_footer(footer: FooterFacts) -> SourceFacts:
    return SourceFacts(
        num_rows=footer.num_rows,
        schema=footer.schema,
        null_counts=MappingProxyType(dict(footer.null_counts)),
        row_groups=footer.row_groups,
        max_row_group_rows=footer.max_row_group_rows,
    )


def source_facts(src: pa.Table | LazySource) -> SourceFacts:
    """The facts of one source. A `LazySource` is read through `footer_facts()`, which opens
    the file once; a table is read in place."""
    if isinstance(src, LazySource):
        return facts_from_footer(src.footer_facts())
    return SourceFacts(num_rows=src.num_rows, schema=src.schema, table=src)


def capture_source_facts(
    caller_sources: Mapping[str, pa.Table | LazySource], table_kinds: Mapping[str, str]
) -> Mapping[str, SourceFacts]:
    """One footer snapshot per mask-kind `LazySource`, in config order. Resident tables and
    generate-kind names are not captured."""
    return {
        name: source_facts(caller_sources[name])
        for name, kind in table_kinds.items()
        if kind == "mask" and isinstance(caller_sources.get(name), LazySource)
    }


def complete_source_facts(
    source_tables: Mapping[str, pa.Table | LazySource] | None,
    provided: Mapping[str, SourceFacts] | None,
) -> Mapping[str, SourceFacts]:
    """`provided` plus a fresh capture for any `LazySource` it does not cover, so a direct
    caller of the planner that passes no facts still reads each footer exactly once."""
    facts = dict(provided or {})
    for name, src in (source_tables or {}).items():
        if isinstance(src, LazySource) and name not in facts:
            facts[name] = source_facts(src)
    return facts


def facts_match(a: SourceFacts, b: SourceFacts) -> bool:
    """Equal row count, schema with metadata, footer null counts and row-group layout."""
    return (
        a.num_rows == b.num_rows
        and a.schema.equals(b.schema, check_metadata=True)
        and dict(a.null_counts or {}) == dict(b.null_counts or {})
        and a.row_groups == b.row_groups
        and a.max_row_group_rows == b.max_row_group_rows
    )


def null_count_gaps(facts: SourceFacts, bucketize_columns: Iterable[str]) -> list[str]:
    """Gated columns with no exact null count: the integer columns and the numeric bucketize
    source columns, which are the only columns the planner reads a null count for."""
    if facts.table is not None:
        return []
    gated: list[str] = []
    for field in facts.schema:
        if pat.is_integer(field.type):
            gated.append(field.name)
    names = set(facts.schema.names)
    for name in bucketize_columns:
        if name in names and name not in gated:
            kind = facts.schema.field(name).type
            if pat.is_integer(kind) or pat.is_floating(kind):
                gated.append(name)
    return [name for name in gated if facts.null_count(name) is None]


def facts_for(
    store: dict[str, SourceFacts] | None, src: pa.Table | LazySource, table: str
) -> SourceFacts:
    """`store[table]` when present; else the facts of `src`, saved into `store` so a lazy
    footer is read once per call chain."""
    facts = (store or {}).get(table)
    if facts is None:
        facts = source_facts(src)
        if store is not None:
            store[table] = facts
    return facts


def null_count_gap_reason(
    facts: SourceFacts, bucketize_columns: Iterable[str], table: str
) -> str | None:
    """The planner's rejection when a gated column of a lazy table has no footer null count."""
    gaps = null_count_gaps(facts, bucketize_columns)
    if not gaps:
        return None
    return (
        f"lazy_source_null_count_unavailable: column(s) {', '.join(gaps)} of table {table!r} "
        "have no footer null count; auto-chunk declines rather than read the column to "
        "count nulls"
    )


def plan_fact(name: str, src: pa.Table | LazySource) -> tuple[object, ...]:
    """One source's route-affecting content for the physical-plan hash: the same facts the
    planner reads, `(name, num_rows, ((column, type, null_count), ...))`, with a
    `"lazy_source"` marker between name and count for a footer-backed source."""
    facts = source_facts(src)
    columns = tuple(
        (field.name, str(field.type), facts.null_count(field.name)) for field in facts.schema
    )
    if isinstance(src, LazySource):
        return (name, "lazy_source", facts.num_rows, columns)
    return (name, facts.num_rows, columns)


def rechunk(
    batches: Iterable[pa.RecordBatch], schema: pa.Schema, chunk_size_rows: int
) -> Iterator[pa.Table]:
    """Re-cut reader batches into tables of exactly `chunk_size_rows` rows (the last may be
    shorter), the boundaries `Table.slice` makes on a resident table.

    Reader batches are at most `chunk_size_rows` rows, so before an emission the pending
    rows are fewer than `2 * chunk_size_rows`. A chunk may combine more than two short
    batches; the bound is on retained rows, not on fragments."""
    pending: list[pa.RecordBatch] = []
    rows = 0
    for batch in batches:
        if batch.num_rows == 0:
            continue
        pending.append(batch)
        rows += batch.num_rows
        while rows >= chunk_size_rows:
            combined = pa.Table.from_batches(pending, schema=schema)
            yield combined.slice(0, chunk_size_rows)
            rest = combined.slice(chunk_size_rows)
            pending = rest.to_batches()
            rows = rest.num_rows
    if rows:
        yield pa.Table.from_batches(pending, schema=schema)


def _slices(source: pa.Table, chunk_size_rows: int) -> Iterator[pa.Table]:
    for start in range(0, source.num_rows, chunk_size_rows):
        yield source.slice(start, chunk_size_rows)


def _changed(table: str, detail: str) -> ExecutionError:
    return ExecutionError(
        code="lazy_source_changed",
        message=f"the source of table {table!r} changed between routing and the read: {detail}.",
    )


def check_opened(opened: OpenedLazyBatches, expected: SourceFacts, *, table: str) -> None:
    """Raise `lazy_source_changed` unless the opened handle's footer facts equal the routing
    snapshot. Reads no second handle: the facts come from the handle being read."""
    if not facts_match(facts_from_footer(opened.facts), expected):
        raise _changed(table, "footer facts differ from the routing snapshot")


def _guarded(
    chunks: Iterator[pa.Table], owner: OpenedLazyBatches, *, table: str
) -> Iterator[pa.Table]:
    """The re-cut stream, with the row-count check at the end and the owner's close on any
    exit (exhaustion, an error, or the consumer closing the generator)."""
    rows = 0
    try:
        for chunk in chunks:
            rows += chunk.num_rows
            yield chunk
        if rows != owner.num_rows:
            raise ExecutionError(
                code="lazy_source_row_count_mismatch",
                message=(
                    f"the source of table {table!r} yielded {rows} rows, "
                    f"but its footer says {owner.num_rows}."
                ),
            )
    finally:
        owner.close()


class InputChunks:
    """One routed table's input: its first chunk, the full chunk stream, the evidence block
    and an idempotent `close()` that releases the source handle."""

    def __init__(
        self,
        first: pa.Table,
        chunks: Iterator[pa.Table],
        block: dict[str, Any],
        owner: OpenedLazyBatches | None,
    ) -> None:
        self.first = first
        self.chunks = chunks
        self._block = block
        self._owner = owner

    def block(self) -> dict[str, Any]:
        return dict(self._block)

    def close(self) -> None:
        close = getattr(self.chunks, "close", None)
        if close is not None:
            close()
        if self._owner is not None:
            self._owner.close()


def _chain(first: pa.Table, rest: Iterator[pa.Table]) -> Iterator[pa.Table]:
    yield first
    yield from rest


def _open_lazy(
    source: LazySource,
    *,
    table: str,
    chunk_size_rows: int,
    expected: SourceFacts | None,
    native_threads: int,
) -> InputChunks:
    opened = source.open_batches(
        chunk_size_rows,
        pre_buffer=False,
        buffer_size=INPUT_BUFFER_BYTES,
        use_threads=native_threads > 1,
    )
    try:
        if expected is not None:
            check_opened(opened, expected, table=table)
        stream = _guarded(
            rechunk(opened.batches, opened.schema, chunk_size_rows), opened, table=table
        )
        first = next(stream, None)
    except BaseException:
        opened.close()
        raise
    block = {
        "mode": "lazy",
        "reason": REASON_LAZY,
        "source_row_groups": opened.row_groups,
        "source_max_row_group_rows": opened.max_row_group_rows,
    }
    if first is None:
        return InputChunks(opened.schema.empty_table(), iter(()), block, opened)
    return InputChunks(first, _chain(first, stream), block, opened)


def open_input(
    source: pa.Table | LazySource,
    *,
    table: str,
    chunk_size_rows: int,
    expected: SourceFacts | None,
    native_threads: int,
) -> InputChunks:
    """Open one routed table's chunk stream.

    A `pa.Table` is sliced exactly as before. A `LazySource` is opened once, checked
    against `expected` (the routing snapshot) before any batch is read, re-cut by
    `rechunk`, and primed with its first chunk; any `BaseException` before the first
    yield closes the handle. After the last chunk the row total must equal the footer's."""
    if isinstance(source, LazySource):
        return _open_lazy(
            source,
            table=table,
            chunk_size_rows=chunk_size_rows,
            expected=expected,
            native_threads=native_threads,
        )
    block = resident_block(REASON_RESIDENT_SOURCE)
    return InputChunks(
        source.slice(0, chunk_size_rows), _slices(source, chunk_size_rows), block, None
    )


def lazy_stream_candidates(
    facts: Mapping[str, SourceFacts],
    *,
    table_kinds: Mapping[str, str],
    route_chunked: bool,
    bearing: frozenset[str],
) -> Mapping[str, SourceFacts]:
    """The lazy tables that may still stream: mask-kind, not transform-bearing and unprepared
    (`bearing`), not empty, in a job that routed chunked or holds two or more mask tables
    (a B7 split is still possible) and has no generate table. Every other `LazySource` is
    resolved before the route executors run."""
    masks = [name for name, kind in table_kinds.items() if kind == "mask"]
    if not all(kind == "mask" for kind in table_kinds.values()):
        return {}
    if not (route_chunked or len(masks) >= 2):
        return {}
    return {
        name: fact
        for name, fact in facts.items()
        if name in masks and name not in bearing and fact.num_rows > 0
    }


def input_modes(
    kept: Iterable[str],
    *,
    out_mode: str,
    out_reason: str,
    routed: str | None,
    split: MultiTableSplit | None,
) -> dict[str, tuple[str, str]]:
    """`(mode, reason)` for each kept-lazy table.

    `lazy` when the output streams and the table is the routed table or a dispatched split
    table; `resident` with the output's reason when it is routed or dispatched but the
    output stays resident; `resident` / `not_dispatched` for a table that no lane takes."""
    dispatched = set(split.dispatched) if split is not None else set()
    if routed is not None:
        dispatched.add(routed)
    out: dict[str, tuple[str, str]] = {}
    for name in kept:
        if name not in dispatched:
            out[name] = ("resident", MODE_NOT_DISPATCHED)
        elif out_mode == "streamed":
            out[name] = ("lazy", REASON_LAZY)
        else:
            out[name] = ("resident", out_reason)
    return out
