"""engine-v2 S10 slice 4a: source-comparison scans (null_audit, leakage, sampled_values)."""

from __future__ import annotations

from types import SimpleNamespace

import pyarrow as pa

from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry
from decoy_engine.validation.post._checks._leakage import run_leakage
from decoy_engine.validation.post._checks._null_audit import run_null_audit
from decoy_engine.validation.post._checks._sampled_values import run_sampled_values
from decoy_engine.validation.post._scan import ScanContext

_REG = get_default_registry()
_GRAPH = RelationshipGraph(edges=(), ordering=())
_NS = NamespaceRegistry(bindings=())


def _seed(strategy: str) -> ColumnSeed:
    return ColumnSeed(
        namespace="ns",
        strategy=strategy,
        provider="person_email",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _ctx(
    plan_cols: tuple[tuple[str, ColumnSeed], ...],
    output: pa.Table,
    source: pa.Table,
    *,
    sample_size: int = 100,
) -> ScanContext:
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x00" * 8,
            per_table=(("t", TableSeed(per_column=plan_cols, per_group=())),),
        )
    )
    return ScanContext(
        plan=plan,  # type: ignore[arg-type]
        outputs={"t": output},
        sources={"t": source},
        profile=SimpleNamespace(tables=()),  # type: ignore[arg-type]
        registry=_REG,
        relationship_graph=_GRAPH,
        namespace_registry=_NS,
        sample_size=sample_size,
    )


class TestNullAudit:
    def test_matching_null_positions_pass(self) -> None:
        ctx = _ctx(
            (("a", _seed("hash")),),
            output=pa.table({"a": ["MX", None, "MY"]}),
            source=pa.table({"a": ["x", None, "y"]}),
        )
        outcome = run_null_audit(ctx)
        assert outcome.failed is False
        assert outcome.null_counts["t.a"].source_nulls == 1
        assert outcome.null_counts["t.a"].output_nulls == 1

    def test_moved_null_fails(self) -> None:
        ctx = _ctx(
            (("a", _seed("hash")),),
            output=pa.table({"a": ["MX", "MY", "MZ"]}),  # no null where source had one
            source=pa.table({"a": ["x", None, "y"]}),
        )
        assert run_null_audit(ctx).failed is True


class TestLeakage:
    def test_no_overlap_passes(self) -> None:
        ctx = _ctx(
            (("a", _seed("hash")),),
            output=pa.table({"a": ["MX", "MY"]}),
            source=pa.table({"a": ["x", "y"]}),
        )
        assert run_leakage(ctx).failed is False

    def test_leaked_source_value_hard_fails_count_only(self) -> None:
        ctx = _ctx(
            (("a", _seed("hash")),),
            output=pa.table({"a": ["x", "MY"]}),  # "x" survived from source
            source=pa.table({"a": ["x", "y"]}),
        )
        outcome = run_leakage(ctx)
        assert outcome.failed is True
        assert outcome.warnings[0].code == "source_value_leak"
        assert outcome.warnings[0].detail["leaked_count"] == 1
        assert "x" not in str(outcome.warnings[0].detail)  # never echo the leaked value

    def test_passthrough_excluded(self) -> None:
        ctx = _ctx(
            (("a", _seed("passthrough")),),
            output=pa.table({"a": ["x", "y"]}),  # equals source, but passthrough
            source=pa.table({"a": ["x", "y"]}),
        )
        assert run_leakage(ctx).failed is False


class TestLeakageValueReuse:
    """B1: value-reuse strategies (shuffle/categorical) re-emit source values by
    design; set-membership must NOT hard-fail them. A positional fixed-point is a
    warning, never a hard fail."""

    def test_shuffle_permutation_does_not_hard_fail(self) -> None:
        # Full derangement: every output value is a source value, but no fixed point.
        ctx = _ctx(
            (("a", _seed("shuffle")),),
            output=pa.table({"a": ["b", "c", "a"]}),
            source=pa.table({"a": ["a", "b", "c"]}),
        )
        outcome = run_leakage(ctx)
        assert outcome.failed is False  # set-membership would have flagged all 3
        assert outcome.warnings == ()  # no fixed points

    def test_shuffle_fixed_point_warns_not_fails(self) -> None:
        ctx = _ctx(
            (("a", _seed("shuffle")),),
            output=pa.table({"a": ["a", "c", "b"]}),  # index 0 stayed in place
            source=pa.table({"a": ["a", "b", "c"]}),
        )
        outcome = run_leakage(ctx)
        assert outcome.failed is False
        assert outcome.warnings[0].code == "value_reuse_fixed_point"
        assert outcome.warnings[0].detail["fixed_point_count"] == 1

    def test_categorical_reuse_does_not_hard_fail(self) -> None:
        ctx = _ctx(
            (("g", _seed("categorical")),),
            output=pa.table({"g": ["F", "M", "F"]}),  # categories overlap the source
            source=pa.table({"g": ["M", "F", "M"]}),
        )
        assert run_leakage(ctx).failed is False


class TestSampledValues:
    def test_samples_synthetic_output_excludes_passthrough(self) -> None:
        ctx = _ctx(
            (("a", _seed("hash")), ("p", _seed("passthrough"))),
            output=pa.table({"a": ["MX", "MY"], "p": ["x", "y"]}),
            source=pa.table({"a": ["x", "y"], "p": ["x", "y"]}),
        )
        outcome = run_sampled_values(ctx)
        assert outcome.failed is False
        assert outcome.sampled_values["t.a"] == ["MX", "MY"]
        assert "t.p" not in outcome.sampled_values  # passthrough excluded

    def test_sample_size_caps_rows(self) -> None:
        ctx = _ctx(
            (("a", _seed("hash")),),
            output=pa.table({"a": [f"m{i}" for i in range(10)]}),
            source=pa.table({"a": [f"s{i}" for i in range(10)]}),
            sample_size=3,
        )
        assert len(run_sampled_values(ctx).sampled_values["t.a"]) == 3
