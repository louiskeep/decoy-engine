"""Phase 5 Track B acceptance: native deterministic categorical.

Byte-identity (value AND Arrow FIELD TYPE) vs the pandas oracle is the merge
gate, proven at two boundaries: the shadow coordinator's assembled output
(`run_shadow_and_oracle` + `assert_shadow_matches_oracle`, which hard-compares
`schema.field(...).type`), and the production unified-slice `ExecutionResult`
(flag-off vs flag-on). The seam is proven both ways: the FULL-FRAME route
EXECUTES native categorical (positive route/kernel evidence), and the CHUNKED
route DECLINES it to the oracle (v1 scope). The determinism gate is proven at
BOTH admission boundaries plus the runtime assertion.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import run_pipeline
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._dispatch import plan_native_route
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.execution.native._requirements import is_deterministic_categorical
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import ExecutionBinding, KeyBinding
from decoy_engine.execution.physical._shadow_diff_codes import ShadowDifference
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.keyprovider import SecretKeyProvider
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_every_node_bound,
    assert_route_evidence_matches_plan,
    assert_shadow_matches_oracle,
    build_config,
    run_shadow_and_oracle,
    write_read_only_fixture,
)

_MASK_KEY = bytes(range(32))


def _kp() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


def _cat_column(pc: dict[str, Any], *, deterministic: bool = True, namespace: str = "ns") -> dict:
    col: dict[str, Any] = {
        "name": "c",
        "strategy": "categorical",
        "namespace": namespace,
        "provider_config": pc,
    }
    if deterministic:
        col["deterministic"] = True
    return col


# ── Byte-identity: coordinator vs oracle (value + Arrow field type) ──

_UNI = ["red", "green", "blue"]

_CASES = [
    ("uniform_populated", ["a", "b", "c", "d", "e"], {"categories": _UNI}),
    ("uniform_null_dup_unicode", ["a", None, "a", "café", "日本"], {"categories": ["α", "β", "γ"]}),  # noqa: RUF001
    ("uniform_single", ["x", "y", "z"], {"categories": ["only"]}),
    ("uniform_all_null", [None, None, None], {"categories": _UNI}),
    ("uniform_empty", [], {"categories": _UNI}),
    (
        "weighted_populated",
        ["a", "b", "c", "d", "e"],
        {"categories": _UNI, "weights": [1.0, 2.0, 3.0]},
    ),
    ("weighted_equal", ["p", "q", "r", "s"], {"categories": ["X", "Y"], "weights": [1.0, 1.0]}),
    (
        "weighted_zero_band",
        [f"v{i}" for i in range(40)],
        {"categories": _UNI, "weights": [1.0, 0.0, 1.0]},
    ),
    (
        "weighted_skewed",
        [f"v{i}" for i in range(40)],
        {"categories": ["A", "B"], "weights": [0.99, 0.01]},
    ),
    ("weighted_null", ["a", None, "c", None], {"categories": _UNI, "weights": [3.0, 1.0, 1.0]}),
    ("weighted_all_null", [None, None], {"categories": ["X", "Y"], "weights": [1.0, 2.0]}),
    ("weighted_empty", [], {"categories": ["X", "Y"], "weights": [1.0, 2.0]}),
]


@pytest.mark.parametrize("case", _CASES, ids=[c[0] for c in _CASES])
def test_coordinator_matches_oracle_byte_identical(tmp_path: Path, case) -> None:
    _label, values, pc = case
    source = pa.table({"c": pa.array(values, type=pa.string())})
    write_read_only_fixture(tmp_path, source, "cat")
    config = build_config(tmp_path, "t", tmp_path / "cat.parquet", [_cat_column(pc)])
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
    assert_every_node_bound(run.plan)
    assert_shadow_matches_oracle(run)  # value + null + order + row-count + SCHEMA (field type)
    assert_route_evidence_matches_plan(run)


# ── Seam proof: full-frame EXECUTES native categorical ──────────────


def test_full_frame_executes_native_categorical(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", None, "d"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "cat")
    config = build_config(
        tmp_path, "t", tmp_path / "cat.parquet", [_cat_column({"categories": _UNI})]
    )
    run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
    (evidence,) = run.shadow.route_evidence.values()
    assert evidence.actual_operator == "native_categorical"
    assert evidence.executed is True
    # Positive index-kernel-call evidence, never inferred from success alone.
    assert evidence.compiled_kernel_executed is True


# ── Seam proof: chunked route DECLINES categorical to the oracle ────


def test_chunked_route_declines_categorical(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["x", "y", "z"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "cat")
    config = build_config(
        tmp_path, "t", tmp_path / "cat.parquet", [_cat_column({"categories": _UNI})]
    )
    profile = first_chunk_profile(source, table="t", engine_version=ENGINE_VERSION)
    preflight = plan_native_route(
        config, profile, table="t", engine_version=ENGINE_VERSION, first_schema=source.schema
    )
    assert preflight.evidence.native_admitted is False
    assert "categorical_not_native_chunked_route:c" in (preflight.evidence.reroute_reason or "")


# ── Determinism gate: decline at BOTH admission boundaries ──────────


def _compile_plan_for(config: dict, source: pa.Table):
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=ENGINE_VERSION)
    return compile_physical_plan(inputs)


def test_unseeded_categorical_declines_on_native_route_config_query(tmp_path: Path) -> None:
    """Boundary 1: the config-only native-route eligibility query."""
    config = build_config(
        tmp_path,
        "t",
        tmp_path / "x.parquet",
        [_cat_column({"categories": _UNI}, deterministic=False)],
    )
    result = native_route_eligibility(config, table="t")
    assert not result.accepted
    assert any(r == "categorical_not_deterministic:c" for r in result.rejections), result.rejections


def test_unseeded_categorical_declines_on_full_frame_binding(tmp_path: Path) -> None:
    """Boundary 2: the compiled full-frame binding leaves the node UNBOUND
    (`execution is None`), so the coordinator never runs it natively -- it
    declines to the oracle."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "x")
    config = build_config(
        tmp_path,
        "t",
        tmp_path / "x.parquet",
        [_cat_column({"categories": _UNI}, deterministic=False)],
    )
    plan = _compile_plan_for(config, source)
    cat_nodes = [n for tbl in plan.tables for n in tbl.nodes if n.strategy == "categorical"]
    assert cat_nodes, "expected a categorical node in the compiled plan"
    assert all(n.execution is None for n in cat_nodes), (
        "unseeded categorical must NOT bind natively"
    )


