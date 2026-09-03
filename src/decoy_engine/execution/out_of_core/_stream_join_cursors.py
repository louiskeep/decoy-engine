"""The order-restore cursor/lifecycle unit for the streamed FK joiner (pure-move
decomposition out of `_stream_join.py`, P4 HIGH-1 module-size decomposition).

This module holds the complete reorder/cursor/lifecycle unit that
`StreamFkJoiner.run_ordered_join` and `_stream_driver.py` consume:

- `_OrderedJoinRows`: the owning, closeable iterator over one edge's
  order-restored join rows, returned by `run_ordered_join` once the unordered
  join has already been drained into a `BoundedExternalSorter` and the DuckDB
  connection closed.
- `JoinRowCursor`: the forward-only cursor the driver uses to re-batch the
  ordered join reader to the payload store's own batch boundaries.
- `_guarded_reorder_iter`/`_release_reorder`: the fail-closed contiguity guard
  and finalizer callback `_OrderedJoinRows` wraps `sorter.iter_ordered()` in.
- `_contiguity_error`/`_row_alignment_error`/`_concat_join_row_batches`: the
  small helpers this unit's error paths and batch re-slicing need.

`_OrderedJoinRows` calls `_guarded_reorder_iter`/`_release_reorder` directly, so
they must live together in one module; this sibling imports nothing back from
`_stream_join.py` (only `BoundedExternalSorter` from `_external_sort.py`, plus
Arrow/weakref/`ExecutionError`), so there is no import cycle. See
`_stream_join.py`'s module docstring for the joiner's full lifecycle.
"""

from __future__ import annotations

import weakref
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.compute as pc

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution.out_of_core._external_sort import BoundedExternalSorter

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator


def _contiguity_error(detail: str) -> ExecutionError:
    return ExecutionError(
        code="out_of_core_fk_reorder_contiguity",
        message=(
            "the order-restored FK join output is not exactly the child's own "
            f"0..N-1 row_nr domain: {detail}. N is taken from the INDEPENDENT "
            "child-stage count (the SpillChildKeys row count), never inferred "
            "from the join output itself, so a lost suffix fails closed instead "
            "of silently self-validating as a shorter dense range. This is an "
            "all-or-nothing failure: no partial result from this iterator is a "
            "valid FK output."
        ),
    )


def _guarded_reorder_iter(
    sorter: BoundedExternalSorter, expected_row_count: int
) -> Generator[pa.RecordBatch, None, None]:
    """The fail-closed 0..N-1 contiguity guard over `sorter.iter_ordered()`.

    A module-level generator (NOT a bound method) so its frame captures only
    `sorter` and `expected_row_count`, never an `_OrderedJoinRows` instance --
    see that class's `__init__` for why the finalizer depends on that.
    """
    expected_next = 0
    seen = 0
    for batch in sorter.iter_ordered():
        n = batch.num_rows
        if n == 0:
            continue
        row_nr = batch.column("__decoy_row_nr")
        # Reject nulls explicitly: `pc.all` below SKIPS null diffs, so a null
        # row_nr would slip the adjacent-difference check while still counting
        # toward `seen`/`expected_next`. The sorter's own key contract already
        # rejects null keys, but the guard must not depend on that to stay
        # fail-closed on its own.
        if row_nr.null_count > 0:
            raise _contiguity_error("the ordered stream contains a null row_nr")
        first = row_nr[0].as_py()
        if first != expected_next:
            raise _contiguity_error(f"a batch started at row_nr {first}, expected {expected_next}")
        if n > 1:
            # pc.* funcs are dynamically generated; stubs miss them.
            diffs = pc.subtract(  # type: ignore[attr-defined, unused-ignore]
                row_nr.slice(1, n - 1), row_nr.slice(0, n - 1)
            )
            if not pc.all(pc.equal(diffs, 1)).as_py():  # type: ignore[attr-defined, unused-ignore]
                raise _contiguity_error(
                    f"row_nr [{first}..{row_nr[n - 1].as_py()}] is not a "
                    "contiguous ascending run (duplicate or missing value)"
                )
        expected_next = first + n
        seen += n
        yield batch
    # The per-batch first-of-batch check makes `expected_next` telescope to
    # exactly `seen` (each batch begins where the last ended), so the second
    # clause is provably redundant with the first -- kept as intentional
    # belt-and-suspenders on the safety-critical count. (A mutation dropping it
    # therefore survives; that survivor is accepted, not a coverage gap.)
    if seen != expected_row_count or expected_next != expected_row_count:
        raise _contiguity_error(
            f"expected exactly {expected_row_count} contiguous rows (the "
            f"independent child-stage count) but the ordered stream produced "
            f"{seen} row(s), ending at row_nr {expected_next - 1}"
        )


