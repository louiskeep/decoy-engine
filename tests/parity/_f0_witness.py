"""Executed-backend witness for generation selection (Phase F F0, test-side only).

Question it answers, per output column: WHICH backend actually produced the selected values, and
were that backend's results the ones that ended up in the column? Counting calls cannot answer
it, because a path can run a backend, drop the result, and fill the column some other way.

Modeled on the D-audit marking-kernel witness (`test_phase_d_audit_no_legacy.py`,
`_kernel_witness.py`): the witness MARKS what the backend produced so that the mark must be
visible in the final column, and the mutants (discarded result, recompute, wrong-column
attribution) are shown to fail it.

Taxonomy (the full set the generation path uses today; verified against the dispatch):

=========================  ================================================================
label                      where it happens
=========================  ================================================================
numpy_selection            `PoolSampler.sample` non-deterministic REUSE: `np.random.default_rng`
                           (`pool/_sampler.py`), reached by pooled `faker` columns
compiled_index_selection   the compiled `derive_index_batch` (`pool/_sampler.py` loader): the
                           deterministic sampler, and (FR3) the unified core's position-keyed
                           uniform / weighted selections (`_kernel_select.PositionalIndexer`)
python_index_reference     the byte-identical Python reference kernel, picked by the same lookup
                           when the companion is absent (or for a value class it declines)
python_rng_selection       `random.Random.choices` / `.choice` (categorical `synthesize.py:377`,
                           reference `:588-604`)
composite_direct_derive    `derive(...)` + modulo in `composite/_person.py:171` and
                           `_name_email.py`, which bypass `_sampler` entirely
live_provider_generation   unpooled `_faker`: one provider call per output ROW
no_selection               `sequence`, `derived`, `derived_aggregate`: no selection at all
formula_expression         an evaluated `formula` (the expression may draw from its own per-row
                           RNG; that is expression evaluation, not a generator selection)
=========================  ================================================================

Two production paths feed this taxonomy since the unified generation core: the legacy whole-table
generator (`synthesize._generate_column`, plans with an ineligible column) and the core's
persistent per-column streams (`_column_streams`, eligible plans). The witness hooks both and
labels a column the same way whichever path produced it; the core hooks wrap each stream's
`next_batch`, so a column's events and final values are accumulated across batches.

FR3 moved the core's uniform and weighted selections onto the index kernel. The witness records
each kernel call's RESULT under the column that was being drawn, reads the stream's choice list
and indexer, and requires every live cell to equal `choices[index]`. A `kernel_shift` skews the
kernel's result so a consumer that drops the result and recomputes cannot reproduce the column.

Two further sub-paths are recorded but are NEVER folded into a selection backend:
`pool_construction` (provider calls inside `build_pool_values`) and `null_mask` (the
`null_probability` post-process). `statistical`, `grouped_series`, `windowed_date`, `group_key`
are intentionally UNOBSERVED here (they do not route through a selection backend F3 targets) and
the classifier raises rather than label them `no_selection`.

Evidence labels: every report carries `evidence`, either `real` (a real compiled kernel or a real
production call executed) or `stand_in` (a pure-Python stand-in kernel used to test the assertion
logic where no companion is installed). The two are never merged.
"""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.generation import (
    _column_streams,
    _faker_pool,
    _kernel_select,
    _streamed,
    synthesize,
)
from decoy_engine.generation.pool import _sampler
from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.generation.pool._sampler import PoolSampler
from decoy_engine.generators.columns import ColumnGenerator

NUMPY_SELECTION = "numpy_selection"
COMPILED_INDEX = "compiled_index_selection"
PYTHON_INDEX_REFERENCE = "python_index_reference"
PYTHON_RNG = "python_rng_selection"
COMPOSITE_DERIVE = "composite_direct_derive"
LIVE_PROVIDER = "live_provider_generation"
NO_SELECTION = "no_selection"
FORMULA_EXPRESSION = "formula_expression"
POOL_CONSTRUCTION = "pool_construction"
NULL_MASK = "null_mask"

