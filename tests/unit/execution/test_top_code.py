"""HC-3b: top_code numeric top-coding / bottom-coding strategy.

Mirrors `test_hash_bucketize.py`'s structure (top_code is bucketize's direct
sibling: same coercion + fail-closed pattern, config-resolution precedence,
and RowError bookkeeping) plus `test_bucket_perturb.py`'s ColumnSeed/plan/run
harness for integration-level assertions.

Design note pinned by these tests (see `_top_code.py` module docstring): an
in-range cell renders through `str()`, same as a tail cell, rather than
staying in its native numeric type. A column mixing native Python numerics
(kept cells) with `str` labels (tail cells) would fail the engine's single
Arrow<->pandas conversion boundary (`pa.Table.from_pandas` raises
`ArrowInvalid` on a genuinely mixed-type object column) the moment BOTH a
kept and a generalized row exist -- the HIPAA age>89 column's ordinary
shape. Per-cell formatting must also be independent of what other rows in
the same chunk need, which `CHUNK_SAFE_STRATEGIES` membership requires. The
in-range cell's numeric CONTENT is still exactly preserved (only its Python
type changes).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import ExecutionError, ExecutionResult, PandasExecutionAdapter
from decoy_engine.execution._errors import StrategyError
from decoy_engine.execution._row_errors import RowError
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution._strategies._top_code import TopCodeStrategyHandler
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


class TestNullPassthrough:
    def test_null_source_passes_through_unchanged_no_row_error(self) -> None:
        src = pa.table({"age": [23, None, 105]})
        out = _run(_plan("age", _col((("preset", "hipaa_age"),))), src)
        # A null-bearing int64 Arrow column upcasts to pandas float64 on
        # ingestion (numpy int64 has no null representation) -- an unrelated
        # pandas/Arrow interop nuance every numeric strategy inherits, not
        # something top_code introduces; "23.0" reflects the ingested float,
        # not a masking bug. The null cell itself passes through untouched.
        assert out.output.column("age").to_pylist() == ["23.0", None, "90+"]
        assert out.row_errors == ()


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