def test_missing_namespace_categorical_declines(tmp_path: Path) -> None:
    config = build_config(
        tmp_path,
        "t",
        tmp_path / "x.parquet",
        [
            {
                "name": "c",
                "strategy": "categorical",
                "deterministic": True,
                "provider_config": {"categories": _UNI},
            }
        ],
    )
    result = native_route_eligibility(config, table="t")
    assert not result.accepted
    assert any(r == "categorical_requires_namespace:c" for r in result.rejections), (
        result.rejections
    )


# ── Runtime determinism assertion (defensive, at dispatch) ──────────


def test_run_operator_asserts_categorical_determinism() -> None:
    binding = ExecutionBinding(
        operator_id="native_categorical",
        operator_reason="test",
        resolved_config=(),
        input_schema=pa.schema([pa.field("c", pa.string())]),
        output_schema=pa.schema([pa.field("c", pa.string())]),
        determinism_family=None,
        determinism_version=1,
        key_binding=KeyBinding(key_source="mask_key", namespace="ns"),
        diagnostic_obligations=(),
        required_prepasses=(),
        batch_estimate=None,
        categorical_deterministic=False,  # the wiring-bug case the assertion guards
        categorical_categories=("a", "b"),
        categorical_cdf=None,
    )
    ctx = SimpleNamespace(mask_key=_MASK_KEY, native_threads=None)
    evidence = OperatorCallEvidence(planned_operator="native_categorical")
    with pytest.raises(AssertionError, match="categorical_deterministic=False"):
        run_operator(
            pa.array(["x", "y"], type=pa.string()),
            binding=binding,
            ctx=ctx,  # type: ignore[arg-type]
            evidence=evidence,
            index_kernel=None,
        )


# ── is_deterministic_categorical == ColumnSeed.deterministic ────────


@pytest.mark.parametrize(
    "col_extra, expected",
    [
        ({"deterministic": True}, True),
        ({"deterministic": False}, False),
        ({}, False),
        ({"allow_collisions": True}, True),
        ({"deterministic": True, "allow_collisions": True}, True),
    ],
)
def test_predicate_agrees_with_columnseed_deterministic(
    tmp_path: Path, col_extra: dict, expected: bool
) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "x")
    col: dict[str, Any] = {
        "name": "c",
        "strategy": "categorical",
        "namespace": "ns",
        "provider_config": {"categories": _UNI},
        **col_extra,
    }
    config = build_config(tmp_path, "t", tmp_path / "x.parquet", [col])
    inputs = capture_physical_plan_inputs(config, {"t": source}, engine_version=ENGINE_VERSION)
    per_table = dict(inputs.plan.seed_envelope.per_table)
    seed = dict(per_table["t"].per_column)["c"]
    assert seed.deterministic == expected
    # The config-only predicate reads the raw column config the query sees. The
    # compiler's config normalization is what feeds the seed envelope, so read
    # the predicate off the same raw column the eligibility query is handed.
    assert is_deterministic_categorical(col) == seed.deterministic


