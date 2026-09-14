"""D1/D2 unit tests for `PhysicalPlanInputs` (`plan_hash`) and the compiler's
free functions (`relationship_role`, `native_applies`,
`out_of_core_not_ready_reason`, `select_driver`) that
`test_compiler_preflight_equivalence.py`'s real-job corpus does not exercise
directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution.physical import (
    DriverId,
    NativeAdmissionFact,
    OutOfCoreRoutingFacts,
    capture_physical_plan_inputs,
)
from decoy_engine.execution.physical._compiler import (
    native_applies,
    out_of_core_not_ready_reason,
    relationship_role,
    select_driver,
)


def _write(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _flat_inputs(tmp_path: Path, **kwargs: Any) -> Any:
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": "redact"}]}],
        }
    ).model_dump()
    return capture_physical_plan_inputs(config, {"t": source}, engine_version="unit-test", **kwargs)


def _fk_inputs(tmp_path: Path, **kwargs: Any) -> Any:
    parent = pa.table({"id": pa.array(["p1", "p2"], type=pa.string())})
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    pp = _write(tmp_path, parent, "parent")
    cp = _write(tmp_path, child, "child")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": str(pp)},
                "child": {"type": "file", "format": "parquet", "path": str(cp)},
            },
            "targets": {
                "parent": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "parent.out.parquet"),
                },
                "child": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "child.out.parquet"),
                },
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [{"name": "id", "strategy": "hash", "namespace": "n"}],
                },
                {
                    "name": "child",
                    "columns": [{"name": "pid", "strategy": "hash", "namespace": "n"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                }
            ],
        }
    ).model_dump()
    return capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="unit-test", **kwargs
    )


# ---------------------------------------------------------------------------
# plan_hash (D1)
# ---------------------------------------------------------------------------


def test_plan_hash_stable_for_identical_inputs(tmp_path: Path) -> None:
    inputs_a = _flat_inputs(tmp_path)
    inputs_b = _flat_inputs(tmp_path)
    assert inputs_a.plan_hash() == inputs_b.plan_hash()


def test_plan_hash_changes_with_execution_mode(tmp_path: Path) -> None:
    inputs_auto = _flat_inputs(tmp_path)
    inputs_forced = _flat_inputs(tmp_path, execution_mode="full_frame")
    assert inputs_auto.plan_hash() != inputs_forced.plan_hash()


def test_plan_hash_changes_with_native_route_enabled(tmp_path: Path) -> None:
    inputs_off = _flat_inputs(tmp_path)
    inputs_on = _flat_inputs(tmp_path, native_route_enabled=True)
    assert inputs_off.plan_hash() != inputs_on.plan_hash()


def test_registry_fingerprint_stable_for_the_same_registry(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path)
    assert inputs.registry_fingerprint() == inputs.registry_fingerprint()
    assert len(inputs.registry_fingerprint()) == 64  # sha256 hex digest


def _capability_matrix(*, poolable: bool) -> Any:
    from decoy_engine.providers_v2._adapter import CapabilityMatrix

    return CapabilityMatrix(
        provider="dup_provider",
        backend_type="faker",
        backend_version="1",
        supports_deterministic=True,
        supports_uniqueness=False,
        supports_value_reuse=True,
        preserves_source_cardinality=True,
        participates_in_fk_pk=False,
        poolable=poolable,
        supported_locales=("en_US",),
        supports_coherent_link=False,
        format_regex=None,
        blocklist_validators=(),
        fallback_behavior="error",
    )


def _registry_with(matrix: Any) -> Any:
    from typing import cast

    from decoy_engine.providers_v2._adapter import BackendAdapter
    from decoy_engine.providers_v2._registry import ProviderRegistry

    stub_adapter = cast(BackendAdapter, object())
    return ProviderRegistry({"dup_provider": (stub_adapter, matrix)})


def test_plan_hash_changes_with_resident_row_count(tmp_path: Path) -> None:
    """H1 counterexample: a 2-row vs 4-row resident source on the IDENTICAL
    config must NOT collide -- pre-fix, `plan_hash` never hashed resident
    source content at all, so these produced the SAME hash despite routing
    `full_frame` vs `chunked` under a low auto-chunk threshold.
    """
    small = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    large = pa.table({"note": pa.array([f"s{i}" for i in range(4)], type=pa.string())})
    path = _write(tmp_path, small, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": "redact"}]}],
        }
    ).model_dump()
    kwargs: dict[str, Any] = dict(auto_chunk=True, auto_chunk_threshold_rows=3, chunk_size_rows=1)
    inputs_small = capture_physical_plan_inputs(
        config, {"t": small}, engine_version="unit-test", **kwargs
    )
    inputs_large = capture_physical_plan_inputs(
        config, {"t": large}, engine_version="unit-test", **kwargs
    )
    assert inputs_small.plan_hash() != inputs_large.plan_hash()
    # Confirms the collision was real, not just a hash-of-nothing-in-
    # particular: the two resident inputs actually route differently.
    from decoy_engine.execution.physical._compiler import compile_physical_plan

    small_driver = compile_physical_plan(inputs_small).tables[0].driver
    large_driver = compile_physical_plan(inputs_large).tables[0].driver
    assert small_driver.value == "full_frame"
    assert large_driver.value == "chunked"
    assert small_driver != large_driver


def test_plan_hash_changes_with_extra_source_frame(tmp_path: Path) -> None:
    """H1's "extra/missing source frame" bullet: an extra loaded source frame
    the config never declares changes the resident-source key set, so it must
    change the hash even though nothing else about the job differs."""
    inputs_bare = _flat_inputs(tmp_path)
    extra = pa.table({"unused": pa.array([1, 2], type=pa.int64())})
    caller_sources = dict(inputs_bare.caller_sources)
    caller_sources["extra"] = extra
    from dataclasses import replace

    inputs_extra = replace(inputs_bare, caller_sources=caller_sources)
    assert inputs_bare.plan_hash() != inputs_extra.plan_hash()


def test_registry_fingerprint_changes_with_capability_matrix(tmp_path: Path) -> None:
    """H1 counterexample: two registries that declare the SAME provider name
    but different capability matrices must NOT collide -- pre-fix,
    `registry_fingerprint` hashed `known_providers()` names only, so a
    same-name/different-matrix registry swap was invisible to `plan_hash`
    despite `_build_nodes` consulting the matrix to build node shape."""
    from dataclasses import replace

    inputs = _flat_inputs(tmp_path)
    registry_a = _registry_with(_capability_matrix(poolable=True))
    registry_b = _registry_with(_capability_matrix(poolable=False))
    assert registry_a.known_providers() == registry_b.known_providers()
    inputs_a = replace(inputs, registry=registry_a)
    inputs_b = replace(inputs, registry=registry_b)
    assert inputs_a.registry_fingerprint() != inputs_b.registry_fingerprint()
    assert inputs_a.plan_hash() != inputs_b.plan_hash()


# ---------------------------------------------------------------------------
# relationship_role
# ---------------------------------------------------------------------------


def test_relationship_role_independent_for_flat_table(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path)
    assert relationship_role("t", inputs.graph) == "independent"


def test_relationship_role_parent_and_child(tmp_path: Path) -> None:
    inputs = _fk_inputs(tmp_path)
    assert relationship_role("parent", inputs.graph) == "parent"
    assert relationship_role("child", inputs.graph) == "child"


# ---------------------------------------------------------------------------
# native_applies
# ---------------------------------------------------------------------------


def test_native_applies_false_when_disabled(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path)
    assert native_applies(inputs) is False


def test_native_applies_true_when_enabled_with_mask_table(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path, native_route_enabled=True)
    assert native_applies(inputs) is True


# ---------------------------------------------------------------------------
# out_of_core_not_ready_reason
# ---------------------------------------------------------------------------


def test_out_of_core_not_ready_reason_reports_no_relationships_first(tmp_path: Path) -> None:
    """A flat (no-relationships) table fails the LIVE conjunction's FIRST
    operand (`_sequential_eligible` -> `no_relationships`), before eligibility
    even reaches the `compatible` check -- this is the H2 counterexample: the
    pre-fix compiler skipped straight to `(compatible, size, threshold)` and
    reported the unrelated `out_of_core_incompatible` fallback for a job that
    was never sequential-eligible to begin with.
    """
    inputs = _flat_inputs(tmp_path)  # no relationships -> ineligible at the first operand
    facts = inputs.out_of_core_facts
    assert facts.compatible is False
    assert out_of_core_not_ready_reason(inputs) == "no_relationships"


def test_out_of_core_not_ready_reason_reports_incompatible_code_when_eligible(
    tmp_path: Path,
) -> None:
    """An FK job that IS sequential-eligible (pure-mask FK, no cycle, has a
    mask table) but carries a strategy the out-of-core compat gate rejects:
    reaches the `compatible` operand and reports its `reject_code`. Isolates
    the H2 fix's LATER conjunction operands from the `no_relationships` case
    above, which now short-circuits before ever reaching them.
    """
    parent = pa.table(
        {
            "id": pa.array(["p1", "p2"], type=pa.string()),
            "extra": pa.array(["x", "y"], type=pa.string()),
        }
    )
    child = pa.table({"pid": pa.array(["p1", "p2"], type=pa.string())})
    pp = _write(tmp_path, parent, "parent")
    cp = _write(tmp_path, child, "child")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {
                "parent": {"type": "file", "format": "parquet", "path": str(pp)},
                "child": {"type": "file", "format": "parquet", "path": str(cp)},
            },
            "targets": {
                "parent": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "parent.out.parquet"),
                },
                "child": {
                    "type": "file",
                    "format": "parquet",
                    "path": str(tmp_path / "child.out.parquet"),
                },
            },
            "tables": [
                {
                    "name": "parent",
                    "columns": [
                        {"name": "id", "strategy": "hash", "namespace": "n"},
                        {"name": "extra", "strategy": "synthetic"},
                    ],
                },
                {
                    "name": "child",
                    "columns": [{"name": "pid", "strategy": "hash", "namespace": "n"}],
                },
            ],
            "relationships": [
                {
                    "parent": {"table": "parent", "columns": ["id"]},
                    "children": [{"table": "child", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                    "namespace": "n",
                }
            ],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(
        config, {"parent": parent, "child": child}, engine_version="unit-test"
    )
    facts = inputs.out_of_core_facts
    assert facts.compatible is False
    assert out_of_core_not_ready_reason(inputs) == (facts.reject_code or "out_of_core_incompatible")


def test_out_of_core_not_ready_reason_validators_disqualify_before_compat_check(
    tmp_path: Path,
) -> None:
    """H2's exact counterexample: a validators-bearing FK job is disqualified
    at the FIRST conjunction operand (`_sequential_eligible` ->
    `validators_present`), so the reason must name THAT disqualifier, never
    fall through to `compatible`/size/threshold (which the pre-fix compiler
    never even checked eligibility before consulting).
    """
    from dataclasses import replace

    inputs = _fk_inputs(tmp_path, use_byte_estimate_routing=False)
    # Sanity: the same job without validators is sequential-eligible (the D4
    # corpus's test_sequential_small_fk_byte_estimate_off routes it
    # `sequential`), so `validators_present` below is attributable to the
    # `validators` field, not the job shape.
    assert out_of_core_not_ready_reason(inputs) != "validators_present"
    inputs = replace(inputs, validators=({"name": "fk_intact"},))
    assert out_of_core_not_ready_reason(inputs) == "validators_present"


def test_out_of_core_not_ready_reason_reports_below_threshold(tmp_path: Path) -> None:
    inputs = _fk_inputs(
        tmp_path, use_byte_estimate_routing=False, out_of_core_threshold_rows=1_000_000
    )
    facts = inputs.out_of_core_facts
    assert facts.compatible is True
    assert facts.largest_table_rows is not None
    reason = out_of_core_not_ready_reason(inputs)
    assert reason == f"out_of_core_below_threshold:{facts.largest_table_rows}"


# ---------------------------------------------------------------------------
# select_driver: RejectedAlternative content
# ---------------------------------------------------------------------------


def test_select_driver_full_frame_records_unattempted_native_and_attempted_chunked(
    tmp_path: Path,
) -> None:
    # auto_chunk defaults True (`_pipeline_finalize.AUTO_CHUNK_DEFAULT`), so
    # classify_job runs and declines chunked for the 2-row table (below the
    # default 100k auto-chunk threshold) -- chunked IS attempted here, just
    # not admitted; native never applies (native_route_enabled defaults False).
    inputs = _flat_inputs(tmp_path)
    selection = select_driver(inputs)
    assert selection.driver == DriverId.FULL_FRAME
    by_driver = {alt.driver: alt for alt in selection.rejected_alternatives}
    assert by_driver[DriverId.NATIVE_STREAM].attempted is False
    assert by_driver[DriverId.CHUNKED].attempted is True
    assert by_driver[DriverId.CHUNKED].reason == "chunked_source_below_threshold"
    # A non-FK table never had out_of_core/sequential as plausible
    # alternatives, so neither appears.
    assert DriverId.OUT_OF_CORE not in by_driver
    assert DriverId.SEQUENTIAL not in by_driver


def test_select_driver_native_stream_records_relationship_free_alternatives(tmp_path: Path) -> None:
    from decoy_engine.profile._readers import LazySource

    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": "redact"}]}],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(
        config,
        {"t": LazySource(path=path)},
        engine_version="unit-test",
        native_route_enabled=True,
    )
    selection = select_driver(inputs)
    assert selection.driver == DriverId.NATIVE_STREAM
    by_driver = {alt.driver: alt for alt in selection.rejected_alternatives}
    assert DriverId.OUT_OF_CORE not in by_driver
    assert DriverId.SEQUENTIAL not in by_driver
    # chunked declines here too: classify_job's per-table runtime gate
    # (`_planner._runtime_source_rejections`) rejects a LazySource outright.
    assert by_driver[DriverId.CHUNKED].attempted is True
    assert by_driver[DriverId.CHUNKED].reason == "chunked_lazy_source_unsupported"


def test_select_driver_sequential_records_out_of_core_alternative(tmp_path: Path) -> None:
    inputs = _fk_inputs(tmp_path, use_byte_estimate_routing=False)
    selection = select_driver(inputs)
    assert selection.driver == DriverId.SEQUENTIAL
    assert len(selection.rejected_alternatives) == 1
    assert selection.rejected_alternatives[0].driver == DriverId.OUT_OF_CORE
    assert selection.rejected_alternatives[0].attempted is True


def test_select_driver_out_of_core_records_no_alternatives(tmp_path: Path) -> None:
    inputs = _fk_inputs(tmp_path, execution_mode="out_of_core")
    selection = select_driver(inputs)
    assert selection.driver == DriverId.OUT_OF_CORE
    assert selection.rejected_alternatives == ()


# ---------------------------------------------------------------------------
# NativeAdmissionFact / OutOfCoreRoutingFacts construction sanity
# ---------------------------------------------------------------------------


def test_native_admission_fact_disabled_sentinel_is_consistent(tmp_path: Path) -> None:
    inputs = _flat_inputs(tmp_path)
    admission = inputs.native_admission
    assert admission.static_candidate is False
    assert admission.admitted is False
    assert admission.reason == "native_route_disabled_or_no_mask_table"
    assert admission.table is None
    assert admission.lane is None


def test_out_of_core_facts_is_frozen(tmp_path: Path) -> None:
    inputs = _fk_inputs(tmp_path)
    facts = inputs.out_of_core_facts
    assert isinstance(facts, OutOfCoreRoutingFacts)
    try:
        facts.compatible = not facts.compatible  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("OutOfCoreRoutingFacts must be frozen")


def test_native_admission_fact_is_frozen() -> None:
    fact = NativeAdmissionFact(
        table=None,
        static_candidate=False,
        static_reason=None,
        sink_mode=None,
        lane=None,
        admitted=False,
        reason=None,
    )
    try:
        fact.admitted = True  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("NativeAdmissionFact must be frozen")


def test_table_kinds_is_immutable_after_capture(tmp_path: Path) -> None:
    """H1's "freeze mutable snapshot members" bullet: `table_kinds` is a
    `MappingProxyType`, not a plain `dict`, so a caller cannot mutate the
    captured snapshot out from under `plan_hash`'s content contract."""
    inputs = _flat_inputs(tmp_path)
    with pytest.raises(TypeError):
        inputs.table_kinds["t"] = "generate"  # type: ignore[index]


