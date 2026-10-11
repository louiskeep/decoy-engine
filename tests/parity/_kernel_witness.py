"""Native-execution witness for the out-of-core mask seam (test-side only).

Question it answers: did a native kernel PRODUCE the masked column, or did a
legacy/fallback path supply it? Counting calls cannot answer that, because one
entrypoint can try a native path, fail, and let a fallback return the result.

Patch-where-looked-up (pytest's documented monkeypatch pattern):
`out_of_core/_mask.py` binds the four scalar entrypoints by name at import
(`from decoy_engine.kernel import hash_array, ...`), so the spy replaces the
names in `decoy_engine.execution.out_of_core._mask`. Patching
`decoy_engine.kernel.<name>` would miss every OOC call. The oracle reaches the
kernels through its own bindings, so installing the spy only around the OOC call
isolates what it observes to the OOC route.

Provenance is read from what the dispatch callable ACTUALLY returned. A backend
wrapper (`legacy()` / `native()`) tags the result object it produced; the spy
then looks the returned object up in that registry. Nothing is a preassigned
flag, so a native attempt whose result is replaced by a fallback's result is
tagged legacy. A result nobody tagged is "unknown" and satisfies neither
assertion.

Insertion boundary: an entrypoint's return is not proof the engine USED it. The spy
therefore also wraps every `mask_batch` binding and records the arrays the finished
batch actually carries (`Insertion`); `assert_native_ran` requires each native
result's buffers to BE the inserted column's buffers, so a native call whose result
is discarded in favour of an equal-valued Python array fails. Table, column and
batch-call context for each observation comes from the `mask_batch` / `mask_column`
spies. The compiled loader's own known-answer self-test never passes through a
`_mask` binding, so it is never observed.

Attribution: the scalar entrypoints get no table/column identity, so each
fixture masks ONE strategy on ONE column and the observation is matched to that
column by its input values (the concatenated inputs must equal the source
column exactly). Extra calls on other columns break that match loudly instead
of being silently attributed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.out_of_core import (
    _categorical_native,
    _hash_run,
    _mask,
    _runner,
    _stream_driver,
)
from tests.parity._strict_compare import _cell_token

ENTRYPOINTS = (
    "hash_array",
    "redact_array",
    "truncate_array",
    "passthrough_array",
    "categorical_array",
)
# Native entrypoints, spied only where `_mask` binds them. `native_hash_array` is the compiled
# hash dispatch; it is a physical entrypoint of the LOGICAL `hash_array` mask.
NATIVE_ENTRYPOINTS = ("native_hash_array",)
# The categorical adapter is spied too, but its native provenance comes from the compiled
# operator it calls (`_categorical_native.native_categorical`), not from the adapter's return:
# the adapter also answers an empty or all-null slice with no kernel call, and that answer must
# not read as a native execution.
CATEGORICAL_ADAPTER = "native_categorical_array"
_LOGICAL = {"native_hash_array": "hash_array", CATEGORICAL_ADAPTER: "categorical_array"}
# Every module that binds `mask_batch` by name (the drivers import it directly).
_BATCH_BINDINGS = (_mask, _runner, _stream_driver)

NATIVE = "native"
LEGACY = "legacy"
UNKNOWN = "unknown"
# An adapter call that made no compiled-kernel call (empty or all-null slice).
IDLE = "native_idle"


def force_companion_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the out-of-core route's one-time companion loads fail, as on a box without it.

    The legacy-only controls need this to hold on a machine that HAS the companion: without
    it the admitted hash and categorical columns run native and the legacy-only claim is
    false by design."""

    def unavailable() -> None:
        raise CryptoExtensionUnavailableError("companion hidden for a legacy-path-only control")

    monkeypatch.setattr(_hash_run, "load_compiled_crypto_kernel", unavailable)
    monkeypatch.setattr(_hash_run, "load_compiled_index_kernel", unavailable)


@dataclass(frozen=True)
class ExpectedMask:
    """One column a fixture expects to be masked, and by which entrypoint."""

    table: str
    column: str
    entrypoint: str


@dataclass
class Observation:
    entrypoint: str
    values: pa.Array
    result: pa.Array
    backend: str
    table: str | None = None
    column: str | None = None
    call: int | None = None

    @property
    def logical(self) -> str:
        return _LOGICAL.get(self.entrypoint, self.entrypoint)