# ── Unified-slice ExecutionResult boundary (flag-off vs flag-on) ────


def _run_both(tmp_path: Path, source: pa.Table, columns: list[dict]):
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, "t", path, columns)
    off = run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_kp(),
        unified_slice_enabled=False,
    )
    on = run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=_kp(),
        unified_slice_enabled=True,
    )
    return off, on


@pytest.mark.parametrize(
    "label, values, pc",
    [
        ("uniform", ["a", "b", "c", None, "e"], {"categories": _UNI}),
        ("weighted", ["a", "b", "c", None, "e"], {"categories": _UNI, "weights": [1.0, 2.0, 3.0]}),
        ("all_null", [None, None, None], {"categories": _UNI}),
        ("empty", [], {"categories": _UNI}),
    ],
)
def test_unified_slice_execution_result_byte_identical(
    tmp_path: Path, label: str, values: list, pc: dict
) -> None:
    source = pa.table({"c": pa.array(values, type=pa.string())})
    off, on = _run_both(tmp_path, source, [_cat_column(pc)])
    ot, nt = off.outputs["t"], on.outputs["t"]
    # Value AND field type at the final boundary, plus pandas schema metadata.
    assert ot.schema.field("c").type == nt.schema.field("c").type
    assert ot.column("c").to_pylist() == nt.column("c").to_pylist()
    assert ot.schema.equals(nt.schema, check_metadata=True)
    if native_companion_status().ok:
        # The lane actually ACTIVATED for categorical (not a decline that would
        # pass parity against itself); the node ran native categorical.
        assert QUALITY_METRICS_KEY in on.quality_metrics
        node_ev = on.quality_metrics[QUALITY_METRICS_KEY]["nodes"]
        assert node_ev, "expected D7 node evidence"
        for ev in node_ev.values():
            assert ev["operator"] == "native_categorical"
            assert ev["executed"] is True


@pytest.mark.skipif(
    not native_companion_status().ok, reason="compiled decoy-engine-native companion unavailable"
)
def test_unified_slice_parquet_round_trip(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c", "d", "e", None], type=pa.string())})
    off, on = _run_both(
        tmp_path, source, [_cat_column({"categories": _UNI, "weights": [1.0, 2.0, 3.0]})]
    )
    off_path, on_path = tmp_path / "off.parquet", tmp_path / "on.parquet"
    pq.write_table(off.outputs["t"], off_path)
    pq.write_table(on.outputs["t"], on_path)
    off_back, on_back = pq.read_table(off_path), pq.read_table(on_path)
    assert off_back.schema.equals(on_back.schema, check_metadata=True)
    assert off_back.column("c").to_pylist() == on_back.column("c").to_pylist()


def test_non_string_source_declines_to_oracle(tmp_path: Path) -> None:
    """A non-string categorical SOURCE is outside the native operator's admitted
    resident type, so both arms take the legacy route and agree (no activation)."""
    source = pa.table({"c": pa.array([1, 2, 3], type=pa.int64())})
    off, on = _run_both(tmp_path, source, [_cat_column({"categories": _UNI})])
    ot, nt = off.outputs["t"], on.outputs["t"]
    assert ot.column("c").to_pylist() == nt.column("c").to_pylist()
    assert ot.schema.field("c").type == nt.schema.field("c").type
    assert QUALITY_METRICS_KEY not in on.quality_metrics


def test_shadow_difference_is_never_raised_for_admitted_categorical(tmp_path: Path) -> None:
    """A guard that the admitted native categorical path produces a result, not
    a coded `ShadowDifference` (which would signal an admission-predicate gap)."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    write_read_only_fixture(tmp_path, source, "cat")
    config = build_config(
        tmp_path, "t", tmp_path / "cat.parquet", [_cat_column({"categories": _UNI})]
    )
    try:
        run = run_shadow_and_oracle(config, "t", source, key_provider=_kp())
    except ShadowDifference as exc:  # pragma: no cover - regression guard
        pytest.fail(f"admitted categorical raised a ShadowDifference: {exc.code}")
    assert run.shadow.outputs["t"].column("c").to_pylist()
