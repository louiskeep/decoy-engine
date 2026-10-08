"""Run-time backstop for a bucket_perturb `date_format` that cannot write a date back.

These tests bypass plan compile (raw plans built by hand), as a direct library
caller or a stored raw-dict config would. Every route must still refuse the
format with `bucket_perturb_invalid_config` before any output is written, even
when there is nothing to mask: an empty frame, an all-null column, or a `when:`
predicate that selects no row.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._strategies._nested import NestedStrategyHandler
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._mask import mask_column, masked_output_type
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = b"\xb0\x0c\xfe\x00\x12\x34\x56\x78"
CODE = "bucket_perturb_invalid_config"

_BAD = ["mixed", "ISO8601", "%Q", "no directive", "%H:%M:%S", 0]


def _bp(
    date_format: Any = "mixed", *, when: str | None = None, strategy: str = "bucket_perturb"
) -> ColumnSeed:
    return ColumnSeed(
        namespace="dates",
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("bucket", "month"), ("date_format", date_format)),
        coherent_with=(),
        when=when,
    )


def _plan(per_table: tuple[tuple[str, TableSeed], ...]) -> Any:
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=_SEED, per_table=per_table))


def _single(seed: ColumnSeed) -> Any:
    return _plan((("t", TableSeed(per_column=(("d", seed),), per_group=())),))


def _oracle(seed: ColumnSeed, values: list[str | None]) -> Any:
    return PandasExecutionAdapter().run_single(
        _single(seed),
        pa.table({"d": pa.array(values, type=pa.string())}),
        registry=_REG,
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
    )


# ── pandas oracle ────────────────────────────────────────────────────────────

_SHAPES = {
    "populated": ["2024-01-15", "2024-03-02", None],
    "empty": [],
    "all_null": [None, None],
}


class TestOracle:
    @pytest.mark.parametrize("fmt", _BAD, ids=[repr(f) for f in _BAD])
    @pytest.mark.parametrize("shape", list(_SHAPES))
    def test_rejected_for_every_input_shape(self, fmt: Any, shape: str) -> None:
        with pytest.raises(StrategyError) as exc:
            _oracle(_bp(fmt), _SHAPES[shape])
        assert exc.value.code == CODE
        assert exc.value.strategy == "bucket_perturb"

    def test_rejected_under_a_when_gate_that_selects_nothing(self) -> None:
        seed = _bp("mixed", when="d == 'ZZZ_NEVER_MATCHES'")
        with pytest.raises(StrategyError) as exc:
            _oracle(seed, ["2024-01-15", "2024-03-02"])
        assert exc.value.code == CODE

    def test_rejected_under_a_when_gate_on_an_empty_frame(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _oracle(_bp("mixed", when="d == 'x'"), [])
        assert exc.value.code == CODE

    def test_a_writable_format_still_runs_under_a_zero_match_gate(self) -> None:
        seed = _bp("%Y-%m-%d", when="d == 'ZZZ_NEVER_MATCHES'")
        out = _oracle(seed, ["2024-01-15"])
        assert out.output.column("d").to_pylist() == ["2024-01-15"]

    def test_the_error_does_not_carry_a_cell_value(self) -> None:
        with pytest.raises(StrategyError) as exc:
            _oracle(_bp("mixed"), ["2024-01-15"])
        assert "2024-01-15" not in str(exc.value)

    def test_a_valid_format_is_unchanged(self) -> None:
        out = _oracle(_bp("%Y-%m-%d"), ["2024-06-15"])
        assert out.output.column("d").to_pylist() == ["2024-06-12"]


# ── nested child, run time ───────────────────────────────────────────────────


class _Ctx:
    def __init__(self) -> None:
        self.row_errors: list = []
        self.job_seed = _SEED
        self.mask_key = _SEED
        self.row_offset = 0


def _nested_seed(child_format: Any, *, when: str | None = None) -> ColumnSeed:
    child = {"bucket": "month", "date_format": child_format}
    return ColumnSeed(
        namespace="dates",
        strategy="nested",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=tuple(
            sorted(
                {"target": "$.seen", "strategy": "bucket_perturb", "strategy_config": child}.items()
            )
        ),
        when=when,
    )


_NESTED_CELLS = {
    "populated": [json.dumps({"seen": "2024-01-15"})],
    "no_match": [json.dumps({"other": 1})],  # target matches nothing in any cell
    "all_null": [None, None],
    "empty": [],
}


class TestNestedChildAtRunTime:
    @pytest.mark.parametrize("shape", list(_NESTED_CELLS))
    def test_rejected_for_every_leaf_shape(self, shape: str) -> None:
        df = pd.DataFrame({"data": pd.Series(_NESTED_CELLS[shape], dtype=object)})
        with pytest.raises(StrategyError) as exc:
            NestedStrategyHandler().run(df, "data", _nested_seed("mixed"), _Ctx())
        assert exc.value.code == CODE

    def test_rejected_by_preflight_for_a_zero_match_gate(self) -> None:
        with pytest.raises(StrategyError) as exc:
            NestedStrategyHandler().preflight(_nested_seed("mixed", when="x == 1"), _Ctx())  # type: ignore[arg-type]
        assert exc.value.code == CODE

    def test_a_valid_child_passes_preflight(self) -> None:
        NestedStrategyHandler().preflight(_nested_seed("%Y-%m-%d", when="x == 1"), _Ctx())  # type: ignore[arg-type]

    def test_a_valid_child_still_masks(self) -> None:
        df = pd.DataFrame({"data": [json.dumps({"seen": "2024-06-15"})]})
        out, _ = NestedStrategyHandler().run(df, "data", _nested_seed("%Y-%m-%d"), _Ctx())
        assert json.loads(out["data"].iloc[0])["seen"] == "2024-06-12"


# ── out-of-core kernel and schema resolution ─────────────────────────────────


class TestOutOfCore:
    @pytest.mark.parametrize("fmt", _BAD, ids=[repr(f) for f in _BAD])
    def test_mask_column_rejects(self, fmt: Any) -> None:
        values = pa.array(["2024-01-15"], type=pa.string())
        with pytest.raises(StrategyError) as exc:
            mask_column(values, _bp(fmt), _SEED, column="d")
        assert exc.value.code == CODE

    @pytest.mark.parametrize("fmt", _BAD, ids=[repr(f) for f in _BAD])
    def test_mask_column_rejects_an_empty_batch(self, fmt: Any) -> None:
        with pytest.raises(StrategyError) as exc:
            mask_column(pa.array([], type=pa.string()), _bp(fmt), _SEED, column="d")
        assert exc.value.code == CODE

    @pytest.mark.parametrize("fmt", _BAD, ids=[repr(f) for f in _BAD])
    def test_output_type_resolution_rejects(self, fmt: Any) -> None:
        with pytest.raises(StrategyError) as exc:
            masked_output_type(_bp(fmt), pa.string())
        assert exc.value.code == CODE

    def test_a_valid_format_resolves(self) -> None:
        assert masked_output_type(_bp("%Y-%m-%d"), pa.string()) == pa.string()


class _SpySink:
    """Records every write so a test can prove nothing was staged."""

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.committed = False
        self.aborted = False

    def write(self, table: str, data: pa.Table) -> None:
        self.writes.append(table)

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        self.writes.append(table)
        # A real sink drains the stream; the runner builds parent relations from it.
        for _ in batches:
            pass

    def commit(self) -> None:
        self.committed = True

    def abort(self) -> None:
        self.aborted = True


def _fk_job(
    fmt: Any, values: list[str | None], *, parent_fmt: Any = None
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    key = ColumnSeed(
        namespace="kns",
        strategy="hash",
        provider="hash",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )
    n = len(values)
    parent = pa.table(
        {
            "pk": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "pay": pa.array(values, type=pa.string()),
        }
    )
    child = pa.table(
        {
            "fk": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "cpay": pa.array(list(reversed(values)), type=pa.string()),
        }
    )
    plan = _plan(
        (
            (
                "parent",
                TableSeed(per_column=(("pk", key), ("pay", _bp(parent_fmt or fmt))), per_group=()),
            ),
            ("child", TableSeed(per_column=(("fk", key), ("cpay", _bp(fmt))), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=OrphanPolicy.FAIL,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


class TestOutOfCoreRunner:
    @pytest.mark.parametrize(
        "values", [["2024-01-15", "2024-03-02"], [None, None]], ids=["populated", "all_null"]
    )
    @pytest.mark.parametrize("fmt", ["mixed", "%Q"])
    def test_runner_rejects_before_any_write(self, fmt: str, values: list[str | None]) -> None:
        plan, sources, graph = _fk_job(fmt, values)
        sink = _SpySink()
        with pytest.raises(StrategyError) as exc:
            run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph, sink=sink)  # type: ignore[arg-type]
        assert exc.value.code == CODE
        assert sink.writes == []
        assert not sink.committed

    def test_a_later_table_rejects_before_the_first_table_is_written(self) -> None:
        # The parent's format is valid; only the child (processed second) is not.
        plan, sources, graph = _fk_job("mixed", ["2024-01-15", "2024-03-02"], parent_fmt="%Y-%m-%d")
        sink = _SpySink()
        with pytest.raises(StrategyError) as exc:
            run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph, sink=sink)  # type: ignore[arg-type]
        assert exc.value.code == CODE
        assert sink.writes == []
        assert not sink.committed