SELECTION_LABELS = frozenset(
    {
        NUMPY_SELECTION,
        COMPILED_INDEX,
        PYTHON_INDEX_REFERENCE,
        PYTHON_RNG,
        COMPOSITE_DERIVE,
        LIVE_PROVIDER,
        NO_SELECTION,
        FORMULA_EXPRESSION,
    }
)
SUBPATH_LABELS = frozenset({POOL_CONSTRUCTION, NULL_MASK})
REAL = "real"
STAND_IN = "stand_in"

_UNOBSERVED_KINDS = frozenset({"statistical", "grouped_series", "windowed_date", "group_key"})
_NO_EVENT_KINDS = frozenset({"sequence", "derived", "derived_aggregate"})


class WitnessError(AssertionError):
    """The witness could not prove a column's values came from the backend it observed."""


ColumnKey = tuple[str, str]


@dataclass
class Event:
    label: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ColumnReport:
    key: ColumnKey
    kind: str
    selection: frozenset[str]
    subpaths: frozenset[str]
    evidence: str = REAL


# --------------------------------------------------------------------------------------------
# marking helpers (pool value i is rendered "⟦i⟧value")
# --------------------------------------------------------------------------------------------

_MARK = re.compile(r"^⟦(\d+)⟧")


def mark_value(i: int, value: Any) -> str:
    return f"⟦{i}⟧{value}"


def decode_mark(value: Any) -> int | None:
    m = _MARK.match(value) if isinstance(value, str) else None
    return int(m.group(1)) if m else None


def strip_mark(value: Any) -> Any:
    return _MARK.sub("", value) if isinstance(value, str) else value


# --------------------------------------------------------------------------------------------
# end-to-end witness over generate_tables
# --------------------------------------------------------------------------------------------


class _RecordingRandom(random.Random):
    """`random.Random` with identical streams that logs each selection result it returns."""

    sink: Callable[[str, dict[str, Any]], None]

    def choices(
        self, population: Any, weights: Any = None, *, cum_weights: Any = None, k: int = 1
    ) -> list[Any]:
        out = super().choices(population, weights, cum_weights=cum_weights, k=k)
        type(self).sink(PYTHON_RNG, {"method": "choices", "result": list(out)})
        return out

    def choice(self, seq: Any) -> Any:
        out = super().choice(seq)
        type(self).sink(PYTHON_RNG, {"method": "choice", "result": [out]})
        return out


class _NpProxy:
    """`numpy` stand-in for `_sampler.np` that logs `random.default_rng(seed)` and delegates."""

    def __init__(self, sink: Callable[[str, dict[str, Any]], None]) -> None:
        self._sink = sink
        self.random = SimpleNamespace(default_rng=self._default_rng)

    def _default_rng(self, seed: Any = None) -> Any:
        self._sink(NUMPY_SELECTION, {"stage": "default_rng", "seed": int(seed)})
        return np.random.default_rng(seed)

    def __getattr__(self, name: str) -> Any:
        return getattr(np, name)


