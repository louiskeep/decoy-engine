"""HC-3b: top_code numeric top-coding / bottom-coding strategy.

Mirrors `test_hash_bucketize.py`'s structure (top_code is bucketize's direct
sibling: same coercion + fail-closed pattern, config-resolution precedence,
and RowError bookkeeping) plus `test_bucket_perturb.py`'s ColumnSeed/plan/run
harness for integration-level assertions.

Design note pinned by these tests (see `_top_code.py` module docstring): an
in-range cell renders to a string (not its native numeric type), so the column
never mixes kept ints with generalized `str` labels -- that would fail the
engine's single Arrow<->pandas boundary (`pa.Table.from_pandas` raises
`ArrowInvalid` on a genuinely mixed-type object column) the moment both a kept
and a generalized row exist, the HIPAA age>89 column's ordinary shape. The
in-range string is CANONICAL: integral values render WITHOUT a trailing ".0",
derived from the coerced numeric rather than the raw column's inferred dtype,
so `str(value)` is a pure function of the value -- identical whether the chunk
ingested as int64 (null-free) or float64 (null-bearing), which
`CHUNK_SAFE_STRATEGIES` membership requires. `TestChunkSafety` pins the
whole-frame-vs-chunked byte-identity on a null-bearing int64 column (the
BLOCKER Dennis reproduced). The in-range cell's numeric CONTENT is preserved
exactly (only its Python type changes).
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine import run_mask_pipeline_chunked
from decoy_engine.config import PipelineConfig
from decoy_engine.execution import (
    ExecutionError,
    ExecutionResult,
    PandasExecutionAdapter,
    run_pipeline,
)
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution._strategies._top_code import TopCodeStrategyHandler
from decoy_engine.execution._when_gate import run_with_when_gate
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = (0x77).to_bytes(8, "big")


def _col(
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="top_code",
        provider="top_code",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(col_name: str, seed: ColumnSeed) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(("t", TableSeed(per_column=((col_name, seed),), per_group=())),),
        )
    )


def _run(plan: Any, table: pa.Table) -> ExecutionResult:
    return PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )


class _FakeCtx:
    """Minimal StrategyContext stand-in for direct handler.run() calls
    (mirrors test_hash_bucketize.py's `_FakeCtx`, extended with a real
    mutable `row_errors` sink since top_code appends to it)."""

    job_seed = _SEED
    mask_key = _SEED

    def __init__(self) -> None:
        self.row_errors: list[RowError] = []


class TestHipaaAgePreset:
    def test_age_89_kept_90_and_105_generalized(self) -> None:
        src = pa.table({"age": [89, 90, 105]})
        out = _run(_plan("age", _col((("preset", "hipaa_age"),))), src)
        assert out.output.column("age").to_pylist() == ["89", "90+", "90+"]


class TestManualCapOverLabel:
    def test_manual_bound(self) -> None:
        src = pa.table({"score": [50, 100, 101, 999]})
        seed = _col((("cap", 100), ("over_label", "capped")))
        out = _run(_plan("score", seed), src)
        assert out.output.column("score").to_pylist() == ["50", "100", "capped", "capped"]


class TestFloorAndUnderLabel:
    def test_both_tails(self) -> None:
        src = pa.table({"v": [-50, -10, 0, 10, 50, 200]})
        seed = _col((("cap", 100), ("over_label", "BIG"), ("floor", -10), ("under_label", "SMALL")))
        out = _run(_plan("v", seed), src)
        assert out.output.column("v").to_pylist() == [
            "SMALL",
            "-10",
            "0",
            "10",
            "50",
            "BIG",
        ]


class TestInRangeContentPreserved:
    def test_in_range_string_matches_original_numeric_content(self) -> None:
        """The in-range cell's numeric CONTENT is unchanged; only its Python
        type changes from numeric to its string rendering (see module
        docstring for the Arrow-boundary + chunk-safety rationale)."""
        src = pa.table({"age": [23, 45, 89]})
        out = _run(_plan("age", _col((("preset", "hipaa_age"),))), src)
        result = out.output.column("age").to_pylist()
        assert result == ["23", "45", "89"]
        assert all(int(v) == orig for v, orig in zip(result, [23, 45, 89], strict=True))


class TestLargeIntegralRender:
    """Large but VALID (|value| < 2**53) integral values render canonically on
    the float path (no trailing ".0", exact, no int64 overflow) via
    arbitrary-precision Python int. Codex BLOCKER: a cap AT/beyond 2**53 is
    fail-closed rejected instead (the tail comparison cannot be exact on a
    null-widened column), so the overflow path is unreachable by construction."""

    def test_large_valid_integral_float_renders_exactly(self) -> None:
        # 5e14 < 2**53: integral, exactly representable, in-range under cap 1e15.
        src = pa.table({"n": pa.array([5e14, None], type=pa.float64())})
        seed = _col((("cap", 10**15), ("over_label", "HUGE")))
        out = _run(_plan("n", seed), src)
        got = out.output.column("n").to_pylist()
        assert got == ["500000000000000", None]
        assert not got[0].endswith(".0")

    def test_cap_at_or_beyond_2_to_53_is_fail_closed(self) -> None:
        # Codex BLOCKER 1: cap >= 2**53 can't be compared exactly against a
        # null-widened column, so a true tail value could escape generalization.
        # The handler resolves no bound and fails closed.
        src = pa.table({"n": [1, 2, 3]})
        seed = _col((("cap", 2**53), ("over_label", "OVER")))
        with pytest.raises((ExecutionError, StrategyError)):
            _run(_plan("n", seed), src)


class TestNullPassthrough:
    def test_null_source_passes_through_unchanged_no_row_error(self) -> None:
        src = pa.table({"age": [23, None, 105]})
        out = _run(_plan("age", _col((("preset", "hipaa_age"),))), src)
        # A null-bearing int64 Arrow column upcasts to pandas float64 on
        # ingestion (numpy int64 has no null representation). top_code renders
        # the in-range cell CANONICALLY (integral -> no trailing ".0") from the
        # coerced numeric, NOT `raw.astype(str)`, so 23 renders "23" whether or
        # not the chunk carried a null -- the dtype-independence the CHUNK_SAFE
        # contract requires (see test_chunk_safety below). The null cell itself
        # passes through untouched.
        assert out.output.column("age").to_pylist() == ["23", None, "90+"]
        assert out.row_errors == ()


class TestChunkSafety:
    """BLOCKER regression (Dennis, HC-3b re-gate): top_code is in
    CHUNK_SAFE_STRATEGIES, so `run_mask_pipeline_chunked` must be byte-identical
    to the full-frame `run_pipeline` for ANY chunking. An Arrow int64 column
    with a null ingests as pandas int64 in a null-free chunk but widens to
    float64 in a null-bearing one; a naive `raw.astype(str)` would emit "89" in
    one chunk and "89.0" in another for the same age, splitting joins. The
    canonical render (integral -> no ".0", from the coerced numeric) must make
    every chunking agree.
    """

    _CFG_COLUMNS = [
        {"name": "age", "strategy": "top_code", "provider_config": {"preset": "hipaa_age"}}
    ]

    def _cfg(self, tmp_path) -> dict:
        cfg = {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
            "tables": [{"name": "t", "columns": self._CFG_COLUMNS}],
            "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}},
        }
        return PipelineConfig.model_validate(cfg).model_dump()

    # Boundaries >=2 keep at least one real value beside the null in its chunk.
    # boundary=1 would isolate the single null into an ALL-NULL chunk, which any
    # string-output coarsening strategy (bucketize included, verified) cannot
    # emit -- an all-null object column infers Arrow `null` type and fails to
    # concat with the string chunks. That is a pre-existing chunked-route
    # limitation shared with bucketize, not the dtype-rendering regression this
    # test pins; see docs/backlog for the all-null-chunk follow-up.
    @pytest.mark.parametrize("boundary", [2, 3, 4, 5])
    def test_chunked_equals_full_frame_on_null_bearing_int64(self, tmp_path, boundary: int) -> None:
        # Arrow int64 + null: some slices are null-free (pandas int64), the slice
        # holding the null widens to float64. 67 and 89 are in-range; 94 and 200
        # are the >89 tail. Every boundary must produce identical output.
        src = pa.table({"age": [67, 94, 67, None, 89, 200]})
        src.to_pandas().to_csv(tmp_path / "in.csv", index=False)
        cfg = self._cfg(tmp_path)

        full = run_pipeline(cfg, sources={"t": src}, engine_version="hc3b-test").outputs["t"]
        chunks = [src.slice(i, boundary) for i in range(0, src.num_rows, boundary)]
        chunked = pa.concat_tables(
            list(run_mask_pipeline_chunked(cfg, chunks, table="t", engine_version="hc3b-test"))
        )
        assert chunked.column("age").to_pylist() == full.column("age").to_pylist()

    def test_in_range_never_carries_trailing_dot_zero(self, tmp_path) -> None:
        src = pa.table({"age": [67, 94, 67, None, 89, 200]})
        src.to_pandas().to_csv(tmp_path / "in.csv", index=False)
        cfg = self._cfg(tmp_path)
        vals = run_pipeline(cfg, sources={"t": src}, engine_version="hc3b-test").outputs["t"]
        got = vals.column("age").to_pylist()
        assert got == ["67", "90+", "67", None, "89", "90+"]
        assert not any(isinstance(v, str) and v.endswith(".0") for v in got if v is not None)

    @pytest.mark.parametrize("boundary", [2, 3, 4])
    def test_large_null_bearing_int_is_chunk_exact(self, tmp_path, boundary: int) -> None:
        # Codex R2 HIGH 1: a large in-range int64 with a null used to widen to
        # float64 in the full-frame/null-bearing path (rounding it) but stay
        # exact in a null-free chunk -> chunk-dependent output. The lossless
        # nullable-Int64 ingest (top_code_columns) makes it exact everywhere.
        big = -9007199254740993  # |v| > 2**53: not exactly representable in float64
        src = pa.table({"n": pa.array([big, 1, None, 90], type=pa.int64())})
        cfg = {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "in.csv")}},
            "tables": [
                {
                    "name": "t",
                    "columns": [
                        {
                            "name": "n",
                            "strategy": "top_code",
                            "provider_config": {"cap": 89, "over_label": "OVER"},
                        }
                    ],
                }
            ],
            "targets": {"t": {"type": "file", "format": "csv", "path": str(tmp_path / "out.csv")}},
        }
        cfg = PipelineConfig.model_validate(cfg).model_dump()
        src.to_pandas().to_csv(tmp_path / "in.csv", index=False)
        full = run_pipeline(cfg, sources={"t": src}, engine_version="hc3b-test").outputs["t"]
        chunks = [src.slice(i, boundary) for i in range(0, src.num_rows, boundary)]
        chunked = pa.concat_tables(
            list(run_mask_pipeline_chunked(cfg, chunks, table="t", engine_version="hc3b-test"))
        )
        assert chunked.column("n").to_pylist() == full.column("n").to_pylist()
        # and the large in-range value is preserved EXACTLY (not float-rounded).
        assert full.column("n").to_pylist()[0] == str(big)


class TestBottomBoundFailClosed:
    """Codex R2 HIGH 2: a PRESENT-but-invalid floor must fail closed at the
    handler, not silently disable bottom-coding. The compile check rejects these
    shapes, but a Plan deserialized straight into execution bypasses it, so the
    handler is the backstop."""

    def test_invalid_floor_raises(self) -> None:
        with pytest.raises(StrategyError) as exc:
            TopCodeStrategyHandler._resolve_bottom_bound({"floor": -(2**53), "under_label": "U"})
        assert exc.value.code == "top_code_bounds_unresolvable"

    def test_floor_without_under_label_raises(self) -> None:
        with pytest.raises(StrategyError):
            TopCodeStrategyHandler._resolve_bottom_bound({"floor": 0})

    def test_absent_floor_is_not_an_error(self) -> None:
        assert TopCodeStrategyHandler._resolve_bottom_bound({}) == (None, None)

    def test_explicit_none_floor_fails_closed(self) -> None:
        # Codex R3 HIGH: `floor: None` is PRESENT-but-malformed (keyed on
        # "floor" in cfg, not `is None`), so it must raise, NOT be read as
        # "no bottom tail" the way an absent floor is.
        with pytest.raises(StrategyError) as exc:
            TopCodeStrategyHandler._resolve_bottom_bound({"floor": None, "under_label": "U"})
        assert exc.value.code == "top_code_bounds_unresolvable"


class TestWhenGatePreflightFailsClosed:
    """Codex R4 HIGH: the compile check rejects when+top_code, but a Plan that
    bypasses compile (direct construction / YAML deserialization) reaches the
    handler with a live `when`. The when-gate calls `preflight` unconditionally,
    so top_code's preflight is the route-independent backstop: it fails closed
    whether the gate matches zero rows (validation-bypass leak) or some rows
    (Int64 writeback crash)."""

    def test_preflight_always_raises_coded_error(self) -> None:
        # preflight is invoked by the when-gate ONLY when plan.when is set, so
        # its invocation means when+top_code -- it raises unconditionally.
        with pytest.raises(StrategyError) as exc:
            TopCodeStrategyHandler().preflight(_col((("preset", "hipaa_age"),)), _FakeCtx())
        assert exc.value.code == "top_code_with_when_unsupported"

    def _when_seed(self, when: str) -> ColumnSeed:
        base = _col((("preset", "hipaa_age"),))
        return replace(base, when=when)

    @pytest.mark.parametrize(
        "when, region",
        [
            ("region == 'nowhere'", ["A", "B", "C"]),  # zero-match (validation bypass)
            ("region == 'A'", ["A", "B", "C"]),  # some match (Int64 writeback crash)
        ],
    )
    def test_when_gate_fails_closed_on_both_match_counts(self, when: str, region: list) -> None:
        # Drive the REAL pandas when-gate: preflight runs unconditionally before
        # the zero-match short-circuit AND before run(), so both of Codex R4's
        # reachable scenarios raise the coded error instead of leaking/crashing.
        df = pd.DataFrame({"age": [50, 90, 105], "region": region})
        with pytest.raises(StrategyError) as exc:
            run_with_when_gate(
                TopCodeStrategyHandler(), df.copy(), "age", self._when_seed(when), _FakeCtx()
            )
        assert exc.value.code == "top_code_with_when_unsupported"


class TestNonNullUncoercible:
    def test_uncoercible_cell_records_row_error_and_leaves_original(self) -> None:
        """A non-null, non-numeric cell is a RowError (trigger format_error),
        NOT a silent keep-original -- direct handler call (mirrors
        `TestMixedObjectColumnRegression` in test_hash_bucketize.py) since a
        genuinely mixed str/int source column has no single Arrow type."""
        df = pd.DataFrame({"age": [23, "abc", 90]})
        seed = _col((("preset", "hipaa_age"),))
        ctx = _FakeCtx()
        out, warnings = TopCodeStrategyHandler().run(df.copy(), "age", seed, ctx)

        assert out["age"].iloc[1] == "abc"  # original left in place, not rewritten
        assert len(ctx.row_errors) == 1
        err = ctx.row_errors[0]
        assert err.column == "age"
        assert err.row_index == 1
        assert err.trigger == "format_error"
        assert "abc" not in err.reason  # trap T3: reason never embeds the cell value


class TestUnresolvableConfigFailsClosed:
    def test_no_preset_no_cap_raises(self) -> None:
        src = pa.table({"age": [23]})
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("age", _col(())), src)
        assert exc.value.code == "top_code_bounds_unresolvable"

    def test_it_is_a_strategy_error(self) -> None:
        src = pa.table({"age": [23]})
        with pytest.raises(StrategyError):
            _run(_plan("age", _col(())), src)

    def test_unknown_preset_raises(self) -> None:
        src = pa.table({"age": [23]})
        with pytest.raises(ExecutionError) as exc:
            _run(_plan("age", _col((("preset", "by_fortnight"),))), src)
        assert exc.value.code == "top_code_bounds_unresolvable"


class TestEvidenceWarning:
    def test_warning_emitted_with_over_and_under_decisions_no_raw_values(self) -> None:
        src = pa.table({"v": [-50, 5, 200]})
        seed = _col((("cap", 100), ("over_label", "BIG"), ("floor", -10), ("under_label", "SMALL")))
        out = _run(_plan("v", seed), src)
        assert len(out.warnings) == 1
        w = out.warnings[0]
        assert w.code == "top_code_generalized"
        assert w.provider == "top_code"
        assert w.column == "v"
        assert w.detail["generalized"] == {"row_0": "under", "row_2": "over"}
        assert w.detail["over_count"] == 1
        assert w.detail["under_count"] == 1
        # HC-1 lesson: never leak raw values into evidence.
        detail_str = str(w.detail)
        for raw in ("-50", "200", "5"):
            assert raw not in detail_str

    def test_no_warning_when_nothing_generalized(self) -> None:
        src = pa.table({"age": [10, 20, 30]})
        out = _run(_plan("age", _col((("preset", "hipaa_age"),))), src)
        assert out.warnings == ()


class TestWiredThroughAdapter:
    def test_top_code_registered_in_scalar_handlers(self) -> None:
        assert "top_code" in SCALAR_HANDLERS
        assert isinstance(SCALAR_HANDLERS["top_code"], TopCodeStrategyHandler)
