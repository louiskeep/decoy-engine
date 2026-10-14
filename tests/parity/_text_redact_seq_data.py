"""text_redact SEQ-native: shared fixtures and the oracle / native / Python trio runner.

Shapes reuse the D2c payload builder (`_d2c_cat_data.payload_shape`): the text_redact columns
are payload and the FK key is hashed. A masking cell runs three ways on identical inputs: the
full-frame oracle (the shipped Python handler), the SEQ route with the text_redact kernel
loaded, and the SEQ route with that kernel hidden (the Python fallback); every pair must agree
on all nine strict dimensions. The native witness is the `sequential_text_redact` evidence
entry (`rust_companion` plus `native_calls`), and the T8 spy records what the compiled
candidate call actually received.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter, _sequential_native_ops
from decoy_engine.execution._sequential_native_ops import SEQ_TEXT_REDACT_DISPOSITION_KEY
from decoy_engine.execution.native import _kernels_scalar, _text_redact_kernel
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.providers_v2 import get_default_registry
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2b_shapes import NAMESPACES, Shape

ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)
PASSTHROUGH = column_seed("passthrough", namespace=None)
UNAVAILABLE = "crypto_extension_unavailable"


def tr_seed(**config: Any) -> ColumnSeed:
    """A text_redact column seed; `config` is the handler's provider_config."""
    return column_seed("text_redact", namespace=None, provider_config=tuple(config.items()))


def hide_text_redact(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make only the text_redact span-kernel load fail (the SEQ gate and the kernel's own
    self-load), leaving the hash and index loads alone."""
    monkeypatch.setattr(_sequential_native_ops, "load_text_redact_kernel", lambda: None)
    monkeypatch.setattr(_kernels_scalar, "load_text_redact_kernel", lambda: None)


def kernel_loads() -> bool:
    return _text_redact_kernel.load_text_redact_kernel() is not None


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


def run_seq_with(shape: Shape, registry: Any) -> Any:
    srcs = shape.sources
    return PandasExecutionAdapter().run_sequential(
        shape.plan,
        lambda t: srcs[t],
        registry=registry,
        relationship_graph=shape.graph,
        namespace_registry=NAMESPACES,
    )


def run_full_with(shape: Shape, registry: Any) -> Any:
    return PandasExecutionAdapter().run(
        shape.plan,
        shape.sources,
        registry=registry,
        relationship_graph=shape.graph,
        namespace_registry=NAMESPACES,
    )


@dataclass(frozen=True)
class Trio:
    oracle: Any
    native: Any
    python: Any


def run_trio(shape: Shape, monkeypatch: pytest.MonkeyPatch) -> Trio:
    registry = get_default_registry()
    oracle = run_full_with(shape, registry)
    native = run_seq_with(shape, registry)
    with monkeypatch.context() as hidden:
        hide_text_redact(hidden)
        python = run_seq_with(shape, registry)
    return Trio(oracle, native, python)


def assert_trio_strict(trio: Trio, tables: tuple[str, ...] = ("P", "C")) -> None:
    for table in tables:
        for label, other in (("native", trio.native), ("python", trio.python)):
            report = sc.compare_strict(trio.oracle.outputs[table], other.outputs[table])
            assert report == ALL_TRUE, (table, label, report)
    for other in (trio.native, trio.python):
        assert other.warnings == trio.oracle.warnings
        assert other.row_errors == trio.oracle.row_errors


def tr_entries(result: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = result.quality_metrics.get(SEQ_TEXT_REDACT_DISPOSITION_KEY, [])
    return entries


def tr_entry(result: Any, column: str) -> dict[str, Any]:
    [entry] = [e for e in tr_entries(result) if e["column"] == column and e["path"] == "scalar"]
    return entry


def assert_native_witness(result: Any, column: str, *, calls: int = 1) -> None:
    entry = tr_entry(result, column)
    assert entry["executed_backend"] == "rust_companion", entry
    assert "reason" not in entry, entry
    assert entry["native_calls"] == calls, entry


def assert_python_ran(result: Any, column: str, reason: str) -> None:
    entry = tr_entry(result, column)
    assert entry["executed_backend"] == "python_scalar", entry
    assert entry["reason"] == reason, entry
    assert entry["native_calls"] == 0, entry


def strs(values: list[str | None]) -> pa.Array:
    return pa.array(values, pa.string())
