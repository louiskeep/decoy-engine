"""Chunk-stream adapters over the batch kernel, for routes that mask a table piece by piece.

``cast_stream`` sits between a source's chunks and the masker; ``format_stream`` sits between the
masker and whatever publishes. They pair chunk-for-chunk through a FIFO, so a masker that reads
ahead still gets the right source positions for each output. A chunk whose rows were all
quarantined is held back rather than shown to the masker as an empty frame, and one typed empty
chunk is still produced when nothing else is, so a table with no surviving rows keeps its schema.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine._execution_error import ExecutionError
from decoy_engine.declared_casting._boundary import CastBatch, DeclaredCastRun
from decoy_engine.reason_adapter import is_capacity_oserror

if TYPE_CHECKING:
    from decoy_engine.fileaccess import ScratchArena


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


@contextmanager
def _scratch_write() -> Iterator[None]:
    """A full scratch disk is the capacity failure the budget check raises; any other error,
    and anything the source read raises, keeps its own type. The message holds no path."""
    try:
        yield
    except OSError as exc:
        if not is_capacity_oserror(exc):
            raise
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_TEMP_DISK_EXCEEDED,
            message="out-of-core staging could not write its scratch files: the disk is full.",
        ) from exc


def stage_typed_sources(
    names: Sequence[str],
    sources: Mapping[str, Any],
    *,
    batch_fn: Callable[[str, pa.RecordBatch, int], pa.Table],
    empty_fn: Callable[[str, Any], pa.Table],
    resident_fn: Callable[[str, Any], Any],
    root: Path | ScratchArena,
    batch_rows: int,
    max_bytes: int | None = None,
    stem: str = "typed",
    open_fn: Callable[[str, Any], Iterable[pa.RecordBatch]] | None = None,
) -> dict[str, Any]:
    """Stage each named source once, up front, into a source the route then treats as an
    ordinary one. The one staging writer: declared casting and the subset stage both call it.

    A resident table goes through ``resident_fn`` in place. A ``LazySource`` is streamed batch
    by batch, each batch through ``batch_fn(table, batch, absolute_offset)``, into a Parquet
    file under ``root`` (bounded memory), because a route that re-reads its sources in several
    passes must see one stable, already-reduced row set on every pass. ``empty_fn`` gives the
    typed table for a zero-row source. ``open_fn`` replaces ``iter_batches`` when the caller must
    check the handle it reads. ``max_bytes`` bounds the footprint of ``root``, checked per batch.

    ``root`` is a scratch arena (declared staging) or an engine-made directory (the subset stage,
    until it moves onto the arena). Each file is named by a per-run counter and ``stem``, never
    by the table name, so no config value shapes a path (the out-of-core runner's opaque
    ``t0000`` scratch names follow the same rule), and it is read back only through a reader
    issued over the root (``scratch_access_for``), as the out-of-core key stager does. Each file
    is created 0600 before any row lands, whatever the umask: by the arena, or on its own
    descriptor (``open_private``) under a directory.
    """
    import pyarrow.parquet as pq

    from decoy_engine.execution.out_of_core._budget import check_temp_disk_budget
    from decoy_engine.execution.out_of_core._scratch import open_private, scratch_access_for
    from decoy_engine.fileaccess import ScratchArena
    from decoy_engine.fileaccess.adapters.arrow import parquet_writer
    from decoy_engine.profile._readers import LazySource

    arena = root if isinstance(root, ScratchArena) else None
    base = Path(root.local_path()) if isinstance(root, ScratchArena) else root
    out = dict(sources)
    reader: Any = None
    for index, table in enumerate(names):
        src = sources.get(table)
        if src is None:
            continue
        if not isinstance(src, LazySource):
            out[table] = resident_fn(table, src)
            continue
        name = f"s{index:04d}.{stem}.parquet"
        writer: Any = None
        offset = 0
        sink: Any = None
        if arena is None:
            with _scratch_write():
                sink = open_private(base / name)
        batches: Any = None
        try:
            batches = open_fn(table, src) if open_fn is not None else src.iter_batches(batch_rows)
            for batch in batches:
                cast = batch_fn(table, batch, offset)
                offset += batch.num_rows
                with _scratch_write():
                    if writer is None:
                        writer = (
                            pq.ParquetWriter(sink, cast.schema)
                            if arena is None
                            else parquet_writer(arena, name, cast.schema)
                        )
                    writer.write_table(cast)
                if max_bytes is not None:
                    check_temp_disk_budget(base, max_bytes=max_bytes)
            if writer is None:  # a zero-row source still needs a typed, readable file
                empty = empty_fn(table, src)
                with _scratch_write():
                    writer = (
                        pq.ParquetWriter(sink, empty.schema)
                        if arena is None
                        else parquet_writer(arena, name, empty.schema)
                    )
                    writer.write_table(empty)
        finally:
            try:
                # Closed here, not left to garbage collection: the handle behind it is the source file.
                if hasattr(batches, "close"):
                    batches.close()
            finally:
                try:
                    with _scratch_write():
                        if writer is not None:
                            writer.close()
                finally:
                    if sink is not None:
                        with _scratch_write():
                            sink.close()
        if max_bytes is not None:
            check_temp_disk_budget(base, max_bytes=max_bytes)
        if reader is None:
            reader = scratch_access_for(base)
        # The arena's released path is absolute: the reader strips its own root prefix from it.
        path = Path(arena.local_path(name)) if arena is not None else base / name
        out[table] = LazySource(path, access=reader, field_key="engine.scratch")
    return out


def stage_declared_sources(
    run: DeclaredCastRun,
    sources: Mapping[str, Any],
    root: Path | ScratchArena,
    batch_rows: int,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Cast each declared source once, up front, through the shared staging writer."""
    return stage_typed_sources(
        list(run.tables()),
        sources,
        batch_fn=lambda table, batch, offset: run.cast_batch(table, batch, offset=offset).table,
        empty_fn=lambda table, src: run.cast_batch(table, src.schema.empty_table(), offset=0).table,
        resident_fn=lambda table, value: run.pre_mask({table: value})[table],
        root=root,
        batch_rows=batch_rows,
        max_bytes=max_bytes,
    )


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
    staging again. One scratch arena (under the caller's ``temp_dir`` when given, ambient policy
    only, else in the system temp directory) holds the copy and is handed on as the route's
    ``_arena``, so the route spills beside the copy and every footprint check, the copy's per
    batch and the route's per table, measures both against the one ``temp_disk_budget_bytes``.
    Two arenas would each get the whole budget. The route never reclaims a handed arena; this
    opener does, on every exit.
    """
    from decoy_engine.fileaccess import ScratchArena

    run = call["cast_run"]
    plan, sources = call.pop("plan"), call.pop("sources")
    call.pop("_staged", None)
    call.pop("_arena", None)  # the route's own default; a second value would be a duplicate
    with ScratchArena.open(under=call.get("temp_dir")) as arena:
        batch_rows = call.get("batch_rows") or default_batch_rows
        staged = stage_declared_sources(
            run, sources, arena, batch_rows, call.get("temp_disk_budget_bytes")
        )
        call["temp_dir"] = Path(arena.local_path())
        return fn(plan, staged, _staged=True, _arena=arena, **call)
