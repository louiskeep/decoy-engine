"""F-audit witness extensions (test-side only): producer-boundary witnesses and the zero-companion
spy.

`_f0_witness.EndToEndWitness` classifies a column from selection events (RNG, numpy, provider,
formula). Four compute boundaries emit no such event: `sequence`, `reference` with
`distribution: sequential` (a direct index), `windowed_date`, and the legacy `derived` /
`derived_aggregate`. Today those are labelled `no_selection` by the ABSENCE of an event, which a
path that bypasses the producer satisfies just as well. `AuditWitness` records the producer's
OUTPUT at its real boundary and requires it to be what the final column holds.

Core boundaries MARK the value they hand downstream (a prefix on strings, +`WINDOW_MARK_DAYS` on a
windowed date). A mutant that runs the producer, discards its result and substitutes an
independently recomputed value therefore cannot reproduce the column even though the recomputed
value is deterministic and equal to the unmarked one. That is the same marking-kernel discipline
the D-audit uses for the masking kernels.

`DeterministicIndexSpy` observes the deterministic pool-index entry (`_sampler._derive_pool_indices`
and `PoolSampler._deterministic`) with record-and-delegate wrappers, so a mutant that reaches it
still runs the real body while the audit counts the call.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from decoy_engine.generation import _column_streams, _plan_entry, _streamed, synthesize
from decoy_engine.generation.pool import _sampler
from decoy_engine.generation.pool._sampler import PoolSampler
from tests.parity import _f0_witness as w

BOUNDARY_MARK = "⟦F⟧"
WINDOW_MARK_DAYS = 3

CORE_BOUNDARY_KINDS = frozenset({"sequence", "windowed_date"})
LEGACY_BOUNDARY_KINDS = frozenset({"derived", "derived_aggregate"})


def mark_str(v: Any) -> Any:
    return None if v is None else f"{BOUNDARY_MARK}{v}"


def mark_date(v: Any) -> Any:
    if v is None:
        return None
    return (dt.date.fromisoformat(v) + dt.timedelta(days=WINDOW_MARK_DAYS)).isoformat()


class AuditWitness(w.EndToEndWitness):
    """`EndToEndWitness` plus producer-boundary outputs for the event-less compute kinds."""

    def __init__(
        self,
        mp: pytest.MonkeyPatch,
        *,
        marking: bool = False,
        attribution: Callable[[w.ColumnKey], w.ColumnKey] | None = None,
        boundary_marking: bool = True,
    ) -> None:
        super().__init__(mp, marking=marking, attribution=attribution)
        self.boundary_marking = boundary_marking
        self.cols: dict[w.ColumnKey, dict[str, Any]] = {}
        self.boundary: dict[w.ColumnKey, list[w.Event]] = {}

    def _emit(self, kind: str, result: list[Any]) -> None:
        key = self._current
        if key is None:
            return  # a producer call outside any generated column
        self.boundary.setdefault(self.attribution(key), []).append(
            w.Event(kind, {"result": list(result)})
        )

    def install(self) -> None:
        super().install()
        mp, outer = self.mp, self

        core_make = _streamed.make_column_stream

        def make_stream(col: dict[str, Any], **kw: Any) -> Any:
            outer.cols[(kw["table"], str(col.get("name")))] = col
            return core_make(col, **kw)

        mp.setattr(_streamed, "make_column_stream", make_stream)

        legacy_column = synthesize._generate_column

        def generate_column(col: dict[str, Any], n: int, *a: Any, **kw: Any) -> list[Any]:
            outer.cols[(kw.get("table_name", ""), str(col.get("name")))] = col
            return legacy_column(col, n, *a, **kw)

        mp.setattr(synthesize, "_generate_column", generate_column)

        marker = mark_str if self.boundary_marking else (lambda v: v)
        date_marker = mark_date if self.boundary_marking else (lambda v: v)

        real_seq = _column_streams.SequenceStream.next_batch

        def seq(self: Any, offset: int, size: int) -> list[Any]:
            produced = [marker(v) for v in real_seq(self, offset, size)]
            outer._emit("sequence", produced)
            return produced

        mp.setattr(_column_streams.SequenceStream, "next_batch", seq)

        real_ref = _column_streams.ReferenceStream.next_batch

        def ref(self: Any, offset: int, size: int) -> list[Any]:
            out = real_ref(self, offset, size)
            if self._distribution != "sequential":
                return out
            produced = [marker(v) for v in out]
            outer._emit("reference_sequential", produced)
            return produced

        mp.setattr(_column_streams.ReferenceStream, "next_batch", ref)

        real_win = _column_streams.WindowedDateStream.next_batch_from

        def win(self: Any, offset: int, size: int, siblings: Mapping[str, list[Any]]) -> list[Any]:
            produced = [date_marker(v) for v in real_win(self, offset, size, siblings)]
            outer._emit("windowed_date", produced)
            return produced

        mp.setattr(_column_streams.WindowedDateStream, "next_batch_from", win)

        real_derived = synthesize._derived_generate

        def derived(col: dict[str, Any], n: int, generated: Any) -> list[Any]:
            out = real_derived(col, n, generated)
            outer._emit("derived", out)
            return out

        mp.setattr(synthesize, "_derived_generate", derived)

        real_agg = synthesize.generate_derived_aggregate_column

        def agg(col: dict[str, Any], n: int, generated: Any) -> list[Any]:
            out = list(real_agg(col, n, generated))
            outer._emit("derived_aggregate", out)
            return out

        mp.setattr(synthesize, "generate_derived_aggregate_column", agg)

    # -- classification ----------------------------------------------------------------------

    def _boundary_kind(self, key: w.ColumnKey) -> str | None:
        """Which producer boundary must have witnessed this column, if any."""
        kind = self.kinds[key]
        on_core = key in self.core_keys
        if on_core and kind in CORE_BOUNDARY_KINDS:
            return kind
        if on_core and kind == "reference":
            if self.cols[key].get("distribution", "random") == "sequential":
                return "reference_sequential"
        if not on_core and kind in LEGACY_BOUNDARY_KINDS:
            return kind
        return None

    def _classify_one(self, key: w.ColumnKey, final: list[Any]) -> w.ColumnReport:
        boundary = self._boundary_kind(key)
        if boundary is None:
            return super()._classify_one(key, final)
        events = self.events.get(key, [])
        labels = {e.label for e in events}
        if labels - w.SUBPATH_LABELS:
            raise w.WitnessError(f"{key}: {boundary} must not select, saw {sorted(labels)}")
        nulled = {i for e in events if e.label == w.NULL_MASK for i in e.payload["nulled"]}
        recorded = self.boundary.get(key, [])
        if not recorded:
            raise w.WitnessError(f"{key}: no output was recorded at the {boundary} boundary")
        produced = [v for e in recorded for v in e.payload["result"]]
        if len(produced) != len(final):
            raise w.WitnessError(f"{key}: {boundary} boundary produced a different row count")
        live = [i for i in range(len(final)) if i not in nulled]
        if not all(w._same(final[i], produced[i]) for i in live):
            raise w.WitnessError(f"{key}: {boundary} output is not what the column holds")
        self._require_nonvacuous(key, final, live)
        label = w.WINDOWED_DATE_SHIFT if boundary == "windowed_date" else w.NO_SELECTION
        return w.ColumnReport(
            key, self.kinds[key], frozenset({label}), frozenset(labels & w.SUBPATH_LABELS)
        )


@contextmanager
def audit(
    mp: pytest.MonkeyPatch,
    *,
    marking: bool = False,
    attribution: Callable[[w.ColumnKey], w.ColumnKey] | None = None,
    boundary_marking: bool = True,
) -> Iterator[AuditWitness]:
    witness = AuditWitness(
        mp, marking=marking, attribution=attribution, boundary_marking=boundary_marking
    )
    witness.install()
    yield witness


# --------------------------------------------------------------------------------------------
# the deterministic-index entry spy (zero-companion lock)
# --------------------------------------------------------------------------------------------


class _CountingKernel:
    """An index kernel whose `derive_index_batch` increments a counter, then delegates."""

    def __init__(self, real: Any, count: Callable[[], None]) -> None:
        self._real, self._count = real, count

    def derive_index_batch(self, *args: Any, **kwargs: Any) -> Any:
        self._count()
        return self._real.derive_index_batch(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


@dataclass
class DeterministicIndexSpy:
    """Counts entries to the deterministic pool-index derivation, never replacing its behavior.

    `index_entries` counts `_derive_pool_indices` (the real lookup `_deterministic` calls) and
    `deterministic_calls` counts `PoolSampler._deterministic`; `compiled_calls` and
    `reference_calls` count which kernel then ran. Generation must leave all four at zero.
    """

    index_entries: int = 0
    deterministic_calls: int = 0
    compiled_calls: int = 0
    reference_calls: int = 0
    # Count only inside `_plan_entry.run_generation`, so a legitimate MASK-side compiled call in
    # the same process (the mixed route) is not mistaken for generation reaching the kernel.
    generation_only: bool = False
    _depth: int = 0

    def _hit(self, name: str) -> None:
        if self.generation_only and not self._depth:
            return
        setattr(self, name, getattr(self, name) + 1)

    def install(self, mp: pytest.MonkeyPatch) -> DeterministicIndexSpy:
        spy = self
        real_indices = _sampler._derive_pool_indices
        real_det = PoolSampler._deterministic
        real_compiled = _sampler._compiled_index_kernel
        real_reference = _sampler._reference_index_kernel

        def indices(*a: Any, **kw: Any) -> Any:
            spy._hit("index_entries")
            return real_indices(*a, **kw)

        def deterministic(self: PoolSampler, *a: Any, **kw: Any) -> Any:
            spy._hit("deterministic_calls")
            return real_det(self, *a, **kw)

        def compiled() -> Any:
            kernel = real_compiled()
            return None if kernel is None else _CountingKernel(kernel, spy._count_compiled)

        def reference() -> Any:
            return _CountingKernel(real_reference(), spy._count_reference)

        if self.generation_only:
            real_run = _plan_entry.run_generation

            def scoped_run(*a: Any, **kw: Any) -> Any:
                spy._depth += 1
                try:
                    return real_run(*a, **kw)
                finally:
                    spy._depth -= 1

            mp.setattr(_plan_entry, "run_generation", scoped_run)

        mp.setattr(_sampler, "_derive_pool_indices", indices)
        mp.setattr(PoolSampler, "_deterministic", deterministic)
        mp.setattr(_sampler, "_compiled_index_kernel", compiled)
        mp.setattr(_sampler, "_reference_index_kernel", reference)
        return self

    def _count_compiled(self) -> None:
        self._hit("compiled_calls")

    def _count_reference(self) -> None:
        self._hit("reference_calls")

    def total(self) -> int:
        return (
            self.index_entries
            + self.deterministic_calls
            + self.compiled_calls
            + self.reference_calls
        )

    def assert_generation_never_entered(self, label: str) -> None:
        """The forbidden-path invariant: asserted AFTER the run, so a mutant executed first."""
        if self.total():
            raise AssertionError(
                f"{label}: generation entered the deterministic-index path "
                f"(entries={self.index_entries}, deterministic={self.deterministic_calls}, "
                f"compiled={self.compiled_calls}, reference={self.reference_calls})"
            )
