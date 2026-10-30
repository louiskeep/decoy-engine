"""Chunk-stream adapters over the batch kernel, for routes that mask a table piece by piece.

``cast_stream`` sits between a source's chunks and the masker; ``format_stream`` sits between the
masker and whatever publishes. They pair chunk-for-chunk through a FIFO, so a masker that reads
ahead still gets the right source positions for each output. A chunk whose rows were all
quarantined is held back rather than shown to the masker as an empty frame, and one typed empty
chunk is still produced when nothing else is, so a table with no surviving rows keeps its schema.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa

from decoy_engine.declared_casting._boundary import CastBatch, DeclaredCastRun


class CastPairing:
    """The chunks cast but not yet formatted, oldest first."""

    def __init__(self) -> None:
        self._pending: deque[CastBatch] = deque()

    def push(self, batch: CastBatch) -> None:
        self._pending.append(batch)

    def pop(self) -> CastBatch:
        return self._pending.popleft()

    def __len__(self) -> int:
        return len(self._pending)


def cast_stream(
    run: DeclaredCastRun, table: str, chunks: Iterable[Any], pairing: CastPairing
) -> Iterator[pa.Table]:
    """Typed, compacted chunks with absolute source offsets; registers each for ``format_stream``."""
    offset = 0
    held: pa.Table | None = None
    emitted = False
    for chunk in chunks:
        batch = run.cast_batch(table, chunk, offset=offset)
        offset += chunk.num_rows
        if batch.table.num_rows == 0:
            held = batch.table
            continue
        emitted = True
        pairing.push(batch)
        yield batch.table
    if not emitted and held is not None:
        pairing.push(CastBatch(held, [], offset, None))
        yield held


def format_stream(
    run: DeclaredCastRun, table: str, masked: Iterable[pa.Table], pairing: CastPairing
) -> Iterator[pa.Table]:
    """Rendered chunks; a chunk that loses every row to a format failure is dropped unless it is
    the only one, in which case an empty table of the declared (rendered) schema is produced, so
    the schema never depends on what the masker made of an empty frame."""
    schema_in: pa.Schema | None = None
    emitted = False
    for out in masked:
        batch = pairing.pop()
        schema_in = batch.table.schema
        formatted, _ = run.format_batch(table, out, batch)
        if formatted.num_rows == 0:
            continue
        emitted = True
        yield formatted
    if not emitted and schema_in is not None:
        yield run.rendered_schema(table, schema_in).empty_table()


def stage_typed_sources(
    run: DeclaredCastRun,
    sources: Mapping[str, Any],
    root: Path,
    batch_rows: int,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Cast each declared source once, up front, into a typed source the route then treats as an
    ordinary one.

    A resident table is cast in place. A ``LazySource`` is streamed batch by batch into a typed
    Parquet file under ``root`` (bounded memory), because a route that re-reads its sources in
    several passes must not count or quarantine twice and must see one stable, already-compacted
    row set on every pass. ``max_bytes`` bounds the footprint of ``root``, checked per batch.
    """
    import pyarrow.parquet as pq

    from decoy_engine.execution.out_of_core._budget import check_temp_disk_budget
    from decoy_engine.profile._readers import LazySource

    out = dict(sources)
    for table in run.tables():
        src = sources.get(table)
        if src is None:
            continue
        if not isinstance(src, LazySource):
            out[table] = run.pre_mask({table: src})[table]
            continue
        path = root / f"{table}.typed.parquet"
        writer: Any = None
        offset = 0
        try:
            for batch in src.iter_batches(batch_rows):
                cast = run.cast_batch(table, batch, offset=offset)
                offset += batch.num_rows
                if writer is None:
                    writer = pq.ParquetWriter(path, cast.table.schema)
                writer.write_table(cast.table)
                if max_bytes is not None:
                    check_temp_disk_budget(root, max_bytes=max_bytes)
            if writer is None:  # a zero-row source still needs a typed, readable file
                empty = run.cast_batch(table, src.schema.empty_table(), offset=0).table
                writer = pq.ParquetWriter(path, empty.schema)
                writer.write_table(empty)
        finally:
            if writer is not None:
                writer.close()
        if max_bytes is not None:
            check_temp_disk_budget(root, max_bytes=max_bytes)
        out[table] = LazySource(path)
    return out


def render_batches(
    run: DeclaredCastRun, table: str, batches: Iterable[pa.RecordBatch]
) -> Iterator[pa.RecordBatch]:
    """Rendered record batches for a route whose rows are already compacted and cannot lose more.

    Positions here number the rows the route saw, which only attributes ``fail`` / ``null``
    format issues; a route that renders this way must not admit ``post_mask_row_quarantine``.
    """
    position = 0
    for batch in batches:
        table_in = pa.Table.from_batches([batch])
        probe = CastBatch(
            table_in, list(range(position, position + batch.num_rows)), position, None
        )
        position += batch.num_rows
        rendered, kept = run.format_batch(table, table_in, probe)
        if kept is not None:  # the route's capability set excludes this; fail closed, not lossy
            raise AssertionError("a format-stage quarantine reached a route that cannot drop rows")
        yield from rendered.to_batches() or [pa.RecordBatch.from_pylist([], schema=rendered.schema)]


def maybe_render(
    run: DeclaredCastRun | None, table: str, batches: Iterable[pa.RecordBatch]
) -> Iterator[pa.RecordBatch]:
    """``render_batches`` for a declared table; the batches themselves for any other."""
    if run is None or table not in run.tables():
        return iter(batches)
    return render_batches(run, table, batches)


def maybe_render_empty(run: DeclaredCastRun | None, table: str, out: pa.Table) -> pa.Table:
    """The zero-row output of a declared table, typed as its rendered form."""
    if run is None or table not in run.tables():
        return out
    return run.rendered_schema(table, out.schema).empty_table()


def run_staged(fn: Callable[..., Any], call: dict[str, Any], default_batch_rows: int) -> Any:
    """Run ``fn(plan, sources, **rest)`` over typed copies of its declared sources.

    ``call`` is the caller's own keyword snapshot (``dict(locals())`` at its first line); the
    sources are replaced by their staged form and ``_staged=True`` stops the second call from
    staging again. One private per-run root (under the caller's ``temp_dir`` when given, else
    the system temp directory) holds the copy and is handed on as the route's ``temp_dir``, so
    the route spills beside the copy and every footprint check, the copy's per batch and the
    route's per table, measures both against the one ``temp_disk_budget_bytes``. Two roots would
    each get the whole budget. The root is removed on every exit.
    """
    import os
    import tempfile

    run = call["cast_run"]
    plan, sources = call.pop("plan"), call.pop("sources")
    call.pop("_staged", None)
    parent = call.get("temp_dir")
    if parent is not None:
        Path(parent).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="decoy-ooc-run-", dir=parent) as root:
        os.chmod(root, 0o700)
        batch_rows = call.get("batch_rows") or default_batch_rows
        staged = stage_typed_sources(
            run, sources, Path(root), batch_rows, call.get("temp_disk_budget_bytes")
        )
        call["temp_dir"] = Path(root)
        return fn(plan, staged, _staged=True, **call)
