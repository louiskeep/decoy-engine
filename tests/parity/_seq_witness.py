"""Test-side proof that a native kernel PRODUCED a SEQ hash column (D2a).

The out-of-core witness (`_kernel_witness.py`) requires the inserted Arrow buffers to BE the
kernel's result buffers. SEQ cannot satisfy that: it hands the kernel a rebuilt Arrow array,
takes a Python list back, assigns it into a pandas column and rebuilds the table with
`pa.Table.from_pandas`, so every emitted buffer is new. Value equality alone is no substitute,
because a run that discards the kernel result and recomputes the same tokens in Python would
pass it.

So the proof is causal. `MarkingKernel` is a stand-in that returns a DISTINGUISHABLE token (the
Python token with a prefix). A column counts as natively consumed only when the emitted table
carries those marked values and the expected Arrow type. A discarded native result leaves
plain Python tokens in the table and fails the check, which `test_discarded_native_result_*`
demonstrates against a deliberately broken route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution._strategies import _hash
from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.kernel import hash_array

MARK = "native:"


@dataclass
class Call:
    rows: int
    arrow_type: pa.DataType
    namespace: str
    truncate: int | None


@dataclass
class MarkingKernel:
    """A `derive_batch` stand-in whose output differs from the Python hash on every row."""

    fail_on_call: int | None = None
    calls: list[Call] = field(default_factory=list)

    def derive_batch(
        self,
        values: Any,
        *,
        mask_key: bytes,
        namespace: str,
        truncate: int | None,
        native_threads: int | None = None,
    ) -> pa.Array:
        self.calls.append(Call(len(values), values.type, namespace, truncate))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("injected kernel fault")
        python = hash_array(values, seed=mask_key, namespace=namespace, truncate=truncate)
        return pa.array([None if v is None else MARK + v for v in python.to_pylist()], pa.string())


def install(monkeypatch: pytest.MonkeyPatch, kernel: Any) -> Any:
    """Make the SEQ route's one-time companion load return `kernel`."""
    monkeypatch.setattr(_sequential_native, "load_compiled_crypto_kernel", lambda: kernel)
    return kernel


def hide_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the SEQ route's companion load fail, as on a box without it."""

    def unavailable() -> None:
        raise CryptoExtensionUnavailableError("companion hidden for a Python-only control")

    monkeypatch.setattr(_sequential_native, "load_compiled_crypto_kernel", unavailable)


def spy_python_hash(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Row counts of every call that reaches the Python hash handler's kernel."""
    rows: list[int] = []
    real = _hash.hash_array

    def spy(values: Any, **kwargs: Any) -> Any:
        rows.append(len(values))
        return real(values, **kwargs)

    monkeypatch.setattr(_hash, "hash_array", spy)
    return rows


def marked(python_tokens: list[str | None]) -> list[str | None]:
    return [None if t is None else MARK + t for t in python_tokens]


def assert_native_consumed(table: pa.Table, column: str, python_tokens: list[str | None]) -> None:
    """The emitted column holds the kernel's DISTINGUISHABLE output, with the handler's type."""
    assert table.schema.field(column).type == pa.string(), column
    assert table.column(column).to_pylist() == marked(python_tokens), column


def assert_python_consumed(table: pa.Table, column: str, python_tokens: list[str | None]) -> None:
    """The emitted column holds plain Python tokens and no marked value."""
    emitted = table.column(column).to_pylist()
    assert emitted == python_tokens, column
    assert not any(isinstance(v, str) and v.startswith(MARK) for v in emitted), column