# ---------------------------------------------------------------------------
# H3: OOC/host-budget facts + native companion probe, pinned against the
# LIVE resolvers they capture (`resolve_budget`'s sibling disk stat and
# `native_companion_status()`) -- the equivalence TASK-4.3-REMEDIATION.md's
# H3 bullet asks for on the resolvable subset.
# ---------------------------------------------------------------------------


def test_out_of_core_facts_temp_disk_budget_matches_live_disk_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    from decoy_engine.execution import _pipeline_route_exec
    from decoy_engine.execution.out_of_core._spill_estimate import default_ooc_temp_root

    class _FakeUsage:
        free = 1_000_000_000

    monkeypatch.setattr(shutil, "disk_usage", lambda _path: _FakeUsage())
    inputs = _flat_inputs(tmp_path)
    expected = int(_FakeUsage.free * _pipeline_route_exec._TEMP_DISK_SAFETY_FRACTION)
    assert inputs.out_of_core_facts.temp_disk_budget_bytes == expected
    # Sanity: the captured value used the SAME root production spills under.
    assert default_ooc_temp_root() is not None


def test_out_of_core_facts_temp_disk_budget_none_on_undetectable_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    def _raise(_path: object) -> None:
        raise OSError("undetectable")

    monkeypatch.setattr(shutil, "disk_usage", _raise)
    inputs = _flat_inputs(tmp_path)
    assert inputs.out_of_core_facts.temp_disk_budget_bytes is None