class _Snapshot:
    """Sits under a null mask so the pre-mask values can be compared with the masked ones."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.last: list[Any] = []

    def next_batch(self, offset: int, size: int) -> list[Any]:
        values = self._inner.next_batch(offset, size)
        self.last = list(values)
        return values


class _ObservedStream:
    """A core column stream that attributes every event of a batch to its column."""

    def __init__(self, outer: EndToEndWitness, key: ColumnKey, inner: Any) -> None:
        self._outer, self._key, self._inner = outer, key, inner
        self._snapshot: _Snapshot | None = None
        if isinstance(inner, _column_streams.NullMaskedStream):
            self._snapshot = _Snapshot(inner._inner)
            inner._inner = self._snapshot

    def next_batch(self, offset: int, size: int) -> list[Any]:
        outer = self._outer
        outer._current = self._key
        try:
            values = self._inner.next_batch(offset, size)
        finally:
            outer._current = None
        outer.final.setdefault(self._key, []).extend(values)
        if self._snapshot is not None:
            nulled = [
                offset + i
                for i, (before, after) in enumerate(zip(self._snapshot.last, values, strict=True))
                if after is None and before is not None
            ]
            outer._record(NULL_MASK, {"nulled": nulled}, self._key)
        return values


class _ColumnSink:
    """List-like sink that files each kernel event under the column being drawn."""

    def __init__(self, outer: EndToEndWitness) -> None:
        self._outer = outer

    def append(self, event: Event) -> None:
        self._outer._record(event.label, event.payload)


class EndToEndWitness:
    """Installs recording hooks around `generate_tables` and classifies every generated column.

    `marking=True` marks pooled-faker pool values (`⟦i⟧value`) so the selected indices can be read
    back out of the final column. `attribution` lets a mutant record events under the wrong column.
    """

    def __init__(
        self,
        mp: pytest.MonkeyPatch,
        *,
        marking: bool = False,
        attribution: Callable[[ColumnKey], ColumnKey] | None = None,
        kernel_shift: int = 0,
        compiled: Any = "real",
        compiled_evidence: str = REAL,
    ) -> None:
        self.mp = mp
        self.marking = marking
        self.kernel_shift = kernel_shift
        # "real": whatever the loader finds (None without the companion, so the reference runs).
        self._compiled = _sampler._compiled_index_kernel() if compiled == "real" else compiled
        self._compiled_evidence = compiled_evidence
        self.choices: dict[ColumnKey, list[Any]] = {}
        self.indexers: dict[ColumnKey, Any] = {}
        self.attribution = attribution or (lambda k: k)
        self.events: dict[ColumnKey, list[Event]] = {}
        self.kinds: dict[ColumnKey, str] = {}
        self.final: dict[ColumnKey, list[Any]] = {}
        self.pool_values: dict[ColumnKey, list[Any]] = {}
        self._current: ColumnKey | None = None
        self._table: str = ""

    # -- recording ---------------------------------------------------------------------------

    def _record(self, label: str, payload: dict[str, Any], key: ColumnKey | None = None) -> None:
        key = key if key is not None else self._current
        if key is None:
            return  # a call outside any generated column (for example the compile step)
        self.events.setdefault(self.attribution(key), []).append(Event(label, payload))

    def install(self) -> None:
        mp, outer = self.mp, self
        real_generate_column = synthesize._generate_column

        def generate_column(col: dict[str, Any], n: int, *a: Any, **kw: Any) -> list[Any]:
            outer._table = kw.get("table_name", "")
            key = (outer._table, str(col.get("name")))
            outer._current = key
            outer.kinds[key] = str(col.get("type"))
            outer.events.setdefault(outer.attribution(key), [])
            result = real_generate_column(col, n, *a, **kw)
            outer.final[key] = list(result)
            outer._current = None
            return result

        mp.setattr(synthesize, "_generate_column", generate_column)

        # Python RNG: the sink is a class attribute so every `random.Random(seed)` is recorded.
        _RecordingRandom.sink = lambda label, payload: outer._record(label, payload)
        mp.setattr(synthesize, "random", SimpleNamespace(Random=_RecordingRandom))

        # Live provider generation: wrap each provider the per-row path resolves.
        real_providers = synthesize.get_faker_providers

        def providers(*a: Any, **kw: Any) -> dict[str, Callable[..., Any]]:
            def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
                def called(*args: Any, **kwargs: Any) -> Any:
                    out = fn(*args, **kwargs)
                    outer._record(LIVE_PROVIDER, {"result": out})
                    return out

                return called

            return {name: wrap(fn) for name, fn in real_providers(*a, **kw).items()}

        mp.setattr(synthesize, "get_faker_providers", providers)

        # The unified core: attribute each column's stream, and observe the same backends on it.
        real_make = _streamed.make_column_stream

        def make_stream(col: dict[str, Any], **kw: Any) -> Any:
            outer._table = kw["table"]
            key = (outer._table, str(col.get("name")))
            outer._current = key
            outer.kinds[key] = str(col.get("type"))
            outer.events.setdefault(outer.attribution(key), [])
            try:
                stream = real_make(col, **kw)
            finally:
                outer._current = None
            outer._capture_selection(key, stream)
            return _ObservedStream(outer, key, stream)

        mp.setattr(_streamed, "make_column_stream", make_stream)
        mp.setattr(_column_streams, "random", SimpleNamespace(Random=_RecordingRandom))

        real_core_providers = _column_streams.get_faker_providers

        def core_providers(*a: Any, **kw: Any) -> dict[str, Callable[..., Any]]:
            def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
                def called(*args: Any, **kwargs: Any) -> Any:
                    out = fn(*args, **kwargs)
                    outer._record(LIVE_PROVIDER, {"result": out})
                    return out

                return called

            return {name: wrap(fn) for name, fn in real_core_providers(*a, **kw).items()}

        mp.setattr(_column_streams, "get_faker_providers", core_providers)

        # The index kernel: every lookup the core's selections make returns a recorder that logs
        # the (optionally skewed) result under the column being drawn.
        from decoy_engine.execution.native._index_ext import reference_index_derivation

        sink = _ColumnSink(outer)
        compiled_recorder = (
            MarkingKernel(
                self._compiled,
                evidence=self._compiled_evidence,
                backend=COMPILED_INDEX,
                sink=sink,  # type: ignore[arg-type]
                shift=self.kernel_shift,
            )
            if self._compiled is not None
            else None
        )
        reference_recorder = MarkingKernel(
            reference_index_derivation(),
            evidence=REAL,
            backend=PYTHON_INDEX_REFERENCE,
            sink=sink,  # type: ignore[arg-type]
            shift=self.kernel_shift,
        )
        mp.setattr(_sampler, "_compiled_index_kernel", lambda: compiled_recorder)
        mp.setattr(_sampler, "_reference_index_kernel", lambda: reference_recorder)

        # Pool construction (kept apart from selection) and optional marking.
        real_build = _faker_pool.build_pool_values

        def build(*a: Any, **kw: Any) -> Any:
            values, exact, custom = real_build(*a, **kw)
            if outer.marking:
                values = [mark_value(i, v) for i, v in enumerate(values)]
            outer._record(POOL_CONSTRUCTION, {"values": list(values), "size": len(values)})
            outer.pool_values[outer._current or ("", "")] = list(values)
            return values, exact, custom

        mp.setattr(_faker_pool, "build_pool_values", build)

        # NumPy selection: the sampler's own `np` is proxied so default_rng(seed) is observed.
        mp.setattr(_sampler, "np", _NpProxy(lambda label, payload: outer._record(label, payload)))
        real_sample = PoolSampler.sample

        def sample(self: PoolSampler, pool: Any, n: int, **kw: Any) -> Any:
            result = real_sample(self, pool, n, **kw)
            outer._record(
                NUMPY_SELECTION,
                {
                    "stage": "sample",
                    "result": result.tolist(),
                    "pool_size": pool.size,
                    "n": n,
                    "deterministic": kw.get("deterministic", False),
                },
            )
            return result

        mp.setattr(PoolSampler, "sample", sample)

        # Null masking: its own sub-path.
        real_null = synthesize._apply_null_probability

        def null_mask(values: list[Any], col: dict[str, Any], *a: Any, **kw: Any) -> list[Any]:
            out = real_null(values, col, *a, **kw)
            if float(col.get("null_probability") or 0.0) > 0:
                nulled = [i for i, v in enumerate(out) if v is None and values[i] is not None]
                # The referenced-formula post-pass runs outside `_generate_column`: key it by name.
                key = None if outer._current else (outer._table, str(col.get("name")))
                outer._record(NULL_MASK, {"nulled": nulled}, key)
            return out

        mp.setattr(synthesize, "_apply_null_probability", null_mask)

        # Formula expression evaluation (inline and post-pass).
        real_inline = ColumnGenerator._eval_formula_inline
        real_fill = ColumnGenerator.fill_referenced_formula_column

        def inline(self: ColumnGenerator, *a: Any, **kw: Any) -> Any:
            series = real_inline(self, *a, **kw)
            outer._record(FORMULA_EXPRESSION, {"result": series.tolist(), "stage": "inline"})
            return series

        def fill(self: ColumnGenerator, col_name: str, *a: Any, **kw: Any) -> Any:
            series = real_fill(self, col_name, *a, **kw)
            outer._record(
                FORMULA_EXPRESSION,
                {"result": series.tolist(), "stage": "post_pass"},
                (outer._table, col_name),
            )
            return series

        mp.setattr(ColumnGenerator, "_eval_formula_inline", inline)
        mp.setattr(ColumnGenerator, "fill_referenced_formula_column", fill)

    def _capture_selection(self, key: ColumnKey, stream: Any) -> None:
        """Remember the choice list and indexer a kernel-selected stream draws from."""
        inner = stream._inner if isinstance(stream, _column_streams.NullMaskedStream) else stream
        if isinstance(inner, _column_streams.CategoricalStream):
            choices, indexer = inner._choices, inner._indexer
        elif isinstance(inner, _column_streams.ReferenceStream):
            choices, indexer = inner._vals, inner._indexer
        elif isinstance(inner, _column_streams.PooledFakerStream):
            choices, indexer = list(inner._selector._values), inner._selector._indexer
        else:
            return
        if indexer is not None:
            self.choices[key], self.indexers[key] = choices, indexer

    # -- classification ----------------------------------------------------------------------

    def classify(self, tables: dict[str, pa.Table]) -> dict[ColumnKey, ColumnReport]:
        reports: dict[ColumnKey, ColumnReport] = {}
        for table_name, tbl in tables.items():
            for col_name in tbl.column_names:
                key = (table_name, col_name)
                final = tbl.column(col_name).to_pylist()
                reports[key] = self._classify_one(key, final)
        return reports

    def _classify_one(self, key: ColumnKey, final: list[Any]) -> ColumnReport:
        kind = self.kinds[key]
        if kind in _UNOBSERVED_KINDS:
            raise WitnessError(f"{key}: kind {kind!r} is outside the witnessed taxonomy")
        events = self.events.get(key, [])
        labels = {e.label for e in events}
        nulled = {i for e in events if e.label == NULL_MASK for i in e.payload["nulled"]}
        selection: set[str] = set()

        def live_positions() -> list[int]:
            return [i for i in range(len(final)) if i not in nulled]

        py = [e for e in events if e.label == PYTHON_RNG]
        if py:
            drawn = [v for e in py for v in e.payload["result"]]
            if not (
                len(drawn) == len(final) and all(final[i] == drawn[i] for i in live_positions())
            ):
                raise WitnessError(f"{key}: python RNG selections were not what the column holds")
            selection.add(PYTHON_RNG)

        prov = [e for e in events if e.label == LIVE_PROVIDER]
        if prov:
            got = [e.payload["result"] for e in prov]
            if not (
                len(got) == len(final)
                and all(str(final[i]) == str(got[i]) for i in live_positions())
            ):
                raise WitnessError(f"{key}: live provider results were not what the column holds")
            selection.add(LIVE_PROVIDER)

        fx = [e for e in events if e.label == FORMULA_EXPRESSION]
        if fx:
            produced = fx[-1].payload["result"]  # the post-pass supersedes the placeholder pass
            if not all(_same(final[i], produced[i]) for i in live_positions()):
                raise WitnessError(f"{key}: formula results were not what the column holds")
            selection.add(FORMULA_EXPRESSION)

        kernel = [e for e in events if e.label in (COMPILED_INDEX, PYTHON_INDEX_REFERENCE)]
        if kernel:
            self._check_kernel(key, kernel, final, live_positions())
            selection.update({e.label for e in kernel})

        np_events = [e for e in events if e.label == NUMPY_SELECTION]
        if np_events:
            self._check_numpy(key, events, np_events, final, live_positions())
            selection.add(NUMPY_SELECTION)

        if kind in _NO_EVENT_KINDS:
            if events and (labels - SUBPATH_LABELS):
                raise WitnessError(f"{key}: {kind} must not select, saw {sorted(labels)}")
            selection.add(NO_SELECTION)
        if not selection:
            raise WitnessError(f"{key}: kind {kind!r} produced no observable backend event")
        evidence = STAND_IN if any(e.payload.get("evidence") == STAND_IN for e in kernel) else REAL
        return ColumnReport(
            key, kind, frozenset(selection), frozenset(labels & SUBPATH_LABELS), evidence
        )

    def _check_kernel(
        self, key: ColumnKey, kernel: list[Event], final: list[Any], live: list[int]
    ) -> None:
        consumed = [e for e in kernel if "result" in e.payload]
        choices, indexer = self.choices.get(key), self.indexers.get(key)
        if not consumed or choices is None or indexer is None:
            raise WitnessError(f"{key}: kernel selection without a recorded result or indexer")
        drawn = [v for e in consumed for v in e.payload["result"]]
        if isinstance(indexer, _kernel_select.WeightedIndexer):
            picked = np.searchsorted(indexer.thresholds, np.asarray(drawn, dtype=np.int64), "right")
            indices = [int(i) for i in picked]
        else:
            indices = [int(i) for i in drawn]
        if len(indices) != len(final):
            raise WitnessError(f"{key}: {len(indices)} kernel draws for {len(final)} rows")
        if not all(_same(final[i], choices[indices[i]]) for i in live):
            raise WitnessError(f"{key}: the column does not hold the kernel-selected values")
        if self.marking and key in self.pool_values:
            decoded = [decode_mark(final[i]) for i in live]
            if any(d is None for d in decoded) or decoded != [indices[i] for i in live]:
                raise WitnessError(f"{key}: marked indices differ from the kernel's result")

    def _check_numpy(
        self,
        key: ColumnKey,
        events: list[Event],
        np_events: list[Event],
        final: list[Any],
        live: list[int],
    ) -> None:
        rng_ev = next((e for e in np_events if e.payload.get("stage") == "default_rng"), None)
        sample_evs = [e for e in np_events if e.payload.get("stage") == "sample"]
        build = next((e for e in events if e.label == POOL_CONSTRUCTION), None)
        if rng_ev is None or not sample_evs or build is None:
            raise WitnessError(f"{key}: numpy selection needs default_rng, sample and a pool build")
        pool = build.payload["values"]
        idx = np.random.default_rng(rng_ev.payload["seed"]).integers(
            0, len(pool), size=sum(e.payload["n"] for e in sample_evs)
        )
        expected = [pool[i] for i in idx]
        if [v for e in sample_evs for v in e.payload["result"]] != expected:
            raise WitnessError(f"{key}: sampler result is not pool[default_rng(seed).integers]")
        if not all(final[i] == expected[i] for i in live):
            raise WitnessError(f"{key}: the column does not hold the numpy-selected values")
        if self.marking:
            decoded = [decode_mark(final[i]) for i in live]
            if any(d is None for d in decoded):
                raise WitnessError(f"{key}: marked selection never reached the final column")
            if decoded != [int(idx[i]) for i in live]:
                raise WitnessError(
                    f"{key}: marked indices differ from the recorded default_rng stream"
                )


def _is_null(x: Any) -> bool:
    return x is None or (isinstance(x, float) and x != x)


def _same(a: Any, b: Any) -> bool:
    if _is_null(a) or _is_null(b):
        return _is_null(a) and _is_null(b)
    return bool(a == b)


@contextmanager
def end_to_end(
    mp: pytest.MonkeyPatch,
    *,
    marking: bool = False,
    attribution: Callable[[ColumnKey], ColumnKey] | None = None,
    kernel_shift: int = 0,
    compiled: Any = "real",
    compiled_evidence: str = REAL,
) -> Iterator[EndToEndWitness]:
    w = EndToEndWitness(
        mp,
        marking=marking,
        attribution=attribution,
        kernel_shift=kernel_shift,
        compiled=compiled,
        compiled_evidence=compiled_evidence,
    )
    w.install()
    yield w


def classify_columns(
    mp: pytest.MonkeyPatch,
    run: Callable[[], dict[str, pa.Table]],
    *,
    marking: bool = False,
    **witness_kwargs: Any,
) -> dict[ColumnKey, ColumnReport]:
    """Run `run()` (a `generate_tables` call) under the witness and classify every column."""
    with end_to_end(mp, marking=marking, **witness_kwargs) as w:
        tables = run()
    return w.classify(tables)


# --------------------------------------------------------------------------------------------
# sampler-level witness: PoolSampler.sample(deterministic=True)
# --------------------------------------------------------------------------------------------

MARK_SHIFT = 7


class MarkingKernel:
    """Wraps an index kernel and shifts its result by MARK_SHIFT (mod pool_size).

    The shift makes the kernel's output DISTINGUISHABLE from the reference derivation, so a
    consumer that drops the kernel result and recomputes cannot reproduce the column. The wrapper
    delegates the actual derivation, so `evidence` records whether that was a real compiled kernel
    or a stand-in.
    """

    def __init__(
        self,
        delegate: Any,
        *,
        evidence: str,
        backend: str,
        sink: list[Event],
        shift: int = MARK_SHIFT,
    ) -> None:
        self.shift = shift
        self.delegate = delegate
        self.evidence = evidence
        self.backend = backend
        self.sink = sink

    def derive_index_batch(
        self,
        values: Any,
        *,
        mask_key: Any,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        try:
            raw = self.delegate.derive_index_batch(
                values, mask_key=mask_key, namespace=namespace, pool_size=pool_size
            )
        except GenerationError as exc:
            self.sink.append(Event(self.backend, {"declined": exc.code, "evidence": self.evidence}))
            raise
        shifted = pa.array(
            [None if v is None else (v + self.shift) % pool_size for v in raw.to_pylist()],
            pa.uint64(),
        )
        self.sink.append(
            Event(
                self.backend,
                {
                    "result": shifted.to_pylist(),
                    "unshifted": raw.to_pylist(),
                    "evidence": self.evidence,
                },
            )
        )
        return shifted


class StandInCompiledKernel:
    """Pure-Python stand-in for the compiled kernel, used ONLY to test the assertion logic where no
    companion is installed. Admits int and str Arrow arrays like the real one and declines the
    rest with the same coded error."""

    def derive_index_batch(
        self,
        values: Any,
        *,
        mask_key: Any,
        namespace: str,
        pool_size: int,
        native_threads: int | None = None,
    ) -> pa.Array:
        from decoy_engine.execution.native._index_ext import reference_index_derivation

        if not (pa.types.is_integer(values.type) or pa.types.is_string(values.type)):
            raise GenerationError(code="native_type_not_admitted", message="stand-in declines")
        return reference_index_derivation().derive_index_batch(
            values.to_pylist(), mask_key=mask_key, namespace=namespace, pool_size=pool_size
        )


@dataclass
class SamplerReport:
    label: str
    evidence: str
    events: list[Event]


class SamplerWitness:
    """Installs marking kernels on `_sampler`'s two lookup sites and records which one the
    deterministic index derivation consumed."""

    def __init__(
        self,
        mp: pytest.MonkeyPatch,
        *,
        compiled: Any | None,
        compiled_evidence: str,
        shift: int = MARK_SHIFT,
    ) -> None:
        self.mp = mp
        self.events: list[Event] = []
        self.compiled_evidence = compiled_evidence
        from decoy_engine.execution.native._index_ext import reference_index_derivation

        self._compiled = (
            MarkingKernel(
                compiled,
                evidence=compiled_evidence,
                backend=COMPILED_INDEX,
                sink=self.events,
                shift=shift,
            )
            if compiled is not None
            else None
        )
        self._reference = MarkingKernel(
            reference_index_derivation(),
            evidence=REAL,
            backend=PYTHON_INDEX_REFERENCE,
            sink=self.events,
            shift=shift,
        )
        mp.setattr(_sampler, "_compiled_index_kernel", lambda: self._compiled)
        mp.setattr(_sampler, "_reference_index_kernel", lambda: self._reference)

    def sample(self, pool: Any, source: pd.Series, namespace: str, seed: bytes) -> pd.Series:
        from decoy_engine.generation.pool._cardinality import CardinalityMode

        self.events.clear()
        return PoolSampler().sample(
            pool,
            len(source),
            mode=CardinalityMode.REUSE,
            seed=seed,
            source=source,
            namespace=namespace,
            deterministic=True,
        )

    def classify(
        self, pool: Any, output: pd.Series, events: list[Event] | None = None
    ) -> SamplerReport:
        """Prove which kernel's marked result is in `output`; raise WitnessError otherwise."""
        events = list(self.events if events is None else events)
        consumed = [e for e in events if "result" in e.payload]
        if len(consumed) != 1:
            raise WitnessError(f"expected exactly one consumed kernel result, saw {len(consumed)}")
        ev = consumed[0]
        out = output.tolist()
        marks = [decode_mark(v) for v in out]
        nonnull = [i for i, v in enumerate(out) if v is not pd.NA and v is not None]
        if any(marks[i] is None for i in nonnull):
            raise WitnessError("marked pool value never reached the output")
        if [marks[i] for i in nonnull] != ev.payload["result"]:
            raise WitnessError(
                "output indices are not the kernel's marked result (recomputed or discarded)"
            )
        return SamplerReport(ev.label, ev.payload["evidence"], events)


