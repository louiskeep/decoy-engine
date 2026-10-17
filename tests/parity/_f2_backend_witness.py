"""Kernel-ABI insertion-boundary witness for the F2 mixed generate+mask route (test-side only).

The mixed route's tests already read the `executed_backend` telemetry stamp. That proves
what the route REPORTED, not that the compiled kernel's output is what got published. A
refactor could keep the "mixed" label and the stamp while a column's values quietly come
from the pandas oracle. This witness instruments the actual kernel boundaries instead,
the way `_kernel_witness.py` does for the out-of-core seam:

* the compiled-kernel ABI call: `_kernels_keyed.load_compiled_crypto_kernel` is replaced
  (patch-where-looked-up: `native_keyed_hash` resolves the name in its own module globals)
  by a loader returning a proxy whose `derive_batch` records the array the compiled
  kernel RETURNED;
* the Arrow redact kernel: `_kernels_scalar.redact_array`, bound by name in that module;
* the insertion point: `_chunked_entry._mask_chunk_native`, whose returned chunk table is
  what the lane carries on to the sink.

Provenance is by buffer identity, not value equality: the kernel's returned array must BE
the column the masked chunk carries, so a kernel call whose result is thrown away in
favour of an equal-valued array computed elsewhere is caught. The concatenation of the
recorded results must then equal the committed output column, cell for cell.

Nothing is recorded for the oracle (it never goes through these bindings), so a job that
ran the oracle for a column leaves the witness empty and the assertions fail.

Log hygiene: assertion messages carry counts and column names only, never a value.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native import _chunked_entry, _kernels_keyed, _kernels_scalar

HASH = "hash"
REDACT = "redact"


def same_buffers(a: pa.Array, b: pa.Array) -> bool:
    """True when `a` and `b` are the same Arrow data (same buffers), not equal copies."""
    if len(a) != len(b):
        return False
    if len(a) == 0:
        return True
    ba, bb = a.buffers(), b.buffers()
    return (
        a.type == b.type
        and a.offset == b.offset
        and len(ba) == len(bb)
        and all(
            (x is None and y is None)
            or (x is not None and y is not None and x.address == y.address)
            for x, y in zip(ba, bb, strict=True)
        )
    )


def _combine(values: pa.Array | pa.ChunkedArray) -> pa.Array:
    """One array. A single-chunk column is unwrapped, not combined: `combine_chunks`
    copies even one chunk, which would hide the buffers the identity check compares."""
    if not isinstance(values, pa.ChunkedArray):
        return values
    return values.chunk(0) if values.num_chunks == 1 else values.combine_chunks()


def _equal_copy(values: pa.Array) -> pa.Array:
    """Same cells, different buffers: what an oracle-computed column would look like."""
    return pa.array(values.to_pylist(), values.type)


class _KernelProxy:
    """The compiled crypto kernel, with `derive_batch` observed. Everything else delegates."""

    def __init__(self, real: Any, record: Callable[[pa.Array], pa.Array]) -> None:
        self._real = real
        self._record = record

    def derive_batch(self, *args: Any, **kwargs: Any) -> pa.Array:
        return self._record(self._real.derive_batch(*args, **kwargs))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@dataclass
class F2BackendWitness:
    # What each kernel boundary returned, in call order.
    results: dict[str, list[pa.Array]] = field(default_factory=lambda: {HASH: [], REDACT: []})
    # The masked chunk tables the lane received from the native chunk masker.
    masked_chunks: list[pa.Table] = field(default_factory=list)
    # Negative-control switch: hand the consumer an equal-valued copy instead of the
    # kernel's own array, recording the kernel's real result.
    substitute_equal_copy: bool = False

    def _record(self, kind: str, result: pa.Array) -> pa.Array:
        self.results[kind].append(result)
        return _equal_copy(result) if self.substitute_equal_copy else result

    @contextmanager
    def observe(self) -> Iterator[F2BackendWitness]:
        real_loader = _kernels_keyed.load_compiled_crypto_kernel
        real_redact = _kernels_scalar.redact_array
        real_mask_chunk = _chunked_entry._mask_chunk_native

        def loader() -> Any:
            return _KernelProxy(real_loader(), lambda r: self._record(HASH, r))

        def redact(*args: Any, **kwargs: Any) -> Any:
            return self._record(REDACT, real_redact(*args, **kwargs))

        def mask_chunk(*args: Any, **kwargs: Any) -> pa.Table:
            table = real_mask_chunk(*args, **kwargs)
            self.masked_chunks.append(table)
            return table

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_kernels_keyed, "load_compiled_crypto_kernel", loader)
            mp.setattr(_kernels_scalar, "redact_array", redact)
            mp.setattr(_chunked_entry, "_mask_chunk_native", mask_chunk)
            yield self

    def assert_kernel_result_consumed(
        self, kind: str, column: str, committed: pa.Table | pa.ChunkedArray
    ) -> None:
        """The `kind` kernel ran for `column`, its returned arrays are the ones the masked
        chunks carry, and together they are the committed output column."""
        results = self.results[kind]
        if not results:
            raise AssertionError(f"{kind} kernel boundary never called: {column} not native")
        if len(results) != len(self.masked_chunks):
            raise AssertionError(
                f"{len(results)} {kind} kernel call(s) for {len(self.masked_chunks)} masked "
                f"chunk(s): {column} is not one kernel result per chunk"
            )
        for i, (result, chunk) in enumerate(zip(results, self.masked_chunks, strict=True)):
            if not same_buffers(result, _combine(chunk.column(column))):
                raise AssertionError(
                    f"{kind} kernel result of chunk {i} was discarded: the masked chunk "
                    f"carries a different array for {column}"
                )
        produced = pa.concat_arrays(results)
        emitted = _combine(
            committed.column(column) if isinstance(committed, pa.Table) else committed
        )
        if produced.to_pylist() != emitted.to_pylist():
            raise AssertionError(
                f"{kind} kernel results do not equal the committed {column} column"
            )
