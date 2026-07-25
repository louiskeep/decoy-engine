"""engine-v2 S10 slice 4b: fk_validity + determinism_sample scans."""

from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa

from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import (
    OrphanPolicy,
    RelationshipEdge,
    RelationshipGraph,
)
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.validation.post._checks._determinism_sample import run_determinism_sample
from decoy_engine.validation.post._checks._fk_validity import run_fk_validity
from decoy_engine.validation.post._scan import ScanContext

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())


def _edge(policy: OrphanPolicy) -> RelationshipEdge:
    return RelationshipEdge(
        parent_table="customers",
        parent_columns=("customer_id",),
        child_table="orders",
        child_columns=("customer_id",),
        namespace="cust",
        orphan_policy=policy,
    )


def _fk_ctx(policy: OrphanPolicy, child_ids: list[str]) -> ScanContext:
    outputs = {
        "customers": pa.table({"customer_id": ["MA", "MB"]}),
        "orders": pa.table({"customer_id": child_ids}),
    }
    return ScanContext(
        plan=SimpleNamespace(seed_envelope=SimpleNamespace(per_table=())),  # type: ignore[arg-type]
        outputs=outputs,
        sources=outputs,
        profile=SimpleNamespace(tables=()),  # type: ignore[arg-type]
        registry=_REG,
        relationship_graph=RelationshipGraph(edges=(_edge(policy),), ordering=()),
        namespace_registry=_NS,
    )


class TestFkValidity:
    def test_all_resolve_passes(self) -> None:
        outcome = run_fk_validity(_fk_ctx(OrphanPolicy.FAIL, ["MA", "MB", "MA"]))
        assert outcome.failed is False
        report = outcome.fk_validity["customers.customer_id -> orders.customer_id"]
        assert report.parent_match_count == 3
        assert report.orphan_count == 0
        assert report.orphan_policy == "fail"

    def test_fail_policy_orphan_hard_fails(self) -> None:
        outcome = run_fk_validity(_fk_ctx(OrphanPolicy.FAIL, ["MA", "ORPHAN"]))
        assert outcome.failed is True
        report = outcome.fk_validity["customers.customer_id -> orders.customer_id"]
        assert report.orphan_count == 1 and report.invalid_count == 1

    def test_warn_policy_orphan_warns_not_fails(self) -> None:
        outcome = run_fk_validity(_fk_ctx(OrphanPolicy.WARN, ["MA", "ORPHAN"]))
        assert outcome.failed is False
        assert any(w.code == "orphan_fk" for w in outcome.warnings)
        assert outcome.fk_validity["customers.customer_id -> orders.customer_id"].invalid_count == 0

    def test_preserve_policy_orphan_passes(self) -> None:
        outcome = run_fk_validity(_fk_ctx(OrphanPolicy.PRESERVE, ["MA", "orphan-src"]))
        assert outcome.failed is False
        assert outcome.warnings == ()
        assert outcome.fk_validity["customers.customer_id -> orders.customer_id"].orphan_count == 1


def _det_seed(*, deterministic: bool) -> ColumnSeed:
    return ColumnSeed(
        namespace="ns",
        strategy="hash",
        provider="person_email",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=deterministic,
        provider_config=(),
        coherent_with=(),
    )


def _det_ctx(*, deterministic: bool, source: list[str], output: list[str]) -> ScanContext:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x00" * 8,
            per_table=(
                (
                    "t",
                    TableSeed(
                        per_column=(("c", _det_seed(deterministic=deterministic)),), per_group=()
                    ),
                ),
            ),
        )
    )
    return ScanContext(
        plan=plan,  # type: ignore[arg-type]
        outputs={"t": pa.table({"c": output})},
        sources={"t": pa.table({"c": source})},
        profile=SimpleNamespace(tables=()),  # type: ignore[arg-type]
        registry=_REG,
        relationship_graph=RelationshipGraph(edges=(), ordering=()),
        namespace_registry=_NS,
    )


class TestDeterminismSample:
    def test_same_source_same_output_passes(self) -> None:
        ctx = _det_ctx(deterministic=True, source=["x", "y", "x"], output=["MX", "MY", "MX"])
        assert run_determinism_sample(ctx).failed is False

    def test_same_source_different_output_fails(self) -> None:
        ctx = _det_ctx(deterministic=True, source=["x", "y", "x"], output=["MX", "MY", "MZ"])
        assert run_determinism_sample(ctx).failed is True

    def test_non_deterministic_column_skipped(self) -> None:
        # deterministic=False: the same-source/different-output split is allowed.
        ctx = _det_ctx(deterministic=False, source=["x", "x"], output=["MX", "MZ"])
        assert run_determinism_sample(ctx).failed is False