def _release_reorder(
    guarded_iter: Generator[pa.RecordBatch, None, None], sorter: BoundedExternalSorter
) -> None:
    """Close the guarded generator FIRST (GeneratorExit unwinds
    `iter_ordered`'s open run-file FD), then unlink the sorter's spill. Guarded
    so a failure closing one still attempts the other. Bound as a
    `weakref.finalize` callback, so it must reference neither the owning object
    nor any bound method of it."""
    try:
        guarded_iter.close()
    finally:
        sorter.close()


class _OrderedJoinRows:
    """Owning, closeable iterator over one edge's order-restored join rows.

    Returned by `StreamFkJoiner.run_ordered_join`, which has ALREADY drained
    the unordered join into `sorter` and closed the DuckDB connection by the
    time this object exists -- the only resource it owns is the sorter's
    final ordered run on disk. Implements `__iter__`/`__next__`, the context-
    manager protocol, and an idempotent `close()` (Codex plan-gate MEDIUM 6):
    a bare generator's `try/finally` does not run reliably when a caller
    abandons it before the first `next()` (cleanup would wait for
    nondeterministic GC), so the consumer contract is `with
    joiner.run_ordered_join(...) as rows:` or an explicit `close()` -- the
    only reliable cleanup for abandonment before first use, after partial
    consumption, or on error. The iterator also self-closes on normal
    exhaustion AND on any exception (including its own contiguity failure),
    so no exit path leaks the sorter's spill.

    Wraps `sorter.iter_ordered()` in the fail-closed 0..N-1 contiguity guard:
    `N` (`expected_row_count`) is the INDEPENDENT child-stage row count (the
    `SpillChildKeys` count `run_ordered_join` captured BEFORE the join ran),
    never inferred from the join output itself, so a lost suffix cannot
    silently self-validate as a shorter dense range. A gap is detected only
    when the batch AFTER it arrives (or, for a lost suffix, only once the
    whole stream is exhausted) -- earlier batches have already been yielded
    to the caller by then, so this is an ALL-OR-NOTHING contract: the
    consumer must not commit any batch to durable output until this iterator
    is fully drained without raising.
    """

    def __init__(self, sorter: BoundedExternalSorter, expected_row_count: int) -> None:
        self._sorter = sorter
        # The guarded iterator is a STANDALONE generator (not a bound method), so
        # its frame captures only `sorter`/`expected_row_count`, never `self`.
        # That is what lets the finalizer below hold `self._iter` as an argument
        # without transitively pinning `self` alive -- a bound-method generator's
        # frame WOULD reference self, forming a cycle that defeats the whole GC
        # backstop.
        self._iter = _guarded_reorder_iter(sorter, expected_row_count)
        self._closed = False
        # GC backstop for the pure-abandonment path (dropped without close() or a
        # `with` block): a bare generator would run its `finally` on the
        # `GeneratorExit` GC throws at it, so this owning object -- which exists
        # precisely to HARDEN abandonment -- must be at least as safe, not worse.
        # `_release_reorder` closes the guarded generator (propagating
        # GeneratorExit into `iter_ordered`'s `pa.OSFile` context, releasing the
        # FD) and the sorter's spill. `close()` fires it explicitly (at-most-once
        # and detaching), and GC fires it otherwise.
        self._finalizer = weakref.finalize(self, _release_reorder, self._iter, sorter)

    def __iter__(self) -> _OrderedJoinRows:
        return self

    def __next__(self) -> pa.RecordBatch:
        try:
            return next(self._iter)
        except BaseException:
            # StopIteration (normal exhaustion) and any raised failure
            # (contiguity violation, or anything unexpected) both end this
            # iterator's life -- close it either way, exactly once.
            self.close()
            raise

    def __enter__(self) -> _OrderedJoinRows:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the ordered-run FD (by closing the guarded generator) and
        unlink the sorter's spill. Idempotent; safe before the first `next()`,
        after partial consumption, or called more than once."""
        if self._closed:
            return
        self._closed = True
        # Runs `_release_reorder(self._iter, self._sorter)` exactly once and
        # detaches, so the GC backstop above becomes a no-op after explicit close.
        self._finalizer()


class JoinRowCursor:
    """Forward-only cursor over one edge's ordered raw join-row reader.

    Wraps `StreamFkJoiner.iter_join_rows`, whose own batch boundaries are
    DuckDB's (not the payload store's). `take(n, expected_row_nr_start)`
    returns exactly `n` raw join rows as one `pa.RecordBatch`, slicing across
    reader batch boundaries, holding at most one reader batch plus a slice
    offset, so memory stays bounded regardless of alignment. The result is fed
    straight to `StreamFkJoiner.resolve_batch`.

    Correctness backbone: the join output covers every child row exactly once,
    contiguously numbered 0..N-1 in `ORDER BY` order. Unlike the removed
    `FkOutputCursor` (which only checked that the join reader was contiguous
    with ITS OWN running count -- a check that a two-read design's second pass
    could satisfy even while silently misaligned to the first), `take` also
    asserts the join reader's current position equals `expected_row_nr_start`,
    the row_nr the DRIVER read off the payload store -- an artifact of the
    SAME single source read as the join keys. That makes this an identity
    check between two artifacts of one read, not a hope that two reads agree.
    """

    def __init__(self, reader: Iterator[pa.RecordBatch], join_columns: tuple[str, ...]) -> None:
        self._reader = reader
        self._join_columns = join_columns
        self._batch: pa.RecordBatch | None = None
        self._offset = 0
        self._emitted = 0

    def take(self, n: int, expected_row_nr_start: int) -> pa.RecordBatch:
        """Return exactly `n` raw join rows starting at `expected_row_nr_start`."""
        if self._emitted != expected_row_nr_start:
            raise _row_alignment_error(
                f"the join reader is positioned at row_nr {self._emitted}, but the "
                f"payload store's next batch starts at row_nr {expected_row_nr_start}"
            )
        if n == 0:
            return self._schema_probe().slice(0, 0)
        collected: list[pa.RecordBatch] = []
        remaining = n
        while remaining > 0:
            batch = self._current()
            available = batch.num_rows - self._offset
            take_k = min(available, remaining)
            collected.append(batch.slice(self._offset, take_k))
            self._offset += take_k
            self._emitted += take_k
            remaining -= take_k
        return collected[0] if len(collected) == 1 else _concat_join_row_batches(collected)

    def assert_exhausted(self) -> None:
        """Fail closed unless every raw join row has been consumed.

        The other half of the alignment guard: the payload store must not be
        SHORTER than the join output either, or the tail join rows would be
        silently dropped.
        """
        if self._batch is not None and self._offset < self._batch.num_rows:
            raise _row_alignment_error(
                f"{self._batch.num_rows - self._offset} join row(s) left unconsumed"
            )
        try:
            next(self._reader)
        except StopIteration:
            return
        raise _row_alignment_error("join row reader has rows the payload store never consumed")

    def _schema_probe(self) -> pa.RecordBatch:
        # A zero-row take still needs a batch of the right schema; reuse the
        # last-seen reader batch if there is one, otherwise pull the next one
        # (its rows are left untouched, offset stays where it was).
        return self._batch if self._batch is not None else self._current()

    def _current(self) -> pa.RecordBatch:
        batch = self._batch
        if batch is None or self._offset >= batch.num_rows:
            batch = self._advance()
            self._batch = batch
            self._offset = 0
        return batch

    def _advance(self) -> pa.RecordBatch:
        try:
            batch = next(self._reader)
        except StopIteration:
            raise _row_alignment_error(
                "join row reader exhausted before the payload store did "
                "(join output shorter than the masked payload)"
            ) from None
        row_nr = batch.column("__decoy_row_nr")
        first = row_nr[0].as_py()
        last = row_nr[len(row_nr) - 1].as_py()
        # Each ordered batch must start exactly where the running count left off
        # and be a contiguous ascending run, so positional slicing equals
        # row_nr slicing. This is the join reader's OWN internal contiguity,
        # independent of (and in addition to) the cross-artifact check in take().
        if first != self._emitted or last != self._emitted + len(row_nr) - 1:
            raise _row_alignment_error(
                f"join row_nr [{first}..{last}] does not match the expected "
                f"contiguous range starting at {self._emitted}"
            )
        return batch


def _concat_join_row_batches(batches: list[pa.RecordBatch]) -> pa.RecordBatch:
    table = pa.Table.from_batches(batches).combine_chunks()
    return table.to_batches()[0]


def _row_alignment_error(detail: str) -> ExecutionError:
    return ExecutionError(
        code="out_of_core_fk_row_alignment",
        message=(
            "out-of-core FK join output could not be row-aligned to the masked "
            f"payload: {detail}. The join row_nr must match the payload row_nr "
            "captured in the single source read; this is a fail-closed internal "
            "guard, never silent truncation or misalignment."
        ),
    )
