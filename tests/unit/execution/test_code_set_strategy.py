"""SP-09b: integration tests for the code_set strategy handler (B1 / TDD).

B1 proved: a plan with ``strategy: code_set`` must be reachable through
``_pandas_adapter`` (via SCALAR_HANDLERS), not only via calling
``apply_code_set()`` directly.

These tests drive a real ``ColumnSeed`` with ``strategy: "code_set"``
through ``PandasExecutionAdapter.run_single``, mirroring the pattern used
by the geo_generalize / joint_mask siblings (SP-08).

Methodology: the plan-to-execution path (Arrow in, Arrow out) exercises
strategy registration in SCALAR_HANDLERS. A test that only calls
``apply_code_set()`` directly does NOT prove the strategy is wired.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())
_SEED = b"\xca\xfe" * 4  # 8 bytes


def _col(
    strategy: str,
    *,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider="x_nobackend",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(column: str, col_seed: ColumnSeed) -> Any:
    return SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_SEED,
            per_table=(
                (
                    "t",
                    TableSeed(
                        per_column=((column, col_seed),),
                        per_group=(),
                    ),
                ),
            ),
        )
    )


def _run(column: str, values: list[str | None], provider_config: tuple) -> list:
    table = pa.table({column: pa.array(values, type=pa.string())})
    plan = _plan(column, _col("code_set", provider_config=provider_config))
    result = PandasExecutionAdapter().run_single(
        plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
    )
    return result.output.column(column).to_pylist()


class TestCodeSetAdapterIntegration:
    """B1: code_set is reachable through a real plan -> _pandas_adapter path."""

    def test_code_set_mask_mode_reachable_via_adapter(self) -> None:
        """strategy: code_set is registered in SCALAR_HANDLERS and produces
        real ICD-10 corpus codes when driven through PandasExecutionAdapter."""
        from decoy_engine.transforms.code_set import load_corpus

        codes = {row["code"] for row in load_corpus("icd10")}
        values = ["I10", "E11.9", "F32.9"]
        out = _run(
            "diag",
            values,
            (("code_set", "icd10"), ("mode", "mask")),
        )
        assert len(out) == 3
        # Every output must be a real corpus code.
        for i, v in enumerate(out):
            assert v in codes, (
                f"row {i}: output {v!r} is not a real ICD-10 code. "
                "code_set must be wired into SCALAR_HANDLERS."
            )
        # Mask mode guarantees output != input.
        for inp, outp in zip(values, out, strict=True):
            assert outp != inp, f"mask mode output {outp!r} must differ from input {inp!r}"

    def test_code_set_gen_mode_reachable_via_adapter(self) -> None:
        """Gen mode also routes through the adapter and produces corpus codes."""
        from decoy_engine.transforms.code_set import load_corpus

        codes = {row["code"] for row in load_corpus("icd10")}
        values = ["any1", "any2", "any3", "any4", "any5"]
        out = _run(
            "diag",
            values,
            (("code_set", "icd10"), ("mode", "gen")),
        )
        assert len(out) == 5
        for v in out:
            assert v in codes, f"gen mode output {v!r} is not a real ICD-10 code."

    def test_code_set_gen_mode_varies_across_column(self) -> None:
        """H1: gen mode must NOT produce a constant column.
        A fixed job_seed must yield >1 distinct value across >= 10 rows so that
        the constant-column defect (all rows same code) is caught by this test.
        """
        n = 10
        values = [f"src_{i}" for i in range(n)]
        out = _run(
            "diag",
            values,
            (("code_set", "icd10"), ("mode", "gen")),
        )
        distinct = len(set(out))
        assert distinct > 1, (
            f"gen mode produced a constant column: all {n} rows are {out[0]!r}. "
            "gen mode must vary per row (distinct count must be > 1)."
        )

    def test_code_set_null_passthrough(self) -> None:
        """Null values in the input column pass through as null."""
        out = _run(
            "diag",
            ["I10", None, "E11.9"],
            (("code_set", "icd10"), ("mode", "mask")),
        )
        assert out[1] is None, f"null row should stay null, got {out[1]!r}"

    def test_code_set_deterministic_mask_same_seed(self) -> None:
        """Same plan + same table -> same output (determinism through the adapter)."""
        values = ["I10", "E11.9", "F32.9"]
        out1 = _run("diag", values, (("code_set", "icd10"), ("mode", "mask")))
        out2 = _run("diag", values, (("code_set", "icd10"), ("mode", "mask")))
        assert out1 == out2, "Adapter-level mask mode must be deterministic."

    def test_supports_strategy_code_set(self) -> None:
        """SCALAR_HANDLERS must advertise code_set as a supported strategy."""
        adapter = PandasExecutionAdapter()
        assert adapter.supports_strategy("code_set") is True


class TestCodeSetChapterPreserveUnknownChapter:
    """H2: chapter_preserve with an unknown input chapter must fail closed."""

    def test_unknown_chapter_raises_via_adapter(self) -> None:
        """When chapter_preserve=True and the input's chapter is not in the
        corpus, the adapter run must raise PlanCompileError (fail-closed).
        Falls-back to full-corpus was the old (wrong) behavior."""
        import pathlib
        import tempfile

        import pyarrow.parquet as pq
        from decoy_engine.plan._errors import PlanCompileError

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "two_chapters.parquet"
            tbl = pa.table(
                {
                    "code": pa.array(["A01", "A02", "B01", "B02"], type=pa.string()),
                    "chapter": pa.array(["A", "A", "B", "B"], type=pa.string()),
                }
            )
            pq.write_table(tbl, str(path))

            with pytest.raises(PlanCompileError, match="chapter"):
                _run(
                    "code_col",
                    ["U07.1"],  # U chapter absent from corpus
                    (
                        ("code_set", "two_chapters"),
                        ("chapter_preserve", True),
                        ("corpus_source", f"customer:{path}"),
                        ("mode", "mask"),
                    ),
                )