def marked_value_pool(size: int = 53) -> Any:
    """A `ValuePool` whose value i is `⟦i⟧v{i}` so selected indices can be read back."""
    from decoy_engine.generation.pool._value_pool import ValuePool, _freeze_array

    values = _freeze_array(np.array([mark_value(i, f"v{i}") for i in range(size)], dtype=object))
    return ValuePool(
        values=values,
        provider="f0.marked",
        locale="default",
        config_hash="",
        seed=b"\x00" * 8,
        size=size,
        build_time_ms=0.0,
        backend_type="f0",
        backend_version="f0",
        distinct_count=size,
    )


# --------------------------------------------------------------------------------------------
# composite witness: direct `derive` + modulo, bypassing `_sampler`
# --------------------------------------------------------------------------------------------


@dataclass
class CompositeReport:
    label: str
    index_kernel_calls: int
    derive_calls: int


class CompositeWitness:
    """Observes a composite `generate_bundle(deterministic=True)` run.

    Pools are replaced by marked arrays (`⟦i⟧...`) so the index each output column picked can be
    decoded, and `derive` is wrapped so the latent each row was handed is recorded. The witness then
    requires that every output column's decoded index equals the recorded latent slice modulo the
    pool length, and that the shared index kernel in `_sampler` was NEVER called (the composite path
    does not use it; F3 routing `_sampler` would not change composite output).
    """

    # Latent byte slice per marked output column (`_person.py:171`, `_name_email.py`).
    PERSON_SLICES = {"first_name": (0, 8), "last_name": (8, 16), "dob": (24, 32)}
    NAME_EMAIL_SLICES = {"first_name": (0, 8), "last_name": (8, 16)}

    def __init__(
        self,
        mp: pytest.MonkeyPatch,
        module: Any,
        cls: Any,
        *,
        pools_method: str = "_pools",
        slices: dict[str, tuple[int, int]] | None = None,
        pool_len: int = 61,
    ) -> None:
        self.SLICES = slices or self.PERSON_SLICES
        self.mp = mp
        self.derive_events: list[bytes] = []
        self.index_kernel_calls = 0
        self.pool_len = pool_len
        outer = self
        real_derive = module.derive

        def derive(*a: Any, **kw: Any) -> bytes:
            latent = real_derive(*a, **kw)
            outer.derive_events.append(latent)
            return latent

        mp.setattr(module, "derive", derive)

        def marked(label: str) -> Any:
            arr = np.empty(pool_len, dtype=object)
            for i in range(pool_len):
                arr[i] = mark_value(i, f"{label}{i}")
            return arr

        names = ("first", "last", "dob")[: len(self.SLICES)]
        arrays = tuple(marked(n) for n in names)
        mp.setattr(cls, pools_method, lambda self, spec: arrays)
        real_indices = _sampler._derive_pool_indices

        def spy_indices(*a: Any, **kw: Any) -> Any:
            outer.index_kernel_calls += 1
            return real_indices(*a, **kw)

        mp.setattr(_sampler, "_derive_pool_indices", spy_indices)

    def classify(
        self,
        bundle: dict[str, pd.Series],
        *,
        attribution: Callable[[str], str] | None = None,
        latents: list[bytes] | None = None,
    ) -> CompositeReport:
        attribution = attribution or (lambda c: c)
        latents = self.derive_events if latents is None else latents
        nonnull = [i for i, v in enumerate(bundle["first_name"].tolist()) if v is not pd.NA]
        if len(latents) != len(nonnull):
            raise WitnessError(f"{len(latents)} derive calls for {len(nonnull)} non-null rows")
        for col in self.SLICES:
            seen = [decode_mark(bundle[col].iloc[i]) for i in nonnull]
            if any(s is None for s in seen):
                raise WitnessError(f"{col}: marked pool value never reached the output")
            src = self.SLICES[attribution(col)]
            want = [int.from_bytes(lat[src[0] : src[1]], "big") % self.pool_len for lat in latents]
            if seen != want:
                raise WitnessError(f"{col}: picked indices are not the recorded derive() latents")
        if self.index_kernel_calls:
            raise WitnessError("composite path unexpectedly used the shared index kernel")
        return CompositeReport(COMPOSITE_DERIVE, self.index_kernel_calls, len(self.derive_events))
