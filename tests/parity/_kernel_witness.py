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

from decoy_engine.execution.out_of_core import _mask
from tests.parity._strict_compare import _cell_token

ENTRYPOINTS = ("hash_array", "redact_array", "truncate_array", "passthrough_array")

NATIVE = "native"
LEGACY = "legacy"
UNKNOWN = "unknown"


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


def _combine(values: pa.Array | pa.ChunkedArray) -> pa.Array:
    return values.combine_chunks() if isinstance(values, pa.ChunkedArray) else values


def _tokens(arrays: list[pa.Array]) -> list[tuple[object, ...]]:
    return [_cell_token(v) for a in arrays for v in a.to_pylist()]


@dataclass
class KernelWitness:
    observations: list[Observation] = field(default_factory=list)
    native_attempts: int = 0
    _provenance: dict[int, tuple[str, Any]] = field(default_factory=dict)

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
            result = dispatch(values, *args, **kwargs)
            backend = self._provenance.get(id(result), (UNKNOWN, None))[0]
            self.observations.append(Observation(entrypoint, _combine(values), result, backend))
            return result

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
            yield self

    def _matched(
        self, expected: ExpectedMask, sources: Mapping[str, pa.Table]
    ) -> list[Observation]:
        """Observations of the expected entrypoint, required to cover exactly the
        expected source column. Fails on zero observations (missing observation)
        and on inputs that do not add up to that column (misattribution)."""
        obs = [o for o in self.observations if o.entrypoint == expected.entrypoint]
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
            if backends != {NATIVE}:
                raise AssertionError(f"{e}: backends observed {sorted(backends)}, want native")
            produced = _tokens([o.result for o in obs])
            emitted = _tokens([_combine(outputs[e.table].column(e.column))])
            if produced != emitted:
                raise AssertionError(f"{e}: native result was not the masked output column")
