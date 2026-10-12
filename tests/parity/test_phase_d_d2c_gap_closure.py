"""Phase D / D2c-i: pins for SEQ-native behavior the pre-merge mutation run showed untested.

Each test names the mutant class it closes. They exercise `SeqNativeRun` directly where a
full run cannot reach the case (an orphan key of a type the hash kernel declines never
arrives from a real FK job, because the FK-safe conversion types orphan keys itself).
"""

from __future__ import annotations

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution._adapter import StrategyContext
from decoy_engine.execution._errors import ExecutionError, StrategyError
from decoy_engine.execution._sequential_native import (
    INHERITED,
    MIXED,
    SEQ_HASH_DISPOSITION_KEY,
    BoundHashKernel,
    ColumnDecision,
    SeqNativeRun,
    _table_columns,
)
from decoy_engine.execution._sequential_native_ops import HashCall
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity._d1a_hash_data import column_seed
from tests.parity._d2c_fpe_data import FpeMarkingKernel, fpe_seed, install_fpe, payload_shape
from tests.parity._seq_witness import MARK, MarkingKernel, install

KEY = b"k" * 8


def _remap_run(kernel: MarkingKernel) -> SeqNativeRun:
    """A run whose parent column t.k has a NATIVE REMAP decision on `kernel`."""
    run = SeqNativeRun()
    run._bound["hash"] = BoundHashKernel(kernel)  # type: ignore[arg-type]
    run._load_attempted.add("hash")
    run._remap_decisions[("t", "k")] = ColumnDecision("t", "k", "rust_companion", None, pa.string())
    return run


def _ctx(*, nested: str = "", table: str = "t") -> StrategyContext:
    ctx = object.__new__(StrategyContext)
    object.__setattr__(ctx, "current_table", table)
    object.__setattr__(ctx, "nested_outer_column", nested)
    return ctx


# E1: a declined orphan type goes to Python even when the parent's decision is native.


@pytest.mark.parametrize(
    "orphans",
    [
        pd.Series([1.5, 2.5]),  # double
        pd.Series(pd.to_datetime(["2020-01-01"], utc=True)),  # tz-aware timestamp
    ],
    ids=["double", "tz_timestamp"],
)
def test_a_declined_orphan_type_is_minted_in_python_not_on_the_kernel(
    orphans: pd.Series,
) -> None:
    kernel = MarkingKernel()
    run = _remap_run(kernel)
    assert run._remap_tokens("t", "k", orphans, "ns", KEY, None) is None
    assert kernel.calls == []  # the kernel never saw the batch
    record = run._remap[("t", "k")]
    assert (record.native_rows, record.python_rows) == (0, len(orphans))
    assert len(record.reasons) == 1
    assert record.reasons[0].startswith("hash_input_type_")
    assert record.reasons[0].split(":")[1] == "k"


# E3: the REMAP counters accumulate exactly, across calls and backends.


def test_remap_counters_accumulate_exactly_over_native_and_python_calls() -> None:
    kernel = MarkingKernel()
    run = _remap_run(kernel)
    assert run._remap_tokens("t", "k", pd.Series(["a", "b"]), "ns", KEY, None) is not None
    assert run._remap_tokens("t", "k", pd.Series(["c", "d", "e"]), "ns", KEY, None) is not None
    assert run._remap_tokens("t", "k", pd.Series([1.5] * 4), "ns", KEY, None) is None
    assert run._remap_tokens("t", "k", pd.Series([2.5]), "ns", KEY, None) is None
    record = run._remap[("t", "k")]
    assert (record.native_rows, record.python_rows) == (5, 5)
    assert [c.rows for c in kernel.calls] == [2, 3]
    entry = record.entry()
    assert entry["executed_backend"] == MIXED
    assert str(entry["reason"]).startswith("hash_input_type_not_native_admitted:k:double")


def test_parent_python_remap_counts_every_row_across_calls() -> None:
    run = SeqNativeRun()
    run._remap_decisions[("t", "k")] = ColumnDecision("t", "k", "python_scalar", "r", pa.string())
    for batch in (["a", "b"], ["c", "d", "e"]):
        assert run._remap_tokens("t", "k", pd.Series(batch), "ns", KEY, None) is None
    record = run._remap[("t", "k")]
    assert (record.native_rows, record.python_rows) == (0, 5)


# E2: truncate reaches the kernel on REMAP minting and on the scalar seam.


def _truncated_chain() -> s.Shape:
    seed = column_seed(namespace="ns", provider_config=(("truncate", 12),))
    plan = s.make_plan({"A": s.table_seed({"k": seed}), "B": s.table_seed({"k": seed})})
    return s.Shape(
        plan,
        s.graph(s.edge("A", "k", "B", "k", "ns", OrphanPolicy.REMAP)),
        {"A": pa.table({"k": s.strs(["a", "b"])}), "B": pa.table({"k": s.strs(["a", "b", "zz"])})},
    )


def test_remap_minting_passes_truncate_to_the_kernel_and_matches_the_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shape = _truncated_chain()
    oracle = s.run_full(shape).outputs
    kernel = install(monkeypatch, MarkingKernel())
    result = s.run_seq(shape)
    assert [c.truncate for c in kernel.calls] == [12, 12]  # scalar parent, then the orphan
    assert [c.rows for c in kernel.calls] == [2, 1]
    for table in ("A", "B"):
        got = result.outputs[table].column("k").to_pylist()
        want = oracle[table].column("k").to_pylist()
        assert got == [MARK + w for w in want]  # orphan included: minted on the kernel
        assert all(len(w) == 12 for w in want)


# E4: a column's row count accumulates over calls (chunks of one table never reset it).


