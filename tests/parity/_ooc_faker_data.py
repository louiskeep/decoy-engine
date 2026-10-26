"""Out-of-core deterministic-REUSE faker native slice (B4): fixtures, a marking
wrapper over the compiled selection, and the consumed-output witness.

`MarkingFaker` wraps the shared `sample_faker_array` the route calls and records
every result array (and can inject a mid-run fault). The full-frame oracle is the
pandas adapter (`PandasExecutionAdapter.run`); nothing here is the code under
test. There is no Python faker reference to forbid on this route: faker runs only
through the compiled (or KAT-equal reference) index kernel the pool context feeds.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
from decoy_engine.execution.out_of_core import (
    _faker_native,
    _hash_run,
    _mask,
    _runner,
    _stream_driver,
)
from decoy_engine.plan._types import ColumnSeed
from tests.parity._kernel_witness import _same_buffers

FAKER_KEY = "out_of_core_faker"
NS_F = "ns_faker"

# Repeated values (determinism across repeats is visible), nulls, unicode, empties.
SOURCE: list[str | None] = [
    "alice",
    "Bob",
    "alice",
    "",
    "héllo",
    None,
    "日本語",
    "Bob",
    None,
    "\U0001f600",
]


def faker_seed(
    provider: str = "person_first_name",
    *,
    namespace: str | None = NS_F,
    pool_size: int | None = 64,
    deterministic: bool = True,
    cardinality_mode: str = "reuse",
    **cfg: Any,
) -> ColumnSeed:
    """A faker column seed. Defaults to the admitted deterministic-REUSE person-name shape."""
    return ColumnSeed(
        namespace=namespace,
        strategy="faker",
        provider=provider,
        backend_type="faker",
        backend_version="v",
        cardinality_mode=cardinality_mode,
        deterministic=deterministic,
        provider_config=tuple(cfg.items()),
        coherent_with=(),
        pool_size=pool_size,
    )


def kernel_loads() -> bool:
    try:
        load_compiled_index_kernel()
        return True
    except CryptoExtensionUnavailableError:
        return False


def hide_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the route's index-kernel load answer 'absent', so faker runs on the
    pure-Python reference index kernel (byte-identical by INDEX_KAT)."""

    def unavailable() -> Any:
        raise CryptoExtensionUnavailableError("index kernel hidden for the test")

    monkeypatch.setattr(_hash_run, "load_compiled_index_kernel", unavailable)


class MarkingFaker:
    """Delegates to the real `sample_faker_array` and keeps what it returned.

    `fail_on_call` makes the Nth call raise, to model a mid-run kernel fault."""

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.real = _faker_native.sample_faker_array
        self.fail_on_call = fail_on_call
        self.calls = 0
        self.results: list[pa.Array] = []

    def __call__(
        self,
        source: Any,
        *,
        pool: Any,
        namespace: str,
        mask_key: bytes | None,
        index_kernel: Any,
        native_threads: int | None,
    ) -> Any:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("injected faker kernel fault")
        out = self.real(
            source,
            pool=pool,
            namespace=namespace,
            mask_key=mask_key,
            index_kernel=index_kernel,
            native_threads=native_threads,
        )
        self.results.append(out)
        return out


def install_marking(
    monkeypatch: pytest.MonkeyPatch, *, fail_on_call: int | None = None
) -> MarkingFaker:
    marker = MarkingFaker(fail_on_call)
    monkeypatch.setattr(_faker_native, "sample_faker_array", marker)
    return marker


class FakerConsumedWitness:
    """Records, per finished `mask_batch`, the array the faker column ended up carrying."""

    def __init__(self, column: str = "a") -> None:
        self.column = column
        self.inserted: list[pa.Array] = []

    @contextmanager
    def observe(self) -> Iterator[FakerConsumedWitness]:
        with pytest.MonkeyPatch.context() as mp:
            for module in (_mask, _runner, _stream_driver):
                mp.setattr(module, "mask_batch", self._spy(module.mask_batch))
            yield self

    def _spy(self, real: Any) -> Any:
        def spy(plan: Any, table_name: str, batch: Any, *args: Any, **kwargs: Any) -> Any:
            out = real(plan, table_name, batch, *args, **kwargs)
            idx = out.schema.get_field_index(self.column)
            if table_name == "t" and idx >= 0:
                self.inserted.append(out.column(idx))
            return out

        return spy

    def assert_consumed(self, kernel: MarkingFaker) -> None:
        """Every non-empty kernel result IS an inserted column, and at least one ran."""
        populated = [r for r in kernel.results if len(r) > 0]
        if not populated:
            raise AssertionError("no non-empty native faker result: nothing proves the kernel ran")
        for result in populated:
            if not any(_same_buffers(result, ins) for ins in self.inserted):
                raise AssertionError(
                    "a native faker result was discarded; the batch carries other data"
                )
