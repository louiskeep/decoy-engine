"""Out-of-core fpe native slice: fixtures, a marking kernel and the consumed-output witness.

`MarkingFpeKernel` wraps either the real compiled kernel or the pure-Python reference and
records every result array it hands back. `FpeConsumedWitness` spies the `mask_batch` bindings
and proves each non-empty kernel result is the very array a finished batch carries (same
buffers), so a kernel call whose result is replaced by an equal-valued Python array fails.
Nothing here is the code under test: `fpe_array` is the Python reference the route is compared
with, and the full-frame oracle is the pandas adapter.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    FpeBatchResult,
    FpeConfig,
)
from decoy_engine.execution.native._crypto_reference import reference_fpe
from decoy_engine.execution.native._fpe_ext import load_compiled_fpe_kernel
from decoy_engine.execution.out_of_core import _hash_run, _mask, _runner, _stream_driver
from tests.parity._d1a_hash_data import column_seed
from tests.parity._kernel_witness import _same_buffers

NS = "ns_fpe"
KEY = "out_of_core_fpe"
DIGITS = (("charset", "digits"),)

# Mixed shapes: separators, a Luhn-valid card, empty string, nulls, repeated values.
SOURCE = [
    "123-45-6789",
    "4111111111111111",
    "",
    None,
    "987654321",
    "123-45-6789",
    "000-00-0001",
    "5500005555555559",
    None,
    "12 34 56 78",
    "99999999999999999999",
]
# Under `digits`: one digit is below the FF1 minimum domain, a letter-only value has no digits.
BAD_DOMAIN = "9"
BAD_VALUE = "abc"


def fpe_seed(*, namespace: str | None = NS, config: tuple[tuple[str, Any], ...] = DIGITS) -> Any:
    return column_seed("fpe", namespace=namespace, provider_config=config)


def companion_loads() -> bool:
    try:
        load_compiled_fpe_kernel()
    except CryptoExtensionUnavailableError:
        return False
    return True


class MarkingFpeKernel:
    """Delegates `run` to a real or reference kernel and keeps each result array it returns.

    `fail_on_call` makes the Nth call raise, to model a mid-run kernel fault."""

    def __init__(self, inner: Any = None, fail_on_call: int | None = None) -> None:
        self.inner: Any = inner if inner is not None else reference_fpe()
        self.fail_on_call = fail_on_call
        self.calls = 0
        self.results: list[pa.Array] = []

    def run(
        self,
        values: Any,
        *,
        mask_key: bytes | None,
        namespace: str,
        tweak_column: str,
        config: FpeConfig,
        forward: bool,
        native_threads: int | None = None,
    ) -> FpeBatchResult:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise RuntimeError("injected kernel fault")
        if hasattr(self.inner, "run"):
            result = self.inner.run(
                values,
                mask_key=mask_key,
                namespace=namespace,
                tweak_column=tweak_column,
                config=config,
                forward=forward,
                native_threads=native_threads,
            )
        else:
            result = self.inner._run(
                values,
                mask_key=mask_key,
                namespace=namespace,
                tweak_column=tweak_column,
                config=config,
                forward=forward,
            )
        self.results.append(result.values)
        return result


def install_fpe_kernel(
    monkeypatch: pytest.MonkeyPatch, kernel: MarkingFpeKernel | None = None
) -> MarkingFpeKernel:
    """Make the route's one-time fpe load return `kernel` (default: the reference double)."""
    installed = kernel or MarkingFpeKernel()
    monkeypatch.setattr(_hash_run, "load_compiled_fpe_kernel", lambda: installed)
    return installed


def install_real_fpe_kernel(
    monkeypatch: pytest.MonkeyPatch, fail_on_call: int | None = None
) -> MarkingFpeKernel:
    """Wrap the real compiled kernel with the marker (needs the abi-3 companion)."""
    return install_fpe_kernel(
        monkeypatch, MarkingFpeKernel(load_compiled_fpe_kernel(), fail_on_call)
    )


def hide_fpe_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the fpe kernel load fail, as on a box without the companion."""

    def unavailable() -> None:
        raise CryptoExtensionUnavailableError("fpe companion hidden for a Python-path control")

    monkeypatch.setattr(_hash_run, "load_compiled_fpe_kernel", unavailable)


def forbid_python_fpe(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("the Python fpe loop ran for a column decided native")

    monkeypatch.setattr(_mask, "fpe_array", forbidden)


class FpeConsumedWitness:
    """Records, per finished `mask_batch`, the array each fpe column ended up carrying."""

    def __init__(self, column: str = "a") -> None:
        self.column = column
        self.inserted: list[pa.Array] = []

    @contextmanager
    def observe(self) -> Iterator[FpeConsumedWitness]:
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

    def assert_consumed(self, kernel: MarkingFpeKernel) -> None:
        """Every non-empty kernel result IS an inserted column, and at least one ran."""
        populated = [r for r in kernel.results if len(r) > 0]
        if not populated:
            raise AssertionError("no non-empty native result: nothing proves the kernel was used")
        for result in populated:
            if not any(_same_buffers(result, ins) for ins in self.inserted):
                raise AssertionError(
                    "a native fpe result was discarded; the batch carries other data"
                )


def replace_with_equal_python_array(monkeypatch: pytest.MonkeyPatch, column: str = "a") -> None:
    """Below the witness: swap the masked column for an equal-valued fresh Python array."""
    real = _mask.mask_batch

    def python_equal(*args: Any, **kwargs: Any) -> pa.RecordBatch:
        out = real(*args, **kwargs)
        idx = out.schema.get_field_index(column)
        if idx < 0:
            return out
        cols = list(out.columns)
        cols[idx] = pa.array(cols[idx].to_pylist(), pa.string())
        return pa.record_batch(cols, schema=out.schema)

    for module in (_mask, _runner, _stream_driver):
        monkeypatch.setattr(module, "mask_batch", python_equal)