def test_the_decision_row_count_accumulates_over_calls() -> None:
    kernel = MarkingKernel()
    run = SeqNativeRun()
    run._bound["hash"] = BoundHashKernel(kernel)  # type: ignore[arg-type]
    run._load_attempted.add("hash")
    decision = ColumnDecision("t", "x", "rust_companion", None, pa.string())
    run._decisions[("t", "x")] = decision
    ctx = _ctx()
    for batch in (["a", "b", "c"], ["d", "e"], []):
        df = pd.DataFrame({"x": pd.Series(batch, dtype=object)})
        run.native_column(ctx, df, "x", "hash", HashCall("ns", KEY, None))
    assert decision.rows == 5
    assert [c.rows for c in kernel.calls] == [3, 2]
    assert run.metrics()[SEQ_HASH_DISPOSITION_KEY] == [
        {"table": "t", "column": "x", "path": "scalar", "executed_backend": "rust_companion"}
    ]


# E5: the fail-closed error names the failing column.


def test_the_fpe_fail_closed_error_names_the_column(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fpe(monkeypatch, FpeMarkingKernel())
    cells = ["1234567890", "123", "1234567890", None, "1234567890", "1234567890", "1234567890"]
    shape = payload_shape({"bad_col": (s.strs(cells), fpe_seed())})
    with pytest.raises(StrategyError) as exc:
        s.run_seq(shape)
    assert "column 'bad_col'" in str(exc.value)


# E9: a malformed kernel result of any shape raises the coded error.


class _Wrong(FpeMarkingKernel):
    def __init__(self, make: object) -> None:
        super().__init__()
        self._make = make

    def run(self, values: pa.Array, **kwargs: object) -> object:
        result = super().run(values, **kwargs)  # type: ignore[arg-type]
        return type(result)(values=self._make(result.values), errors=(), warnings=())  # type: ignore[operator]


@pytest.mark.parametrize(
    "make",
    [
        lambda out: out.to_pylist(),  # not an Arrow array at all
        lambda out: out.cast(pa.large_string()),  # an Arrow array of the wrong type
        lambda out: pa.array([1] * len(out), pa.int64()),
    ],
    ids=["not_an_array", "large_string", "int64"],
)
def test_a_malformed_kernel_result_raises_the_coded_error(
    make: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fpe(monkeypatch, _Wrong(make))
    with pytest.raises(ExecutionError) as exc:
        s.run_seq(payload_shape())
    assert exc.value.code == "sequential_fpe_native_result_invalid"


# E6 / E7: inherited-child entries.


def test_an_inherited_entry_is_kept_for_a_hash_column_after_a_non_hash_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    plan = s.make_plan(
        {
            "P": s.table_seed({"a": s.passthrough(), "b": s.hashed("ns_b")}),
            "C": s.table_seed({}, {("a", "b"): "ns_g"}),
        }
    )
    shape = s.Shape(
        plan,
        s.graph(s.edge("P", ("a", "b"), "C", ("a", "b"), "ns_g")),
        {
            "P": pa.table({"a": s.strs(["x", "y"]), "b": s.strs(["u", "v"])}),
            "C": pa.table({"a": s.strs(["x", "y"]), "b": s.strs(["u", "v"])}),
        },
    )
    entries = s.run_seq(shape).quality_metrics[SEQ_HASH_DISPOSITION_KEY]
    inherited = [e for e in entries if e["path"] == "fk_inherited"]
    assert [(e["column"], e["parent"], e["parent_backend"]) for e in inherited] == [
        ("b", "P.b", "rust_companion")
    ]


def test_a_chain_grandchild_reports_its_inherited_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, MarkingKernel())
    result = s.run_seq(s.chain_shape(OrphanPolicy.PRESERVE))
    inherited = {
        (e["table"], e["column"]): e
        for e in result.quality_metrics[SEQ_HASH_DISPOSITION_KEY]
        if e["path"] == "fk_inherited"
    }
    assert inherited[("B", "k")]["parent_backend"] == "rust_companion"
    assert inherited[("C", "k")]["parent"] == "B.k"
    assert inherited[("C", "k")]["parent_backend"] == INHERITED


# E8: nulls convert to nulls under the pinned type.


@pytest.mark.parametrize("cells", [["a", None, "b"], ["a", float("nan"), "b"]], ids=["None", "NaN"])
def test_a_pinned_string_column_with_missing_cells_converts_to_nulls(cells: list[object]) -> None:
    decision = ColumnDecision("t", "x", "rust_companion", None, pa.string())
    out = SeqNativeRun._pinned_array(pd.Series(cells, dtype=object), decision)
    assert out.type == pa.string() and out.to_pylist() == ["a", None, "b"]


# E10: a table with no seed entry has no columns.


def test_a_table_absent_from_the_seed_envelope_has_no_columns() -> None:
    plan = s.make_plan({"A": s.table_seed({"k": s.hashed("ns")})})
    assert _table_columns(plan, "missing") == {}
    assert list(_table_columns(plan, "A")) == ["k"]


# hash_column: a nested outer column is never hashed natively, in or out of a REMAP scope.


def test_a_nested_outer_column_returns_to_python_in_and_out_of_remap() -> None:
    kernel = MarkingKernel()
    run = _remap_run(kernel)
    decision = ColumnDecision("t", "k", "rust_companion", None, pa.string())
    run._decisions[("t", "k")] = decision
    df = pd.DataFrame({"k": ["a", "b"]})
    ctx = _ctx(nested="outer")
    assert run.hash_column(ctx, df, "k", namespace="ns", mask_key=KEY, truncate=None) is None
    with run.remap_scope():
        assert run.hash_column(ctx, df, "k", namespace="ns", mask_key=KEY, truncate=None) is None
    assert kernel.calls == [] and decision.rows == 0 and run._remap == {}
