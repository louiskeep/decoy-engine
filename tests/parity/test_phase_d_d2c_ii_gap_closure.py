"""Phase D / D2c-ii gap closure: three behaviors the mutation run showed no test pinned.

1. The thread budget reaches the loader and the kernel call of every descriptor.
2. A one-row native column is reported as native, not as "no rows".
3. A categorical source holding NaN and None (object dtype) masks natively as nulls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import _sequential_native
from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._sequential_native import SeqNativeRun
from decoy_engine.execution._sequential_native_ops import FpeCall
from decoy_engine.execution.native._crypto_ext import FpeConfig
from tests.parity._d1a_hash_data import JOB_SEED, column_seed
from tests.parity._d2b_shapes import run_seq, strs
from tests.parity._d2c_cat_data import CATS, CatMarkingKernel, cat_seed, install_cat, payload_shape
from tests.parity._d2c_fpe_data import FpeMarkingKernel, fpe_seed
from tests.parity._seq_witness import MarkingKernel


def _ctx(table: str = "t") -> StrategyContext:
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", table)
    object.__setattr__(ctx, "nested_outer_column", "")
    return ctx


@dataclass
class _ThreadSpy:
    """Records the `native_threads` each loader and kernel call receives."""

    loads: list[int | None] = field(default_factory=list)
    calls: list[int | None] = field(default_factory=list)


class _CatKernel(CatMarkingKernel):
    def __init__(self, spy: _ThreadSpy) -> None:
        super().__init__()
        self.spy = spy

    def derive_index_batch(self, values: Any, **kwargs: Any) -> pa.Array:
        self.spy.calls.append(kwargs.get("native_threads", "dropped"))
        return super().derive_index_batch(values, **kwargs)


class _HashKernel(MarkingKernel):
    def __init__(self, spy: _ThreadSpy) -> None:
        super().__init__()
        self.spy = spy

    def derive_batch(self, values: Any, **kwargs: Any) -> pa.Array:
        self.spy.calls.append(kwargs.get("native_threads", "dropped"))
        return super().derive_batch(values, **kwargs)


class _FpeKernel(FpeMarkingKernel):
    def __init__(self, spy: _ThreadSpy) -> None:
        super().__init__()
        self.spy = spy

    def run(self, values: Any, **kwargs: Any) -> Any:
        self.spy.calls.append(kwargs.get("native_threads", "dropped"))
        return super().run(values, **kwargs)


def _decide(run: SeqNativeRun, strategy: str, seed: Any, col: str = "x") -> None:
    run._decisions[("t", col)] = run._decide(
        _sequential_native._OPS[strategy], "t", col, pa.string(), seed
    )


def test_the_thread_budget_reaches_every_loader_and_kernel_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spies = {name: _ThreadSpy() for name in ("categorical", "hash", "fpe")}
    kernels = {
        "categorical": _CatKernel(spies["categorical"]),
        "hash": _HashKernel(spies["hash"]),
        "fpe": _FpeKernel(spies["fpe"]),
    }

    def loader(name: str, target: str) -> None:
        def load(*_args: Any) -> Any:
            return kernels[name]

        monkeypatch.setattr(_sequential_native, target, load)

    loader("categorical", "load_compiled_index_kernel")
    loader("hash", "load_compiled_crypto_kernel")
    loader("fpe", "load_compiled_fpe_kernel")

    run = SeqNativeRun(native_threads=3)
    ctx = _ctx()
    _decide(run, "categorical", cat_seed())
    _decide(run, "hash", column_seed(namespace="ns_h"), "h")
    _decide(run, "fpe", fpe_seed(), "f")
    # The loaders receive the budget: the bound kernel records it.
    assert run._bound["categorical"].native_threads == 3
    assert run._bound["hash"].native_threads == 3
    assert run._bound["fpe"].native_threads == 3

    df = pd.DataFrame({"x": ["a", "b"], "h": ["u", "v"], "f": ["1234567890", "0987654321"]})
    assert (
        run.categorical_column(
            ctx, df, "x", namespace="n", mask_key=JOB_SEED, categories=list(CATS), cdf=None
        )
        is not None
    )
    assert (
        run.hash_column(ctx, df, "h", namespace="n", mask_key=JOB_SEED, truncate=None) is not None
    )
    call = FpeCall("n", JOB_SEED, FpeConfig(), "f")
    assert run.native_column(ctx, df, "f", "fpe", call) is not None
    assert {k: s.calls for k, s in spies.items()} == {
        "categorical": [3],
        "hash": [3],
        "fpe": [3],
    }


def test_a_single_row_native_column_is_reported_as_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_cat(monkeypatch, CatMarkingKernel())
    result = run_seq(payload_shape({"c": (strs(["alice"]), cat_seed())}))
    [entry] = result.quality_metrics["sequential_categorical"]
    assert entry["executed_backend"] == "rust_companion"
    assert "reason" not in entry


def test_nan_and_none_in_an_object_source_mask_natively_as_nulls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel = install_cat(monkeypatch, CatMarkingKernel())
    run = SeqNativeRun()
    _decide(run, "categorical", cat_seed())
    df = pd.DataFrame({"x": pd.Series(["a", float("nan"), None, "b"], dtype=object)})
    out = run.categorical_column(
        _ctx(), df, "x", namespace="n", mask_key=JOB_SEED, categories=list(CATS), cdf=None
    )
    assert out == [CATS[0], None, None, CATS[0]]
    assert [c.rows for c in kernel.calls] == [4]