@dataclass(frozen=True)
class Insertion:
    """One column of a finished `mask_batch` result: what the batch actually carries."""

    table: str
    column: str
    call: int
    array: pa.Array


def _same_buffers(a: pa.Array, b: pa.Array) -> bool:
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
    return values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values


def _tokens(arrays: list[pa.Array]) -> list[tuple[object, ...]]:
    return [_cell_token(v) for a in arrays for v in a.to_pylist()]


@dataclass
class KernelWitness:
    observations: list[Observation] = field(default_factory=list)
    insertions: list[Insertion] = field(default_factory=list)
    native_attempts: int = 0
    _provenance: dict[int, tuple[str, Any]] = field(default_factory=dict)
    _table: str | None = None
    _column: str | None = None
    _calls: dict[str, int] = field(default_factory=dict)

    def _tag(self, backend: str, fn: Callable[..., Any]) -> Callable[..., Any]:
        def tagged(*args: Any, **kwargs: Any) -> Any:
            result = fn(*args, **kwargs)
            # Hold the object so its id cannot be recycled for another result.
            self._provenance[id(result)] = (backend, result)
            return result

        return tagged

    def legacy(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a legacy/fallback backend: results it returns are tagged legacy."""
        return self._tag(LEGACY, fn)

    def native(self, fn: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a native backend: an attempt is counted even if `fn` raises, and
        a result it returns is tagged native."""

        def attempted(*args: Any, **kwargs: Any) -> Any:
            self.native_attempts += 1
            return fn(*args, **kwargs)

        return self._tag(NATIVE, attempted)

    def _spy(self, entrypoint: str, dispatch: Callable[..., Any]) -> Callable[..., Any]:
        def spy(values: Any, *args: Any, **kwargs: Any) -> Any:
            attempts = self.native_attempts
            result = dispatch(values, *args, **kwargs)
            backend = self._provenance.get(id(result), (UNKNOWN, None))[0]
            if (
                entrypoint == CATEGORICAL_ADAPTER
                and backend == UNKNOWN
                and self.native_attempts == attempts
            ):
                backend = IDLE
            call = self._calls.get(self._table) if self._table is not None else None
            self.observations.append(
                Observation(
                    entrypoint,
                    _combine(values),
                    result,
                    backend,
                    self._table,
                    self._column,
                    call,
                )
            )
            return result

        return spy

    def _column_spy(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(*args: Any, **kwargs: Any) -> Any:
            previous, self._column = self._column, kwargs.get("column")
            try:
                return real(*args, **kwargs)
            finally:
                self._column = previous

        return spy

    def _batch_spy(self, real: Callable[..., Any]) -> Callable[..., Any]:
        def spy(plan: Any, table_name: str, batch: Any, *args: Any, **kwargs: Any) -> Any:
            call = self._calls.get(table_name, -1) + 1
            self._calls[table_name] = call
            previous, self._table = self._table, table_name
            try:
                out = real(plan, table_name, batch, *args, **kwargs)
            finally:
                self._table = previous
            for i, name in enumerate(out.schema.names):
                self.insertions.append(Insertion(table_name, name, call, out.column(i)))
            return out

        return spy

    @contextmanager
    def observe(
        self, dispatch: Mapping[str, Callable[..., Any]] | None = None
    ) -> Iterator[KernelWitness]:
        """Install the spies on the `_mask` bindings for the duration of the block.

        `dispatch` overrides the backend for an entrypoint (test doubles, or a
        future slice's native kernel). The default is the real kernel, wrapped as
        legacy: D0 has no native FK-payload entrypoint.
        """
        overrides = dispatch or {}
        with pytest.MonkeyPatch.context() as mp:
            for name in ENTRYPOINTS:
                backend_fn = overrides.get(name) or self.legacy(getattr(_mask, name))
                mp.setattr(_mask, name, self._spy(name, backend_fn))
            for name in NATIVE_ENTRYPOINTS:
                if hasattr(_mask, name):
                    backend_fn = overrides.get(name) or self.native(getattr(_mask, name))
                    mp.setattr(_mask, name, self._spy(name, backend_fn))
            if hasattr(_mask, CATEGORICAL_ADAPTER):
                mp.setattr(
                    _categorical_native,
                    "native_categorical",
                    self.native(_categorical_native.native_categorical),
                )
                adapter = overrides.get(CATEGORICAL_ADAPTER) or getattr(_mask, CATEGORICAL_ADAPTER)
                mp.setattr(_mask, CATEGORICAL_ADAPTER, self._spy(CATEGORICAL_ADAPTER, adapter))
            mp.setattr(_mask, "mask_column", self._column_spy(_mask.mask_column))
            # Wrap what each binding holds NOW, so a test double installed before `observe`
            # sits inside the spy and its substitution is what gets recorded.
            for module in _BATCH_BINDINGS:
                mp.setattr(module, "mask_batch", self._batch_spy(module.mask_batch))
            yield self

    def _matched(
        self, expected: ExpectedMask, sources: Mapping[str, pa.Table]
    ) -> list[Observation]:
        """Observations of the expected entrypoint, required to cover exactly the
        expected source column. Fails on zero observations (missing observation)
        and on inputs that do not add up to that column (misattribution)."""
        # An observation made inside `mask_batch` knows its table and column; one made elsewhere
        # (the whole-table zero-row path) does not, and is matched by its values alone.
        obs = [
            o
            for o in self.observations
            if o.logical == expected.entrypoint
            and (o.table is None or (o.table, o.column) == (expected.table, expected.column))
        ]
        if not obs:
            raise AssertionError(
                f"no {expected.entrypoint} observation for "
                f"{expected.table}.{expected.column}: the kernel was never observed"
            )
        source = _combine(sources[expected.table].column(expected.column))
        if _tokens([o.values for o in obs]) != _tokens([source]):
            raise AssertionError(
                f"{expected.entrypoint} inputs do not add up to "
                f"{expected.table}.{expected.column}; observations are misattributed"
            )
        return obs

    def assert_covered(self, sources: Mapping[str, pa.Table], expected: list[ExpectedMask]) -> None:
        """Each expected-masked column was observed at least once."""
        for e in expected:
            self._matched(e, sources)

    def assert_legacy_path_only(
        self, sources: Mapping[str, pa.Table], expected: list[ExpectedMask]
    ) -> None:
        """Covered, every observation legacy, and no native attempt at all."""
        for e in expected:
            backends = {o.backend for o in self._matched(e, sources)}
            if backends != {LEGACY}:
                raise AssertionError(f"{e}: backends observed {sorted(backends)}, want legacy")
        if self.native_attempts:
            raise AssertionError(f"{self.native_attempts} native attempt(s) in a legacy-only run")

    def assert_native_ran(
        self,
        sources: Mapping[str, pa.Table],
        outputs: Mapping[str, pa.Table],
        expected: list[ExpectedMask],
    ) -> None:
        """Covered, every observation native-tagged, and the native results ARE the
        output column (consumed), so a fallback that supplied the output fails."""
        for e in expected:
            obs = self._matched(e, sources)
            backends = {o.backend for o in obs}
            # An idle adapter call (empty or all-null slice) is part of a native column, but a
            # column whose every call was idle proves nothing, so one real kernel result is required.
            if NATIVE not in backends or not backends <= {NATIVE, IDLE}:
                raise AssertionError(f"{e}: backends observed {sorted(backends)}, want native")
            self._assert_inserted(e, obs)
            produced = _tokens([o.result for o in obs])
            emitted = _tokens([_combine(outputs[e.table].column(e.column))])
            if produced != emitted:
                raise AssertionError(f"{e}: native result was not the masked output column")

    def _assert_inserted(self, expected: ExpectedMask, obs: list[Observation]) -> None:
        """Each native result with batch context is the very array the batch carries."""
        for o in obs:
            if o.table is None or o.call is None:
                continue  # called outside `mask_batch`; the token check below still applies
            inserted = [
                i
                for i in self.insertions
                if (i.table, i.column, i.call) == (o.table, o.column, o.call)
            ]
            if not inserted:
                raise AssertionError(f"{expected}: native result was never inserted into a batch")
            if not any(_same_buffers(o.result, i.array) for i in inserted):
                raise AssertionError(
                    f"{expected}: native result was discarded; the batch carries a different array"
                )
