"""Out-of-core text_mask native slice (B5): fixtures, a marking wrapper and the consumed witness.

`MarkingTextMask` wraps the shared `native_text_mask` the route calls (through
`_text_mask_native`), records every argument set and every returned array, and can inject a fault.
`forbid_old_loop` makes the per-cell `mask_cell` the pre-B5 path ran raise, so a column decided
native proves the compiled span path drove the output (the old loop never runs). Nothing here is
the code under test: the full-frame oracle is the pandas adapter, and `_text_mask_array`'s own
per-cell loop stays the Python reference the companion-absent cases fall back to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution.native import _text_redact_kernel
from decoy_engine.execution.out_of_core import _hash_run, _mask_group_c, _text_mask_native
from tests.parity._d1a_hash_data import column_seed

KEY = "out_of_core_text_mask"
UNAVAILABLE = "text_mask_extension_unavailable"

# One PII shape per detector family plus plain text, nulls, empties, repeats and non-ASCII.
SOURCE: list[str | None] = [
    "mail alice@example.com now",
    "call 415-555-0132 today",
    "ssn 123-45-6789 on file",
    "card 4111 1111 1111 1111 ok",
    "no pii here at all",
    "",
    None,
    "zip 90210 and 10001-1234",
    "Zoe mailed alice@example.com from 123-45-6789",
    "mail alice@example.com now",
    "visit 123 Main Street today",
    None,
]


def tm_seed(**config: Any) -> Any:
    """A text_mask column seed; `config` is the handler's provider_config."""
    return column_seed("text_mask", namespace=None, provider_config=tuple(config.items()))


def kernel_loads() -> bool:
    return _text_redact_kernel.load_text_redact_kernel() is not None


def hide_text_mask_companion(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the route's one-time companion-presence load answer 'absent', as on a box without it."""
    monkeypatch.setattr(_hash_run, "load_text_redact_kernel", lambda: None)


def forbid_old_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the pre-B5 per-cell `mask_cell` loop raise; a native column must never reach it."""

    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("the per-cell mask_cell loop ran for a column decided native")

    monkeypatch.setattr(_mask_group_c, "mask_cell", boom)


@dataclass(frozen=True)
class KernelCall:
    column: str
    detectors: tuple[str, ...] | None
    rows: int


class MarkingTextMask:
    """Delegates to the real `native_text_mask` and keeps what it was given and returned.

    `fail_on_call` makes the Nth call raise, to model a mid-run kernel fault."""

    def __init__(self, fail_on_call: int | None = None) -> None:
        self.real = _text_mask_native.native_text_mask
        self.fail_on_call = fail_on_call
        self.calls: list[KernelCall] = []
        self.results: list[pa.Array] = []

    def __call__(
        self, array: Any, *, mask_key: Any, column: str, detectors: Any, **kw: Any
    ) -> Any:
        self.calls.append(KernelCall(column, detectors, len(array)))
        if len(self.calls) == self.fail_on_call:
            raise RuntimeError("injected text_mask kernel fault")
        out, notices = self.real(array, mask_key=mask_key, column=column, detectors=detectors, **kw)
        self.results.append(out)
        return out, notices


def install_marking(monkeypatch: pytest.MonkeyPatch, *, fail_on_call: int | None = None) -> MarkingTextMask:
    """Route the OOC adapter's kernel call through a marker."""
    marker = MarkingTextMask(fail_on_call)
    monkeypatch.setattr(_text_mask_native, "native_text_mask", marker)
    return marker
