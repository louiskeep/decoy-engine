"""R3 acceptance test 4: the context-to-`maybe_run_unified_slice` boundary.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md section 6, test 4. It replaces
the import-time `_assert_forwarding_covers_signature` check with tests of what the unified
lane really receives from a real `run_pipeline` call:

* the lane is offered exactly the typed context, the routing decision, the executor's resolved
  sources and the job's pool cache, and its admitted-execution call supplies every parameter
  `_execute_admitted` declares and nothing else, including the derived inputs (`plan`, `profile`,
  `graph`, `route_reason`, `adapter`, `pool_cache`);
* the RESOLVED registry and key provider are what flows through, never the raw arguments. The
  values are distinguishable on purpose: the raw `registry` and `key_provider` are `None` while
  the resolved ones are a default registry and a provider built from `mask_secret_ref`, so a
  raw-for-resolved swap fails;
* the knobs reach the lane unchanged (distinguishable non-default values);
* one `PoolCache` serves the unified lane and, when the lane declines, the full-frame oracle.

This is separate from the public-signature freeze (`test_r3_public_signature_freeze.py`).
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.execution import (
    _pipeline_context,
    _pipeline_generate_mask,
    _unified_slice,
    run_pipeline,
)
from decoy_engine.execution import _unified_slice_admission as admission
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.generation.pool import PoolCache
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.providers_v2 import get_default_registry
from tests.unit.execution import _r3_scenarios as sc

pytestmark = pytest.mark.filterwarnings("ignore")

_REAL_LANE = _unified_slice.maybe_run_unified_slice
_REAL_EXECUTE = _unified_slice._execute_admitted
_REAL_CHEAP = admission.cheap_admission
_SECRET_ENV = "R3_BOUNDARY_TEST_MASK_SECRET"
_KNOBS: dict[str, Any] = {
    "fpe_chunk_count": 7,
    "max_workers": 3,
    "chunk_size_rows": 1234,
    "auto_chunk_threshold_rows": 987_654,
    "native_threads": 2,
    "out_of_core_threshold_rows": 4_000_001,
    "full_frame_reject_rows": 8_000_001,
    "out_of_core_budget_bytes": 2**31,
    "out_of_core_reorder_threshold_rows": 17,
    "explain_plan": True,
}


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    seen: dict[str, list[dict[str, Any]]] = {
        "lane": [],
        "admission": [],
        "execute": [],
        "oracle": [],
    }

    def spy(key: str, real: Any) -> Any:
        def wrapped(**kwargs: Any) -> Any:
            seen[key].append(kwargs)
            return real(**kwargs)

        return wrapped

    monkeypatch.setattr(_unified_slice, "maybe_run_unified_slice", spy("lane", _REAL_LANE))
    monkeypatch.setattr(admission, "cheap_admission", spy("admission", _REAL_CHEAP))
    monkeypatch.setattr(_unified_slice, "_execute_admitted", spy("execute", _REAL_EXECUTE))
    monkeypatch.setattr(
        _pipeline_generate_mask,
        "run_generate_and_mask_steps",
        spy("oracle", _pipeline_generate_mask.run_generate_and_mask_steps),
    )
    return seen


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any
) -> dict[str, list[dict[str, Any]]]:
    monkeypatch.setenv(_SECRET_ENV, "ab" * 32)
    seen = _capture(monkeypatch)
    cfg, table = sc.single_table(tmp_path, hash_column=False)
    cfg["global_settings"]["mask_secret_ref"] = f"env:{_SECRET_ENV}"
    run_pipeline(
        cfg,
        {"t": table},
        engine_version=sc.ENGINE_VERSION,
        registry=None,
        key_provider=None,
        now_iso=sc.NOW_ISO,
        **{**_KNOBS, **extra},
    )
    return seen


def _admitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    return _run(tmp_path, monkeypatch)


def _declined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Offered to the lane, declined (fidelity report on), so the oracle runs after it."""
    return _run(tmp_path, monkeypatch, fidelity_report=True)


def test_the_lane_is_offered_the_context_the_decision_and_the_executors_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _declined(tmp_path, monkeypatch)
    assert len(seen["lane"]) == 1
    kw = seen["lane"][0]
    assert set(kw) == set(inspect.signature(_REAL_LANE).parameters)
    assert set(kw) == {"ctx", "decision", "caller_sources", "pool_cache"}
    assert isinstance(kw["ctx"], _pipeline_context.PipelineRunContext)
    assert isinstance(kw["decision"], _pipeline_context.RouteDecision)


