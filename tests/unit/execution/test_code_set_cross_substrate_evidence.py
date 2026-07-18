"""HC-1 slice 1, Codex round-4 P2 structural remediation.

Closes the class of bug the round-4 findings exposed: a code_set column's
corpus-provenance evidence (`ExecutionResult.quality_metrics
['code_set_corpora']`) silently going missing on a route or dispatch shape
the original HC-1 work did not exercise.

Two concrete holes this pins shut:

  1. NESTED CODE_SET MIS-KEYED EVIDENCE: `NestedStrategyHandler` invokes its
     child handler with the synthetic column name `_nested_leaves`, not the
     real outer column. `CodeSetHandler.run` used to key its evidence stamp
     off that literal parameter, so nested code_set evidence attributed to a
     nonexistent column, and two nested code_set columns in one table
     collided on the same `_nested_leaves` key -- one corpus's provenance
     silently disappeared. Fixed via `StrategyContext.nested_outer_column`.

  2. POLARS-NATIVE ROUTE OMITS CODE_SET EVIDENCE: a job whose outer strategy
     is `nested` (polars-native via `PandasStrategyPort`) and whose child is
     `code_set` used to populate `StrategyContext.code_set_corpora` but never
     merge it into the polars adapter's `ExecutionResult.quality_metrics` --
     a fully successful masked run silently returned no provenance. Fixed by
     merging `ctx.code_set_corpora_metrics()` into `_run_polars_native`'s
     result, mirroring `PandasExecutionAdapter.run`.

These tests drive full plans through BOTH `PandasExecutionAdapter` and
`PolarsExecutionAdapter` (the actual adapter boundary, not the handler in
isolation) so a future regression on either substrate's dispatch/merge path
fails here rather than shipping silently. The out-of-core route is not
included for the nested fixture: `execution.out_of_core._runner`'s evidence
scan is gated on `column_seed.strategy == "code_set"` (a plan-level scan
independent of live handler dispatch), so a nested-wrapped code_set column
is out of that route's scope by construction -- see
`tests/parity/test_out_of_core_group_c_parity.py::TestOutOfCoreCodeSetCorporaEvidence`
for the out-of-core direct-code_set coverage this test module does not
duplicate.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import PandasExecutionAdapter, PolarsExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = b"\xca\xfe" * 4  # 8 bytes


def _direct_code_set_col(code_set: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="code_set",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(("code_set", code_set), ("mode", "mask")),
        coherent_with=(),
    )


def _nested_code_set_col(target: str, code_set: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy="nested",
        provider=None,
        backend_type="decoy_native",
        backend_version="1",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(
            ("target", target),
            ("strategy", "code_set"),
            ("strategy_config", {"code_set": code_set, "mode": "mask"}),
        ),
        coherent_with=(),
    )


def _plan(table: str, per_column: dict[str, ColumnSeed]) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    table,
                    TableSeed(per_column=tuple(per_column.items()), per_group=()),
                ),
            ),
        )
    )


def _run_both(plan: Any, sources: dict[str, pa.Table]) -> tuple[Any, Any]:
    """Run the same plan through both adapters; return (pandas, polars) results."""
    pandas_result = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    polars_result = PolarsExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    return pandas_result, polars_result


class TestDirectCodeSetEvidenceBothSubstrates:
    """A direct (non-nested) code_set column must surface evidence on both
    adapters. `code_set` is not itself polars-native, so the polars adapter
    falls back to the pandas oracle for this job -- already-correct
    pre-round-4 behavior, pinned here as the baseline the nested cases below
    are contrasted against."""

    def test_direct_code_set_surfaces_evidence_on_both_adapters(self) -> None:
        plan = _plan("t", {"diag": _direct_code_set_col("icd10")})
        sources = {"t": pa.table({"diag": pa.array(["I10", "E11.9"], type=pa.string())})}

        pandas_result, polars_result = _run_both(plan, sources)

        for label, result in (("pandas", pandas_result), ("polars", polars_result)):
            corpora = result.quality_metrics.get("code_set_corpora")
            assert corpora is not None and len(corpora) == 1, (
                f"{label} adapter dropped direct code_set evidence: {result.quality_metrics!r}"
            )
            entry = corpora[0]
            assert entry["table"] == "t"
            assert entry["column"] == "diag"
            assert entry["code_set"] == "icd10"
            assert entry["row_count"] > 0

        # Confirms this job actually fell back to the pandas oracle on the
        # polars adapter (code_set has no native polars handler), not that
        # the assertion above passed by some unrelated accident.
        assert polars_result.quality_metrics["executed_substrate"] == {"code_set": "pandas"}


class TestNestedCodeSetOuterColumnAttribution:
    """Finding 1: a single nested code_set column must report its REAL outer
    column as evidence, not the synthetic `_nested_leaves` batch-collection
    name `NestedStrategyHandler` invokes its child with."""

    def _fixture(self) -> tuple[Any, dict[str, pa.Table]]:
        plan = _plan(
            "t",
            {"wrapped": _nested_code_set_col("$.v", "icd10")},
        )
        sources = {
            "t": pa.table(
                {
                    "wrapped": pa.array(
                        [json.dumps({"v": "I10"}), json.dumps({"v": "E11.9"})],
                        type=pa.string(),
                    )
                }
            )
        }
        return plan, sources

    def test_nested_code_set_reports_outer_column_on_pandas(self) -> None:
        plan, sources = self._fixture()
        result = PandasExecutionAdapter().run(
            plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        corpora = result.quality_metrics.get("code_set_corpora")
        assert corpora is not None and len(corpora) == 1, (
            f"pandas adapter dropped nested code_set evidence: {result.quality_metrics!r}"
        )
        entry = corpora[0]
        assert entry["column"] == "wrapped", (
            f"evidence attributed to {entry['column']!r}, expected the outer column "
            "'wrapped' -- not the synthetic '_nested_leaves' name the child handler "
            "was literally invoked with."
        )
        assert entry["table"] == "t"
        assert entry["code_set"] == "icd10"
        assert entry["row_count"] > 0

    def test_nested_code_set_reports_outer_column_on_polars(self) -> None:
        plan, sources = self._fixture()
        result = PolarsExecutionAdapter().run(
            plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        # nested is polars-native (PandasStrategyPort); this job has no FK
        # edges and no other strategy, so it must classify fully native --
        # confirming the merge fix in `_run_polars_native` is what is under
        # test, not an oracle fallback.
        assert result.quality_metrics["executed_substrate"] == {"nested": "polars"}
        corpora = result.quality_metrics.get("code_set_corpora")
        assert corpora is not None and len(corpora) == 1, (
            f"polars-native route dropped nested code_set evidence: {result.quality_metrics!r}"
        )
        entry = corpora[0]
        assert entry["column"] == "wrapped"
        assert entry["table"] == "t"
        assert entry["code_set"] == "icd10"
        assert entry["row_count"] > 0


class TestTwoNestedCodeSetColumnsDistinctOuterColumns:
    """The exact reproduction the round-4 cross-model review used: a
    two-column table where BOTH columns are nested code_set bound to
    DIFFERENT corpora. Pre-fix, both children were dispatched with the same
    synthetic `_nested_leaves` column name, so the second stamp silently
    overwrote the first in `StrategyContext.code_set_corpora` -- one
    corpus's provenance vanished. Proven on both adapters: pandas exercises
    the mis-keying fix directly; polars additionally exercises the
    polars-native evidence-merge fix (nested is polars-native, so this job
    has no FK edges/unmigrated strategies and classifies fully native)."""

    def _fixture(self) -> tuple[Any, dict[str, pa.Table]]:
        plan = _plan(
            "t",
            {
                "a": _nested_code_set_col("$.v", "icd10"),
                "b": _nested_code_set_col("$.v", "mcc"),
            },
        )
        sources = {
            "t": pa.table(
                {
                    "a": pa.array(
                        [json.dumps({"v": "I10"}), json.dumps({"v": "E11.9"})],
                        type=pa.string(),
                    ),
                    "b": pa.array(
                        [json.dumps({"v": "alpha"}), json.dumps({"v": "beta"})],
                        type=pa.string(),
                    ),
                }
            )
        }
        return plan, sources

    def _assert_both_corpora_present(self, quality_metrics: dict[str, Any], label: str) -> None:
        corpora = quality_metrics.get("code_set_corpora")
        assert corpora is not None and len(corpora) == 2, (
            f"{label} adapter dropped one nested code_set corpus's evidence "
            f"(collision on the synthetic column name): {quality_metrics!r}"
        )
        by_column = {e["column"]: e["code_set"] for e in corpora}
        assert by_column == {"a": "icd10", "b": "mcc"}, (
            f"{label} adapter evidence not keyed by distinct outer columns: {by_column!r}"
        )
        for entry in corpora:
            assert entry["table"] == "t"
            assert entry["row_count"] > 0
            # Counts + identifiers only -- no raw codes leak into evidence.
            assert "codes" not in entry
            assert "rows" not in entry

    def test_both_nested_corpora_survive_on_pandas(self) -> None:
        plan, sources = self._fixture()
        result = PandasExecutionAdapter().run(
            plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        self._assert_both_corpora_present(result.quality_metrics, "pandas")

    def test_both_nested_corpora_survive_on_polars(self) -> None:
        plan, sources = self._fixture()
        result = PolarsExecutionAdapter().run(
            plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        assert result.quality_metrics["executed_substrate"] == {"nested": "polars"}
        self._assert_both_corpora_present(result.quality_metrics, "polars")
