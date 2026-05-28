"""engine-v2 S10 slice 3a: structural scans (pk_uniqueness + cardinality).

These read the masked output + the plan/profile only (no source comparison).
Tested directly on the scan callables and through the runner's merge + skip path.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pyarrow as pa

from decoy_engine.execution import ExecutionResult
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.profile import ColumnProfile, Profile, TableProfile
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.validation.post import PostValidationRunner, QualitySummary
from decoy_engine.validation.post._checks._cardinality import run_cardinality
from decoy_engine.validation.post._checks._pk_uniqueness import run_pk_uniqueness
from decoy_engine.validation.post._scan import ScanContext

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())


def _seed(cardinality_mode: str = "reuse") -> ColumnSeed:
    return ColumnSeed(
        namespace="ns",
        strategy="faker",
        provider="person_email",
        backend_type="faker",
        backend_version="v",
        cardinality_mode=cardinality_mode,
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _cp(name: str, *, declared_pk: bool = False, distinct: int | None = None) -> ColumnProfile:
    return ColumnProfile(
        name=name,
        dtype="object",
        row_count=3,
        null_count=0,
        distinct_count=distinct,
        sampled=False,
        is_candidate_key_sampled=False,
        declared_pk=declared_pk,
        is_fk=False,
        fk_target=None,
        pii_class=None,
    )


def _ctx(
    *,
    outputs: dict[str, pa.Table],
    profile_cols: tuple[ColumnProfile, ...],
    plan_cols: tuple[tuple[str, ColumnSeed], ...],
    sources: dict[str, pa.Table] | None = None,
) -> ScanContext:
    profile = Profile(
        schema_version=1,
        tables=(TableProfile(name="t", row_count=3, columns=profile_cols),),
        relationships=(),
        profiled_at=datetime(2026, 5, 28),
        decoy_engine_version="0.1.0",
    )
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x00" * 8,
            per_table=(("t", TableSeed(per_column=plan_cols, per_group=())),),
        )
    )
    return ScanContext(
        plan=plan,  # type: ignore[arg-type]
        outputs=outputs,
        sources=sources if sources is not None else outputs,
        profile=profile,
        registry=_REG,
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
    )


class TestPkUniqueness:
    def test_all_distinct_passes(self) -> None:
        ctx = _ctx(
            outputs={"t": pa.table({"id": ["a", "b", "c"]})},
            profile_cols=(_cp("id", declared_pk=True),),
            plan_cols=(("id", _seed()),),
        )
        outcome = run_pk_uniqueness(ctx)
        assert outcome.failed is False
        assert outcome.duplicate_counts["t.id"] == 0

    def test_duplicate_pk_fails(self) -> None:
        ctx = _ctx(
            outputs={"t": pa.table({"id": ["a", "b", "a"]})},
            profile_cols=(_cp("id", declared_pk=True),),
            plan_cols=(("id", _seed()),),
        )
        outcome = run_pk_uniqueness(ctx)
        assert outcome.failed is True
        assert outcome.duplicate_counts["t.id"] == 1

    def test_non_pk_columns_ignored(self) -> None:
        ctx = _ctx(
            outputs={"t": pa.table({"v": ["x", "x", "x"]})},  # repeats, but not a PK
            profile_cols=(_cp("v", declared_pk=False),),
            plan_cols=(("v", _seed()),),
        )
        outcome = run_pk_uniqueness(ctx)
        assert outcome.failed is False
        assert outcome.duplicate_counts == {}


class TestCardinality:
    def test_unique_mode_all_distinct_passes(self) -> None:
        ctx = _ctx(
            outputs={"t": pa.table({"id": ["a", "b", "c"]})},
            profile_cols=(_cp("id", distinct=3),),
            plan_cols=(("id", _seed("unique")),),
        )
        outcome = run_cardinality(ctx)
        assert outcome.failed is False
        assert outcome.distinct_counts["t.id"].output_distinct == 3
        assert outcome.distinct_counts["t.id"].source_distinct == 3

    def test_unique_mode_repeat_fails(self) -> None:
        ctx = _ctx(
            outputs={"t": pa.table({"id": ["a", "b", "a"]})},
            profile_cols=(_cp("id", distinct=3),),
            plan_cols=(("id", _seed("unique")),),
        )
        outcome = run_cardinality(ctx)
        assert outcome.failed is True

    def test_match_source_deviation_warns_not_fails(self) -> None:
        ctx = _ctx(
            outputs={"t": pa.table({"g": ["a", "b", "c"]})},  # 3 distinct
            profile_cols=(_cp("g", distinct=2),),  # source had 2
            plan_cols=(("g", _seed("match_source_cardinality")),),
        )
        outcome = run_cardinality(ctx)
        assert outcome.failed is False
        codes = [w.code for w in outcome.warnings]
        assert "cardinality_match_deviation" in codes


class TestRunnerMergeAndSkip:
    def _runner_summary(self, config: dict[str, Any]) -> QualitySummary | None:
        outputs = {"t": pa.table({"id": ["a", "b", "a"]})}  # duplicate PK + unique violation
        profile = Profile(
            schema_version=1,
            tables=(TableProfile(name="t", row_count=3, columns=(_cp("id", declared_pk=True),)),),
            relationships=(),
            profiled_at=datetime(2026, 5, 28),
            decoy_engine_version="0.1.0",
        )
        plan = SimpleNamespace(
            seed_envelope=SeedEnvelope(
                job_seed=b"\x00" * 8,
                per_table=(("t", TableSeed(per_column=(("id", _seed("unique")),), per_group=())),),
            )
        )
        # Distinct source so the leakage scan does not fire (this test is about
        # pk_uniqueness + cardinality populating failed_checks).
        sources = {"t": pa.table({"id": ["x", "y", "z"]})}
        return PostValidationRunner().run(
            plan=plan,  # type: ignore[arg-type]
            execution_result=ExecutionResult(outputs=outputs, warnings=()),
            sources=sources,
            profile=profile,
            registry=_REG,
            relationship_graph=_GRAPH,
            namespace_registry=_NS,
            config=config,
        )

    def test_merge_populates_failed_checks(self) -> None:
        summary = self._runner_summary({"post_validation": True})
        assert summary is not None
        assert summary.duplicate_counts["t.id"] == 1  # from pk_uniqueness
        assert "t.id" in summary.distinct_counts  # from cardinality
        assert set(summary.failed_checks) == {"pk_uniqueness", "cardinality"}

    def test_skip_list_excludes_a_scan(self) -> None:
        summary = self._runner_summary(
            {"post_validation": True, "post_validation_skip": ["cardinality"]}
        )
        assert summary is not None
        assert "cardinality" not in summary.failed_checks
        assert "pk_uniqueness" in summary.failed_checks  # still runs
        assert summary.distinct_counts == {}  # cardinality skipped -> no distinct counts
