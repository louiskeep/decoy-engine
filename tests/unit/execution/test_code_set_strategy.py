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
from decoy_engine.plan._errors import PlanCompileError
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
    namespace: str | None = None,
    when: str | None = None,
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider="x_nobackend",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
        when=when,
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


def _run(
    column: str,
    values: list[str | None],
    provider_config: tuple,
    namespace: str | None = None,
) -> list:
    table = pa.table({column: pa.array(values, type=pa.string())})
    plan = _plan(column, _col("code_set", provider_config=provider_config, namespace=namespace))
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
            namespace="test.diag",
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
            namespace="test.diag",
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
    """H2: chapter_preserve with an unknown input chapter must fail closed.

    Sprint 2 honesty pack (2026-07-04, S6, intentional hard cutover): fail
    closed is now a PIPELINE-level guarantee (RowErrorsFailedError from
    `run_pipeline`, unless quarantine is enabled with the `mask_error`
    trigger), not an ADAPTER-level raise. Calling the adapter directly (as
    this test does, with no pipeline/quarantine machinery around it) no
    longer raises PlanCompileError -- it records a RowError (trigger
    "mask_error") and keeps the original value in the frame. See
    `tests/unit/execution/test_code_set_mask_error.py` for the handler-level
    coverage and `tests/integration/test_row_errors_e2e.py` for the
    pipeline-level fail-closed / quarantine coverage.
    """

    def test_unknown_chapter_records_mask_error_via_adapter(self) -> None:
        import pathlib
        import tempfile

        import pyarrow.parquet as pq

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "two_chapters.parquet"
            tbl = pa.table(
                {
                    "code": pa.array(["A01", "A02", "B01", "B02"], type=pa.string()),
                    "chapter": pa.array(["A", "A", "B", "B"], type=pa.string()),
                }
            )
            pq.write_table(tbl, str(path))

            table = pa.table({"code_col": pa.array(["U07.1"], type=pa.string())})
            provider_config = (
                ("code_set", "two_chapters"),
                ("chapter_preserve", True),
                ("corpus_source", f"customer:{path}"),
                ("mode", "mask"),
            )
            plan = _plan("code_col", _col("code_set", provider_config=provider_config))
            result = PandasExecutionAdapter().run_single(
                plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
            )

            assert len(result.row_errors) == 1
            rec = result.row_errors[0]
            assert rec.trigger == "mask_error"
            assert rec.column == "code_col"
            # The original (unmasked) value stays in the output; the
            # pipeline layer, not the adapter, decides its fate (D8).
            assert result.output.column("code_col").to_pylist() == ["U07.1"]