def test_out_of_core_facts_merge_fan_in_matches_route_policy_default(tmp_path: Path) -> None:
    from decoy_engine.execution.out_of_core._route_policy import _MERGE_FAN_IN_DEFAULT

    inputs = _flat_inputs(tmp_path)
    assert inputs.out_of_core_facts.merge_fan_in == _MERGE_FAN_IN_DEFAULT


def test_native_companion_reason_matches_live_probe(tmp_path: Path) -> None:
    from decoy_engine.execution.native._companion_status import native_companion_status

    inputs = _flat_inputs(tmp_path)
    assert inputs.native_companion_reason == native_companion_status().reason


def test_plan_hash_changes_with_native_companion_reason(tmp_path: Path) -> None:
    from dataclasses import replace

    inputs = _flat_inputs(tmp_path)
    mutated = replace(inputs, native_companion_reason="abi-mismatch")
    assert inputs.plan_hash() != mutated.plan_hash()


def test_plan_hash_changes_with_temp_disk_budget_bytes(tmp_path: Path) -> None:
    from dataclasses import replace

    inputs = _flat_inputs(tmp_path)
    mutated_facts = replace(inputs.out_of_core_facts, temp_disk_budget_bytes=123)
    mutated = replace(inputs, out_of_core_facts=mutated_facts)
    assert inputs.plan_hash() != mutated.plan_hash()


