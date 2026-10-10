"""R3 acceptance test 4: the context-to-`maybe_run_unified_slice` boundary.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md section 6, test 4. It replaces
the import-time `_assert_forwarding_covers_signature` check with tests of what the unified
lane really receives from a real `run_pipeline` call:

* every parameter `maybe_run_unified_slice` declares is supplied, including the derived
  inputs (`plan`, `profile`, `graph`, `route_reason`, `adapter`, `pool_cache`), and nothing
  unknown is supplied;
* the RESOLVED registry and key provider are forwarded, never the raw arguments. The values
  are distinguishable on purpose: the raw `registry` and `key_provider` are `None` while the
  resolved ones are a default registry and a provider built from `mask_secret_ref`, so a
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

from decoy_engine.execution import _pipeline_generate_mask, _unified_slice, run_pipeline
from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter
from decoy_engine.generation.pool import PoolCache
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.providers_v2 import get_default_registry
from tests.unit.execution import _r3_scenarios as sc

pytestmark = pytest.mark.filterwarnings("ignore")

_REAL_LANE = _unified_slice.maybe_run_unified_slice
_SECRET_ENV = "R3_BOUNDARY_TEST_MASK_SECRET"


def _capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    seen: dict[str, list[dict[str, Any]]] = {"lane": [], "oracle": []}
    real_lane = _unified_slice.maybe_run_unified_slice
    real_oracle = _pipeline_generate_mask.run_generate_and_mask_steps

    def lane(**kwargs: Any) -> Any:
        seen["lane"].append(kwargs)
        return real_lane(**kwargs)

    def oracle(**kwargs: Any) -> Any:
        seen["oracle"].append(kwargs)
        return real_oracle(**kwargs)

    monkeypatch.setattr(_unified_slice, "maybe_run_unified_slice", lane)
    monkeypatch.setattr(_pipeline_generate_mask, "run_generate_and_mask_steps", oracle)
    return seen


def _run_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, list[dict[str, Any]]]:
    """A job the lane is offered but declines (fidelity report on), so the oracle runs after it."""
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
        fpe_chunk_count=7,
        max_workers=3,
        chunk_size_rows=1234,
        auto_chunk_threshold_rows=987_654,
        native_threads=2,
        out_of_core_threshold_rows=4_000_001,
        full_frame_reject_rows=8_000_001,
        out_of_core_budget_bytes=2**31,
        out_of_core_reorder_threshold_rows=17,
        explain_plan=True,
        fidelity_report=True,
        now_iso=sc.NOW_ISO,
    )
    return seen


def test_the_lane_is_offered_every_parameter_it_declares_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _run_declined(tmp_path, monkeypatch)
    assert len(seen["lane"]) == 1
    declared = set(inspect.signature(_REAL_LANE).parameters)
    assert set(seen["lane"][0]) == declared


def test_the_lane_receives_resolved_values_not_the_raw_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kw = _run_declined(tmp_path, monkeypatch)["lane"][0]
    # Raw `registry=None` / `key_provider=None` were passed; the lane must see the resolved ones.
    assert kw["registry"] is get_default_registry()
    assert isinstance(kw["key_provider"], SecretKeyProvider)


def test_the_oracle_receives_the_same_resolved_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _run_declined(tmp_path, monkeypatch)
    lane, oracle = seen["lane"][0], seen["oracle"][0]
    assert oracle["registry"] is lane["registry"] is get_default_registry()
    assert oracle["key_provider"] is lane["key_provider"]
    assert isinstance(oracle["key_provider"], SecretKeyProvider)
    assert oracle["adapter"] is lane["adapter"]


def test_derived_inputs_are_the_resolved_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kw = _run_declined(tmp_path, monkeypatch)["lane"][0]
    assert type(kw["plan"]).__name__ == "Plan"
    assert type(kw["profile"]).__name__ == "Profile"
    assert type(kw["graph"]).__name__ == "RelationshipGraph"
    assert kw["route"] == "full_frame"
    assert kw["route_reason"] == "no_relationships"
    assert kw["route_chunked"] is False
    assert isinstance(kw["adapter"], PandasExecutionAdapter)
    assert kw["adapter"]._fpe_chunk_count == 7
    assert isinstance(kw["pool_cache"], PoolCache)
    assert kw["execution_plan_decision"] is not None  # explain_plan=True classified the job
    assert kw["table_kinds"] == {"t": "mask"}
    assert all(isinstance(v, pa.Table) for v in kw["caller_sources"].values())


def test_knobs_reach_the_lane_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kw = _run_declined(tmp_path, monkeypatch)["lane"][0]
    assert kw["fpe_chunk_count"] == 7
    assert kw["max_workers"] == 3
    assert kw["chunk_size_rows"] == 1234
    assert kw["auto_chunk_threshold_rows"] == 987_654
    assert kw["native_threads"] == 2
    assert kw["out_of_core_threshold_rows"] == 4_000_001
    assert kw["full_frame_reject_rows"] == 8_000_001
    assert kw["out_of_core_budget_bytes"] == 2**31
    assert kw["out_of_core_reorder_threshold_rows"] == 17
    assert kw["explain_plan"] is True
    assert kw["fidelity_report"] is True
    assert kw["unified_slice_enabled"] is True
    assert kw["execution_mode"] == "auto"
    assert kw["engine_version"] == sc.ENGINE_VERSION
    assert kw["substrate"] == "pandas" and kw["resolved_substrate"] == "pandas"
    assert kw["sink"] is None and kw["source_loader"] is None and kw["vault_writer"] is None


def test_one_pool_cache_serves_the_lane_and_the_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _run_declined(tmp_path, monkeypatch)
    assert len(seen["oracle"]) == 1
    assert seen["lane"][0]["pool_cache"] is seen["oracle"][0]["pool_cache"]


def test_the_lane_is_not_offered_a_job_a_bounded_route_took(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _capture(monkeypatch)
    sc.success_snapshot("seq_fk", tmp_path)
    assert seen["lane"] == [] and seen["oracle"] == []
