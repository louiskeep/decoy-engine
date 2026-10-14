"""Out-of-core text_redact native slice: fixtures, a marking wrapper and the consumed-output witness.

`MarkingTextRedact` wraps the shared `native_text_redact` the route calls, records every
argument set and every result array, and can inject a fault or a bad result shape. The
companion-presence gate is stubbed with `install_text_redact_gate`, so the routing suite runs on
any box (without the companion `native_text_redact` runs its own literal Python path, which is
the oracle's bytes); the real compiled kernel is exercised in `test_ooc_text_redact_native_real`.
Nothing here is the code under test: `text_redact_array` is the Python reference the route is
compared with, and the full-frame oracle is the pandas adapter.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native import _text_redact_kernel
from decoy_engine.execution.out_of_core import (
    _hash_run,
    _mask,
    _runner,
    _stream_driver,
    _text_redact_native,
)
from tests.parity._d1a_hash_data import column_seed
from tests.parity._kernel_witness import _same_buffers

KEY = "out_of_core_text_redact"
UNAVAILABLE = "text_redact_extension_unavailable"

# One PII shape per detector family plus plain text, nulls, empties and repeats.
#   Rust-eligible: email, us_phone, pan, iban, ipv4, url.   Lookaround (replayed in Python by the
#   kernel): ssn, us_zip, street_address.   Non-ASCII cells take the per-cell Python path.
SOURCE = [
    "mail alice@example.com now",
    "call 415-555-0132 today",
    "card 4111 1111 1111 1111 ok",
    "iban GB82WEST12345698765432 x",
    "host 192.168.1.10 up",
    "see https://example.com/a?b=1 end",
    "ssn 123-45-6789 on file",
    "zip 90210 and 10001-1234",
    "at 123 Main Street today",
    "no pii here at all",
    "",
    None,
    "Zoë mailed alice@example.com from 123-45-6789",
    "mail alice@example.com now",
    "日本語 bob@example.org 415-555-0199",
    None,
]
# Every cell above holds something at least one detector catches, except these.
CLEAN = {"no pii here at all", "", None}


def tr_seed(**config: Any) -> Any:
    """A text_redact column seed; `config` is the handler's provider_config."""
    return column_seed("text_redact", namespace=None, provider_config=tuple(config.items()))


def kernel_loads() -> bool:
    return _text_redact_kernel.load_text_redact_kernel() is not None


def install_text_redact_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the route's one-time companion-presence load answer 'present' on any box."""
    monkeypatch.setattr(_hash_run, "load_text_redact_kernel", lambda: object())


def hide_text_redact_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the presence load answer 'absent', as on a box without the companion."""
    monkeypatch.setattr(_hash_run, "load_text_redact_kernel", lambda: None)


@dataclass(frozen=True)
class KernelCall:
    detectors: tuple[str, ...] | None
    token: Any
    label_token: Any
    rows: int


class MarkingTextRedact:
    """Delegates to the real `native_text_redact` and keeps what it was given and returned.

    `fail_on_call` makes the Nth call raise, to model a mid-run kernel fault; `bad_result`
    replaces the returned array with a malformed one."""

    def __init__(self, fail_on_call: int | None = None, bad_result: Any = None) -> None:
        self.real = _text_redact_native.native_text_redact
        self.fail_on_call = fail_on_call
        self.bad_result = bad_result
        self.calls: list[KernelCall] = []
        self.results: list[pa.Array] = []

    def __call__(
        self,
        array: Any,
        *,
        detectors: tuple[str, ...] | None,
        token: str,
        label_token: bool,
    ) -> Any:
        self.calls.append(KernelCall(detectors, token, label_token, len(array)))
        if len(self.calls) == self.fail_on_call:
            raise RuntimeError("injected kernel fault")
        if self.bad_result is not None:
            return self.bad_result(array)
        out = self.real(array, detectors=detectors, token=token, label_token=label_token)
        self.results.append(out)
        return out


def install_marking(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate: bool = True,
    fail_on_call: int | None = None,
    bad_result: Any = None,
) -> MarkingTextRedact:
    """Route the OOC adapter's kernel call through a marker (and, by default, open the gate)."""
    marker = MarkingTextRedact(fail_on_call, bad_result)
    if gate:
        install_text_redact_gate(monkeypatch)
    monkeypatch.setattr(_text_redact_native, "native_text_redact", marker)
    return marker


def forbid_python_text_redact(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_: Any, **__: Any) -> None:
        raise AssertionError("the Python text_redact loop ran for a column decided native")

    monkeypatch.setattr(_mask, "text_redact_array", forbidden)


class CandidateSpy:
    """Records every compiled `text_redact_candidates` call: (cells, detector ids sent)."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[str, ...]]] = []


def spy_candidates(monkeypatch: pytest.MonkeyPatch) -> CandidateSpy:
    spy = CandidateSpy()
    original = _text_redact_kernel._CompiledTextRedactKernel.text_redact_candidates

    def recording(self: Any, values: Any, detector_ids: Any, catalog_version: int) -> Any:
        spy.calls.append((len(values), tuple(detector_ids)))
        return original(self, values, detector_ids, catalog_version)

    monkeypatch.setattr(
        _text_redact_kernel._CompiledTextRedactKernel, "text_redact_candidates", recording
    )
    return spy


class TextRedactConsumedWitness:
    """Records, per finished `mask_batch`, the array the text_redact column ended up carrying."""

    def __init__(self, column: str = "a") -> None:
        self.column = column
        self.inserted: list[pa.Array] = []

    @contextmanager
    def observe(self) -> Iterator[TextRedactConsumedWitness]:
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

    def assert_consumed(self, kernel: MarkingTextRedact) -> None:
        """Every non-empty kernel result IS an inserted column, and at least one ran."""
        populated = [r for r in kernel.results if len(r) > 0]
        if not populated:
            raise AssertionError("no non-empty native result: nothing proves the kernel was used")
        for result in populated:
            if not any(_same_buffers(result, ins) for ins in self.inserted):
                raise AssertionError(
                    "a native text_redact result was discarded; the batch carries other data"
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