class TestCodeSetProvenanceEvidence:
    """HC-1 slice 1 item 3: corpus provenance surfaced into
    ExecutionResult.quality_metrics['code_set_corpora'] via the adapter."""

    def test_quality_metrics_code_set_corpora_present(self) -> None:
        out = PandasExecutionAdapter().run_single(
            _plan("diag", _col("code_set", provider_config=(("code_set", "icd10"),))),
            pa.table({"diag": pa.array(["I10", "E11.9"], type=pa.string())}),
            registry=_REG,
            relationship_graph=_GRAPH,
            namespace_registry=_NS,
        )
        corpora = out.quality_metrics.get("code_set_corpora")
        assert corpora is not None and len(corpora) == 1
        entry = corpora[0]
        assert entry["code_set"] == "icd10"
        assert entry["source_version"] == "FY2024"
        assert entry["effective_date"] == "2023-10-01"
        assert entry["is_seed"] is True
        assert entry["row_count"] > 0
        # Counts + identifiers only -- no raw codes leak into evidence.
        assert "codes" not in entry
        assert "rows" not in entry

    def test_quality_metrics_omits_code_set_corpora_when_no_code_set_columns(self) -> None:
        """A job with no code_set columns must not gain a stray, empty
        code_set_corpora key (additive, zero-impact on unrelated jobs)."""
        out = PandasExecutionAdapter().run_single(
            _plan("name", _col("passthrough")),
            pa.table({"name": pa.array(["a", "b"], type=pa.string())}),
            registry=_REG,
            relationship_graph=_GRAPH,
            namespace_registry=_NS,
        )
        assert "code_set_corpora" not in out.quality_metrics

    def test_quality_metrics_omits_code_set_corpora_when_column_all_null(self) -> None:
        """NIT-1 remediation: an all-null code_set column masks ZERO values
        (every row short-circuits on the null check before `apply_code_set`
        ever runs), so it must not report its corpus as "used" -- even though
        the corpus is still eagerly loaded + validated for that column (a
        corpus-level defect, e.g. a missing corpus file, must still fail
        closed for an all-null column; only the EVIDENCE stamp is withheld)."""
        out = PandasExecutionAdapter().run_single(
            _plan("diag", _col("code_set", provider_config=(("code_set", "icd10"),))),
            pa.table({"diag": pa.array([None, None], type=pa.string())}),
            registry=_REG,
            relationship_graph=_GRAPH,
            namespace_registry=_NS,
        )
        assert "code_set_corpora" not in out.quality_metrics

    def test_quality_metrics_keys_evidence_by_table_for_same_named_columns(self) -> None:
        """Codex P2 MULTI-TABLE EVIDENCE COLLISION remediation: two tables
        that each declare a SAME-NAMED code_set column ("code") bound to
        DIFFERENT corpora must both surface their own evidence entry. Before
        this fix, the sink was keyed by bare column name, so the second
        table's stamp silently overwrote the first's and one table's audit
        provenance was dropped."""
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=_SEED,
                per_table=(
                    (
                        "table_a",
                        TableSeed(
                            per_column=(
                                (
                                    "code",
                                    _col(
                                        "code_set",
                                        provider_config=(("code_set", "icd10"),),
                                    ),
                                ),
                            ),
                            per_group=(),
                        ),
                    ),
                    (
                        "table_b",
                        TableSeed(
                            per_column=(
                                (
                                    "code",
                                    _col(
                                        "code_set",
                                        provider_config=(("code_set", "mcc"),),
                                    ),
                                ),
                            ),
                            per_group=(),
                        ),
                    ),
                ),
            )
        )
        sources = {
            "table_a": pa.table({"code": pa.array(["I10", "E11.9"], type=pa.string())}),
            "table_b": pa.table({"code": pa.array(["alpha", "beta"], type=pa.string())}),
        }
        out = PandasExecutionAdapter().run(
            plan, sources, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        corpora = out.quality_metrics.get("code_set_corpora")
        assert corpora is not None and len(corpora) == 2, (
            f"expected one evidence entry per (table, column), got {corpora!r}"
        )
        by_table_column = {(e["table"], e["column"]): e["code_set"] for e in corpora}
        assert by_table_column == {
            ("table_a", "code"): "icd10",
            ("table_b", "code"): "mcc",
        }


class TestCodeSetPlanYamlProvenance:
    """Codex P1 PROVENANCE IS EVIDENCE, NOT PLAN STATE remediation.

    HC-1 slice 1 originally stamped `code_set_provenance` onto the Plan YAML
    from whatever corpus happened to be on disk at `plan_to_yaml` time. That
    made the plan artifact non-deterministic (a swapped or absent corpus
    silently changed or dropped the block) and it never round-tripped
    (`_column_seed_from_dict` ignores the key). The HC-1 spec requires
    provenance "surfaced in output/evidence", not in the reproducible plan
    config, so `_column_seed_to_dict` must NEVER emit this key -- provenance
    lives only in execution evidence
    (`ExecutionResult.quality_metrics['code_set_corpora']`, from the
    actually-loaded corpus at run time; see TestCodeSetCorpusEvidence
    above)."""

    def test_column_seed_to_dict_never_stamps_provenance_for_a_valid_shipped_corpus(
        self,
    ) -> None:
        """Even for a real, resolvable, fully-provenanced shipped corpus
        (icd10), the plan-YAML dict must carry no code_set_provenance key."""
        from decoy_engine.plan._serialize import _column_seed_to_dict

        cs = _col("code_set", provider_config=(("code_set", "icd10"),))
        out = _column_seed_to_dict(cs)
        assert "code_set_provenance" not in out

    def test_column_seed_to_dict_never_stamps_provenance_for_non_code_set_strategy(
        self,
    ) -> None:
        from decoy_engine.plan._serialize import _column_seed_to_dict

        cs = _col("passthrough")
        out = _column_seed_to_dict(cs)
        assert "code_set_provenance" not in out

    def test_column_seed_to_dict_never_stamps_provenance_for_unreachable_customer_corpus(
        self,
    ) -> None:
        """A code_set column referencing a customer corpus that does not
        exist in THIS environment must not raise plan_to_yaml, and (like
        every other code_set column now) must not gain the key either."""
        from decoy_engine.plan._serialize import _column_seed_to_dict

        cs = _col(
            "code_set",
            provider_config=(
                ("code_set", "cpt_local"),
                ("corpus_source", "customer:/no/such/file.parquet"),
            ),
        )
        out = _column_seed_to_dict(cs)  # must not raise
        assert "code_set_provenance" not in out


class TestCodeSetWhenGateFailClosed:
    """Codex P2 FAIL-CLOSED VALIDATION BYPASSED BY A ZERO-MATCH `when` GATE
    remediation: `run_with_when_gate` used to short-circuit to a passthrough
    (never calling `CodeSetHandler.run`, and so never triggering its eager
    corpus load/validation) whenever a `when:`-gated code_set column's
    predicate matched zero rows. A column referencing a missing/invalid
    corpus therefore succeeded silently as long as its gate happened to
    match nothing -- violating fail-closed. `CodeSetHandler.preflight` closes
    this: the corpus is validated unconditionally, even under a zero-match
    gate."""

    def test_zero_match_when_gate_still_fails_closed_on_missing_corpus(self) -> None:
        """A code_set column whose `when:` predicate matches NO rows, and
        whose corpus_source points at a customer path that does not exist,
        must still raise -- not silently pass through the raw values."""
        table = pa.table({"diag": pa.array(["I10", "E11.9"], type=pa.string())})
        plan = _plan(
            "diag",
            _col(
                "code_set",
                provider_config=(
                    ("code_set", "missing_corpus"),
                    ("corpus_source", "customer:/no/such/file.parquet"),
                ),
                # Never true for either row: the gate matches zero rows.
                when="diag == 'ZZZ_NEVER_MATCHES'",
            ),
        )
        with pytest.raises(PlanCompileError) as excinfo:
            PandasExecutionAdapter().run_single(
                plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
            )
        assert excinfo.value.code == "code_set_corpus_path_not_found", (
            f"expected a fail-closed corpus error, got {excinfo.value.code!r}. "
            "A zero-match when: gate must not let a missing corpus succeed silently."
        )

    def test_zero_match_when_gate_does_not_stamp_evidence(self) -> None:
        """The other side of NIT-1: preflight validates but must NOT stamp
        `code_set_corpora` evidence when the gate matches zero rows -- no
        value was actually masked, so there is nothing to attest to."""
        table = pa.table({"diag": pa.array(["I10", "E11.9"], type=pa.string())})
        plan = _plan(
            "diag",
            _col(
                "code_set",
                provider_config=(("code_set", "icd10"),),
                when="diag == 'ZZZ_NEVER_MATCHES'",
            ),
        )
        result = PandasExecutionAdapter().run_single(
            plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )
        # Passthrough: values are untouched (gate matched nothing).
        assert result.output.column("diag").to_pylist() == ["I10", "E11.9"]
        assert result.quality_metrics.get("code_set_corpora") is None, (
            "preflight must validate the corpus without stamping evidence "
            "for a column that never actually masked a value."
        )


class TestCodeSetPinnedRecordAcrossMidRunCorpusReplace:
    """Codex round-6 P2 MASKING/EVIDENCE VERSION DIVERGENCE remediation.

    Pre-fix, `CodeSetHandler.run` stamped evidence via ONE `describe_loaded_
    corpus` call while each per-value `apply_code_set` call independently
    re-resolved the corpus from the module cache. The customer cache
    invalidates on (path, mtime_ns, ctime_ns, size), so a customer corpus
    file REPLACED mid-run (between the handler's initial load and a later
    value) could make later values mask off a DIFFERENT corpus version than
    the one evidence already reported -- masking output and evidence
    silently disagreeing about which corpus was actually used. The fix pins
    ONE `_CorpusRecord` per column/run (`resolve_corpus_record`, called once
    in `CodeSetHandler.run`) and threads it into both the evidence stamp and
    every per-value `apply_code_set` call, so a mid-run file replacement can
    no longer be observed by either -- they draw from the same in-memory
    record regardless of what is on disk after the first load."""

    @staticmethod
    def _write_corpus(path: Any, codes: list[str]) -> None:
        import pyarrow.parquet as pq

        tbl = pa.table({"code": pa.array(codes, type=pa.string())})
        pq.write_table(tbl, str(path))

    def test_mid_run_corpus_replace_does_not_diverge_masking_from_evidence(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from decoy_engine.execution._strategies import _code_set as code_set_module

        path = tmp_path / "swap.parquet"
        v1_codes = ["A01", "A02", "A03"]
        v2_codes = ["B01", "B02", "B03", "B04", "B05"]
        self._write_corpus(path, v1_codes)

        values = ["A01", "A02", "A03"]
        table = pa.table({"diag": pa.array(values, type=pa.string())})
        plan = _plan(
            "diag",
            _col(
                "code_set",
                provider_config=(
                    ("code_set", "swap_corpus"),
                    ("corpus_source", f"customer:{path}"),
                ),
            ),
        )

        real_apply = code_set_module.apply_code_set
        state = {"swapped": False}

        def _apply_then_swap_disk(*args: Any, **kwargs: Any) -> str:
            # Real masking call first (this is what a pinned record must be
            # immune to what happens on disk AFTER it was resolved), then
            # replace the corpus FILE on disk -- a different row count means
            # a different file size, so the customer cache key changes
            # regardless of filesystem timestamp resolution. Only swap once:
            # the point is "a replace happened mid-run," not "every call
            # races a fresh replace."
            result = real_apply(*args, **kwargs)
            if not state["swapped"]:
                state["swapped"] = True
                self._write_corpus(path, v2_codes)
            return result

        monkeypatch.setattr(code_set_module, "apply_code_set", _apply_then_swap_disk)

        result = PandasExecutionAdapter().run_single(
            plan, table, registry=_REG, relationship_graph=_GRAPH, namespace_registry=_NS
        )

        out = result.output.column("diag").to_pylist()
        for i, v in enumerate(out):
            assert v in v1_codes, (
                f"row {i}: masked output {v!r} is not a v1 corpus code {v1_codes!r} -- "
                "a later value picked up the mid-run file replacement instead of the "
                "SAME pinned record the earlier values (and evidence) used."
            )

        corpora = result.quality_metrics.get("code_set_corpora")
        assert corpora is not None and len(corpora) == 1, (
            f"expected exactly one code_set_corpora evidence entry, got {corpora!r}"
        )
        assert corpora[0]["row_count"] == len(v1_codes), (
            f"evidence reported row_count={corpora[0]['row_count']!r}, expected "
            f"{len(v1_codes)} (the v1 corpus actually used to mask every value) -- "
            "evidence must never report a corpus version different from the one "
            "masking actually used."
        )