# ---------------------------------------------------------------------------
# MED: submit-boundary validation at capture time.
# ---------------------------------------------------------------------------


def test_capture_raises_invalid_execution_knob_for_string_auto_chunk(tmp_path: Path) -> None:
    """MED counterexample: `auto_chunk="false"` (a string, not a bool) must
    raise the SAME coded error `run_pipeline` raises at its submit boundary
    (`_substrate.require_bool`), not silently produce a compilable
    `full_frame` snapshot."""
    from decoy_engine.execution._errors import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        _flat_inputs(tmp_path, auto_chunk="false")
    assert exc.value.code == "invalid_execution_knob"


def test_capture_raises_invalid_execution_knob_for_non_positive_chunk_size(
    tmp_path: Path,
) -> None:
    from decoy_engine.execution._errors import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        _flat_inputs(tmp_path, chunk_size_rows=0)
    assert exc.value.code == "invalid_execution_knob"


def test_capture_raises_invalid_substrate(tmp_path: Path) -> None:
    from decoy_engine.execution._errors import ExecutionError

    with pytest.raises(ExecutionError) as exc:
        _flat_inputs(tmp_path, substrate="not_a_real_substrate")
    assert exc.value.code == "invalid_substrate"


def test_capture_matches_live_run_pipeline_rejection_for_invalid_knob(tmp_path: Path) -> None:
    """Both `capture_physical_plan_inputs` and `run_pipeline` must raise the
    identical coded error for the identical invalid knob -- the snapshot
    boundary now owns raising it (D3's exclusion stays correct: the compiler
    itself still never emits `invalid_execution_knob`)."""
    from decoy_engine.execution import run_pipeline
    from decoy_engine.execution._errors import ExecutionError

    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})
    path = _write(tmp_path, source, "t")
    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
            "targets": {
                "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "t.out.parquet")}
            },
            "tables": [{"name": "t", "columns": [{"name": "note", "strategy": "redact"}]}],
        }
    ).model_dump()
    with pytest.raises(ExecutionError) as compiler_exc:
        capture_physical_plan_inputs(
            config, {"t": source}, engine_version="unit-test", auto_chunk="false"
        )
    with pytest.raises(ExecutionError) as live_exc:
        run_pipeline(config, {"t": source}, engine_version="unit-test", auto_chunk="false")
    assert compiler_exc.value.code == live_exc.value.code == "invalid_execution_knob"