def test_the_admitted_call_supplies_every_parameter_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _admitted(tmp_path, monkeypatch)
    assert len(seen["execute"]) == 1, "the fixture must reach admitted execution"
    declared = set(inspect.signature(_REAL_EXECUTE).parameters)
    assert set(seen["execute"][0]) == declared
    cheap = set(inspect.signature(_REAL_CHEAP).parameters)
    assert set(seen["admission"][0]) == cheap


def test_the_lane_receives_resolved_values_not_the_raw_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _admitted(tmp_path, monkeypatch)
    ctx = seen["lane"][0]["ctx"]
    # Raw `registry=None` / `key_provider=None` were passed; the lane must see the resolved ones.
    assert ctx.registry is get_default_registry()
    assert isinstance(ctx.key_provider, SecretKeyProvider)
    for kw in (seen["execute"][0], seen["admission"][0]):
        assert kw["registry"] is get_default_registry()
    assert isinstance(seen["execute"][0]["key_provider"], SecretKeyProvider)
    assert seen["execute"][0]["key_provider"] is ctx.key_provider


def test_the_oracle_receives_the_same_resolved_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _declined(tmp_path, monkeypatch)
    ctx, oracle = seen["lane"][0]["ctx"], seen["oracle"][0]
    assert oracle["registry"] is ctx.registry is get_default_registry()
    assert oracle["key_provider"] is ctx.key_provider
    assert isinstance(oracle["key_provider"], SecretKeyProvider)
    assert oracle["adapter"] is ctx.adapter


def test_derived_inputs_are_the_resolved_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kw = _admitted(tmp_path, monkeypatch)["execute"][0]
    assert type(kw["plan"]).__name__ == "Plan"
    assert type(kw["profile"]).__name__ == "Profile"
    assert type(kw["graph"]).__name__ == "RelationshipGraph"
    assert kw["route_reason"] == "no_relationships"
    assert isinstance(kw["adapter"], PandasExecutionAdapter)
    assert kw["adapter"]._fpe_chunk_count == 7
    assert isinstance(kw["pool_cache"], PoolCache)
    assert kw["execution_plan_decision"] is not None  # explain_plan=True classified the job
    assert kw["table_kinds"] == {"t": "mask"}
    assert all(isinstance(v, pa.Table) for v in kw["caller_sources"].values())
    admit = _admitted_cheap(tmp_path, monkeypatch)
    assert admit["route"] == "full_frame" and admit["route_chunked"] is False


def _admitted_cheap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    (tmp_path / "again").mkdir()
    return _admitted(tmp_path / "again", monkeypatch)["admission"][0]


def test_knobs_reach_the_lane_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kw = _admitted(tmp_path, monkeypatch)["execute"][0]
    for name, value in _KNOBS.items():
        assert kw[name] == value, name
    assert kw["execution_mode"] == "auto"
    assert kw["engine_version"] == sc.ENGINE_VERSION
    assert kw["substrate"] == "pandas" and kw["resolved_substrate"] == "pandas"
    assert kw["fidelity_report"] is False and kw["vault_writer"] is None


def test_the_admitted_call_gets_the_executors_pool_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _admitted(tmp_path, monkeypatch)
    assert seen["execute"][0]["pool_cache"] is seen["lane"][0]["pool_cache"]
    assert seen["execute"][0]["caller_sources"] is seen["lane"][0]["caller_sources"]


def test_the_chunk_decision_reaches_cheap_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _run(
        tmp_path, monkeypatch, auto_chunk_threshold_rows=10, use_byte_estimate_routing=False
    )
    assert seen["admission"][0]["route_chunked"] is True
    assert seen["admission"][0]["route"] == "full_frame"
    assert seen["execute"] == []  # a chunk-routed job is declined before admitted execution


def test_one_pool_cache_serves_the_lane_and_the_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _declined(tmp_path, monkeypatch)
    assert len(seen["oracle"]) == 1
    assert seen["lane"][0]["pool_cache"] is seen["oracle"][0]["pool_cache"]


def test_the_lane_is_not_offered_a_job_a_bounded_route_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture(monkeypatch)
    sc.success_snapshot("seq_fk", tmp_path)
    assert seen["lane"] == [] and seen["oracle"] == []
