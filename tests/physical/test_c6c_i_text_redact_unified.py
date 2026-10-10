"""C6c-i acceptance: text_redact as an ARROW_PYTHON operator on the unified full-frame route.

Every admitted case drives the real `run_pipeline` lane with the pandas oracle poisoned, so a
silent decline to the oracle cannot pass, and compares byte for byte against a separate
lane-off run. The excluded cases run the lane unpoisoned and prove it declined.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution import _unified_slice_admission, run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution.native._chunked_evidence import (
    ARROW_PYTHON,
    RUST_COMPANION,
    RUST_POOL_SELECT,
)
from decoy_engine.execution.native._companion_status import KernelAvailability
from decoy_engine.keyprovider import SecretKeyProvider
from tests.native._c6c_i_support import (
    ADMITTED_CONFIGS,
    CORPUS,
    PASS_THROUGH_CONFIGS,
    SHAPES,
    tr_col,
)
from tests.physical._shadow_helpers import build_config, write_read_only_fixture
from tests.physical.test_unified_route_evidence import (
    _FAKER,
    _HASH,
    NEEDS_COMPANION,
    _evidence_for,
    assert_exact_evidence,
    lane_nodes,
)

OPERATOR = "native_text_redact"
_PASS = {"name": "p", "strategy": "passthrough"}
ENGINE_VERSION = "c6c-i-text-redact-unified"


def _source(values: list[str | None]) -> pa.Table:
    return pa.table(
        {
            "s": pa.array(values, pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )


def _assert_text_redact_evidence(nodes: dict[str, dict[str, Any]], *, calls: int = 1) -> None:
    assert_exact_evidence(
        _evidence_for(nodes, OPERATOR),
        operator=OPERATOR,
        compiled=False,
        planned=ARROW_PYTHON,
        executed=ARROW_PYTHON,
        calls=calls,
    )


# ---------------------------------------------------------------------------
# 1. Parity matrix against the lane-off oracle run, native route proven by evidence.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("config", sorted(ADMITTED_CONFIGS))
def test_unified_route_equals_the_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str, shape: str
) -> None:
    columns = [tr_col("s", **ADMITTED_CONFIGS[config]), _PASS]
    nodes = lane_nodes(tmp_path, _source(SHAPES[shape]), columns, monkeypatch)
    _assert_text_redact_evidence(nodes)


# ---------------------------------------------------------------------------
# 2a. End to end through admission, the coordinator and assembly, with the oracle poisoned.
# ---------------------------------------------------------------------------


def test_a_text_redact_only_table_runs_the_unified_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = pa.table({"s": pa.array(CORPUS, pa.string())})
    nodes = lane_nodes(tmp_path, table, [tr_col("s", label_token=True)], monkeypatch)
    assert len(nodes) == 1
    _assert_text_redact_evidence(nodes)


@NEEDS_COMPANION
def test_a_mixed_hash_and_text_redact_table_runs_the_unified_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 9
    table = pa.table(
        {
            "h": pa.array([f"user{i}@x.com" for i in range(n)], pa.string()),
            "s": pa.array(CORPUS[:n], pa.string()),
        }
    )
    nodes = lane_nodes(tmp_path, table, [_HASH, tr_col("s")], monkeypatch)
    _assert_text_redact_evidence(nodes)
    assert_exact_evidence(
        _evidence_for(nodes, "native_keyed_hash"),
        operator="native_keyed_hash",
        compiled=True,
        planned=RUST_COMPANION,
        executed=RUST_COMPANION,
        calls=1,
    )


def test_a_text_redact_only_table_runs_with_the_compiled_companion_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """text_redact needs no compiled kernel, so a host without the companion still admits it."""
    monkeypatch.setattr(
        _unified_slice_admission,
        "native_kernel_availability",
        lambda: KernelAvailability(crypto=False, index=False, raw_hex=False, fpe=False),
    )
    table = pa.table({"s": pa.array(CORPUS, pa.string())})
    nodes = lane_nodes(tmp_path, table, [tr_col("s")], monkeypatch)
    _assert_text_redact_evidence(nodes)


# ---------------------------------------------------------------------------
# 2. Table-level lift.
# ---------------------------------------------------------------------------


def test_text_redact_beside_redact_and_truncate_keeps_the_table_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 7
    table = pa.table(
        {
            "r": pa.array([f"r{i}" for i in range(n)], pa.string()),
            "t": pa.array([f"abcdef{i}" for i in range(n)], pa.string()),
            "s": pa.array(CORPUS[:n], pa.string()),
        }
    )
    columns = [
        {"name": "r", "strategy": "redact"},
        {"name": "t", "strategy": "truncate", "provider_config": {"length": 3}},
        tr_col("s"),
    ]
    nodes = lane_nodes(tmp_path, table, columns, monkeypatch)
    assert {ev["operator"] for ev in nodes.values()} == {
        "native_redact",
        "native_truncate",
        OPERATOR,
    }
    _assert_text_redact_evidence(nodes)


@NEEDS_COMPANION
def test_text_redact_beside_hash_and_faker_keeps_each_on_its_own_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 6
    table = pa.table(
        {
            "h": pa.array([f"a{i}@x.com" for i in range(n)], pa.string()),
            "f": pa.array([f"src_{i % 3}" for i in range(n)], pa.string()),
            "s": pa.array(CORPUS[:n], pa.string()),
        }
    )
    nodes = lane_nodes(tmp_path, table, [_HASH, _FAKER, tr_col("s")], monkeypatch)
    _assert_text_redact_evidence(nodes)
    backends = {ev["operator"]: ev["executed_backend"] for ev in nodes.values()}
    assert backends == {
        "native_keyed_hash": RUST_COMPANION,
        "native_faker_select": RUST_POOL_SELECT,
        OPERATOR: ARROW_PYTHON,
    }


# ---------------------------------------------------------------------------
# 3-5. Excluded configs, non-string sources and `when:` decline the lane without changing output.
# ---------------------------------------------------------------------------


def _run_both(
    tmp_path: Path,
    source: pa.Table,
    columns: list[dict[str, Any]],
    *,
    when: str | None = None,
) -> tuple[ExecutionResult, ExecutionResult]:
    results = []
    for lane in (False, True):
        sub = tmp_path / ("on" if lane else "off")
        sub.mkdir()
        path = write_read_only_fixture(sub, source, "fixture")
        config = build_config(sub, "t", path, columns)
        if when is not None:
            config["tables"][0]["columns"][0]["when"] = when
        results.append(
            run_pipeline(
                config,
                {"t": pq.read_table(path)},
                engine_version=ENGINE_VERSION,
                key_provider=SecretKeyProvider(secret=bytes(range(32)), key_version="v1"),
                unified_slice_enabled=lane,
            )
        )
    return results[0], results[1]


def _assert_declined_and_equal(off: ExecutionResult, on: ExecutionResult) -> None:
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert QUALITY_METRICS_KEY not in off.quality_metrics
    assert off.outputs["t"].schema.equals(on.outputs["t"].schema, check_metadata=True)
    assert off.outputs["t"].to_pylist() == on.outputs["t"].to_pylist()


@pytest.mark.parametrize("name", sorted(PASS_THROUGH_CONFIGS))
def test_an_excluded_config_declines_the_lane_and_leaves_the_column_unchanged(
    tmp_path: Path, name: str
) -> None:
    cfg, _code = PASS_THROUGH_CONFIGS[name]
    source = _source(CORPUS)
    off, on = _run_both(tmp_path, source, [tr_col("s", **cfg), _PASS])
    _assert_declined_and_equal(off, on)
    assert on.outputs["t"].column("s").to_pylist() == CORPUS


@pytest.mark.parametrize("typ", [pa.int64(), pa.large_string()], ids=["int64", "large_string"])
def test_a_non_string_source_declines_the_lane_and_equals_the_oracle(
    tmp_path: Path, typ: pa.DataType
) -> None:
    values = [10, 22, None, 333] if typ == pa.int64() else ["a@b.com", None, "x", ""]
    source = pa.table({"s": pa.array(values, typ), "p": pa.array(range(4), pa.int64())})
    off, on = _run_both(tmp_path, source, [tr_col("s"), _PASS])
    _assert_declined_and_equal(off, on)


def test_a_chained_comparison_when_predicate_is_rejected_at_compile(tmp_path: Path) -> None:
    # The column alone is admitted; a chained comparison is outside the closed grammar, so
    # compile rejects it before either lane setting runs, instead of declining the lane.
    from decoy_engine.plan._errors import PlanCompileError

    with pytest.raises(PlanCompileError) as info:
        _run_both(tmp_path, _source(CORPUS), [tr_col("s"), _PASS], when="0 < p < 3")
    assert info.value.code == "when_outside_closed_grammar"
    assert info.value.path == "tables.t.columns.s.when"


# ---------------------------------------------------------------------------
# 8. Determinism: no dependence on the mask key or the job seed.
# ---------------------------------------------------------------------------


def _lane_values(tmp_path: Path, name: str, *, secret: bytes, seed: int) -> list[Any]:
    sub = tmp_path / name
    sub.mkdir()
    path = write_read_only_fixture(sub, _source(CORPUS), "fixture")
    config = build_config(sub, "t", path, [tr_col("s", label_token=True), _PASS], seed=seed)
    result = run_pipeline(
        config,
        {"t": pq.read_table(path)},
        engine_version=ENGINE_VERSION,
        key_provider=SecretKeyProvider(secret=secret, key_version="v1"),
        unified_slice_enabled=True,
    )
    return list(result.outputs["t"].column("s").to_pylist())


def test_output_is_reproducible_and_independent_of_the_mask_key_and_job_seed(
    tmp_path: Path,
) -> None:
    base = _lane_values(tmp_path, "a", secret=bytes(range(32)), seed=11)
    assert base == _lane_values(tmp_path, "b", secret=bytes(range(32)), seed=11)
    assert base == _lane_values(tmp_path, "c", secret=bytes(range(1, 33)), seed=11)
    assert base == _lane_values(tmp_path, "d", secret=bytes(range(32)), seed=99)


# ---------------------------------------------------------------------------
# 7. Evidence on a multi-batch table.
# ---------------------------------------------------------------------------


def test_the_unified_evidence_never_claims_compiled_work_for_text_redact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = pa.table({"s": pa.array(CORPUS, pa.string())})
    nodes = lane_nodes(tmp_path, table, [tr_col("s")], monkeypatch)
    ev = _evidence_for(nodes, OPERATOR)
    assert ev["compiled_kernel_executed"] is False
    assert ev["planned_backend"] == ev["executed_backend"] == ARROW_PYTHON
