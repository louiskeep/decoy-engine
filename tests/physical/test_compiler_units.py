"""D1/D2 unit tests for `PhysicalPlanInputs` (`plan_hash`) and the compiler's
free functions (`relationship_role`,
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
    OutOfCoreRoutingFacts,
    capture_physical_plan_inputs,
)
from decoy_engine.execution.physical._compiler import (
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


def test_captured_config_is_immutable(tmp_path: Path) -> None:
    """Codex final-gate HIGH: the stored `config` must be genuinely immutable
    -- a frozen dataclass stops reassigning the field but not mutating the
    dict it points at, and the compiler re-reads config content via
    `classify_job`, so a post-capture mutation would change the driver while
    `plan_hash` (taken at capture) stayed put. Deep-frozen to
    `MappingProxyType`, so item assignment at any nesting level raises."""
    inputs = _flat_inputs(tmp_path)
    with pytest.raises(TypeError):
        inputs.config["injected"] = "x"  # type: ignore[index]
    # Nested mappings are frozen too, not just the top level.
    with pytest.raises(TypeError):
        inputs.config["global_settings"]["seed"] = 999  # type: ignore[index]


def test_plan_hash_covers_full_config_content(tmp_path: Path) -> None:
    """Codex final-gate HIGH: the identity hash must pin the FULL config the
    compiler consumes (`_canonical_config_json`), not just the subset
    `pipeline_config_hash` folds in -- so two jobs differing in ANY config
    content hash differently. Here the second table masks a second column;
    the immutability test above proves the complementary half (a post-capture
    mutation cannot happen at all)."""
    source = pa.table(
        {
            "note": pa.array(["s1", "s2"], type=pa.string()),
            "memo": pa.array(["m1", "m2"], type=pa.string()),
        }
    )
    path = _write(tmp_path, source, "t")

    def _inputs_with_columns(columns: list[dict[str, Any]]) -> Any:
        config = PipelineConfig.model_validate(
            {
                "version": 1,
                "global_settings": {"seed": 1},
                "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
                "targets": {
                    "t": {
                        "type": "file",
                        "format": "parquet",
                        "path": str(tmp_path / "t.out.parquet"),
                    }
                },
                "tables": [{"name": "t", "columns": columns}],
            }
        ).model_dump()
        return capture_physical_plan_inputs(config, {"t": source}, engine_version="unit-test")

    one_col = _inputs_with_columns([{"name": "note", "strategy": "redact"}])
    two_col = _inputs_with_columns(
        [{"name": "note", "strategy": "redact"}, {"name": "memo", "strategy": "redact"}]
    )
    assert one_col.plan_hash() != two_col.plan_hash()


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


def test_resident_source_fact_lazy_source_marker_is_exact(tmp_path: Path) -> None:
    from decoy_engine.execution.physical._inputs import _resident_source_fact
    from decoy_engine.profile._readers import LazySource

    path = tmp_path / "t.parquet"
    pq.write_table(pa.table({"note": pa.array(["s1"], type=pa.string())}), path)
    assert _resident_source_fact("t", LazySource(path=path)) == ("t", "lazy_source")


def test_resident_source_fact_resident_table_is_exact() -> None:
    """Direct, exact-value pin on `_resident_source_fact` -- name, num_rows,
    and the ordered (column, type, null_count) triples -- so a mutation to
    any one of those (the table name, `columns` dropped, or `str(type)`
    corrupted) fails immediately rather than only showing up as SOME
    difference several layers up in `plan_hash`."""
    from decoy_engine.execution.physical._inputs import _resident_source_fact

    table = pa.table(
        {
            "id": pa.array([1, 2, None], type=pa.int64()),
            "note": pa.array(["a", None, "c"], type=pa.string()),
        }
    )
    assert _resident_source_fact("t", table) == (
        "t",
        3,
        (("id", "int64", 1), ("note", "string", 1)),
    )


def test_plan_hash_changes_when_a_source_is_renamed_with_identical_content(
    tmp_path: Path,
) -> None:
    """Kills the mutant that drops the table NAME from `_resident_source_
    fact`'s returned tuple: two otherwise-identical single-source jobs whose
    only difference is which table the SAME content is keyed under must not
    collide (the source's identity is itself route-affecting -- it is what
    `classify_job`'s `source_tables.get(table)` keys off)."""
    source = pa.table({"note": pa.array(["s1", "s2"], type=pa.string())})

    def _config_and_inputs(table_name: str) -> Any:
        path = _write(tmp_path, source, table_name)
        config = PipelineConfig.model_validate(
            {
                "version": 1,
                "global_settings": {"seed": 1},
                "sources": {table_name: {"type": "file", "format": "parquet", "path": str(path)}},
                "targets": {
                    table_name: {
                        "type": "file",
                        "format": "parquet",
                        "path": str(tmp_path / f"{table_name}.out.parquet"),
                    }
                },
                "tables": [
                    {"name": table_name, "columns": [{"name": "note", "strategy": "redact"}]}
                ],
            }
        ).model_dump()
        return capture_physical_plan_inputs(
            config, {table_name: source}, engine_version="unit-test"
        )

    inputs_t = _config_and_inputs("t")
    inputs_u = _config_and_inputs("u")
    assert inputs_t.plan_hash() != inputs_u.plan_hash()


def test_plan_hash_digest_construction_is_pinned(tmp_path: Path) -> None:
    """Golden-value pin on `compute_plan_hash`'s byte-level construction
    (UTF-8 encoding, the `\\x1f` part separator): an inequality-only
    assertion cannot distinguish a mutant that corrupts the separator or
    encoding IDENTICALLY across every part (still self-consistent, still
    differs when inputs differ) from the correct implementation -- only an
    exact digest value can. Recomputes the identical byte sequence
    `compute_plan_hash` documents itself as constructing (repr + UTF-8 +
    `\\x1f` per part) directly from the frozen snapshot's own fields, so
    this pins the ENCODING, not a duplicate of the field SELECTION (which
    the collision tests above already cover).
    """
    import hashlib

    from decoy_engine.execution.physical._inputs import (
        _canonical_config_json,
        _resident_source_fact,
    )

    inputs = _flat_inputs(tmp_path)
    facts = inputs.out_of_core_facts
    parts: tuple[object, ...] = (
        inputs.plan.pipeline_config_hash,
        inputs.plan.profile_hash,
        _canonical_config_json(inputs.config),
        tuple(
            _resident_source_fact(name, inputs.caller_sources[name])
            for name in sorted(inputs.caller_sources)
        ),
        inputs.resolved_substrate,
        inputs.sink_class_token,
        inputs.source_loader_present,
        inputs.execution_mode,
        inputs.fidelity_report,
        inputs.vault_writer_present,
        len(inputs.validators),
        inputs.auto_chunk,
        inputs.chunk_size_rows,
        inputs.auto_chunk_threshold_rows,
        inputs.out_of_core_threshold_rows,
        inputs.full_frame_reject_rows,
        inputs.use_byte_estimate_routing,
        inputs.use_probe_routing,
        inputs.fpe_chunk_count,
        inputs.max_workers,
        inputs.fallback_to_pandas,
        inputs.registry_fingerprint(),
        tuple(sorted(inputs.table_kinds.items())),
        facts.compatible,
        facts.reject_code,
        facts.largest_table_rows,
        facts.largest_table_rows_exact,
        facts.full_frame_fits_estimate,
        facts.probe_recovers_full_frame,
        facts.budget_bytes,
        facts.reorder_threshold_rows,
        facts.merge_fan_in,
        inputs.native_companion_reason,
    )
    expected = hashlib.sha256()
    for part in parts:
        expected.update(repr(part).encode("utf-8"))
        expected.update(b"\x1f")
    assert inputs.plan_hash() == expected.hexdigest()


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


def test_out_of_core_not_ready_reason_fidelity_report_disqualifies(tmp_path: Path) -> None:
    """Kills mutants that drop `fidelity_report` from the forwarded
    `_sequential_eligible` call (`out_of_core_not_ready_reason` mutating it
    to `None` still passes a falsy value through, silently un-disqualifying
    a fidelity-report job)."""
    from dataclasses import replace

    inputs = _fk_inputs(tmp_path, use_byte_estimate_routing=False)
    assert out_of_core_not_ready_reason(inputs) != "fidelity_report_requested"
    inputs = replace(inputs, fidelity_report=True)
    assert out_of_core_not_ready_reason(inputs) == "fidelity_report_requested"


def test_out_of_core_not_ready_reason_vault_writer_disqualifies(tmp_path: Path) -> None:
    """Kills mutants that force `vault_writer_sentinel`/`vault_writer` to
    `None` regardless of `inputs.vault_writer_present`."""
    from dataclasses import replace

    inputs = _fk_inputs(tmp_path, use_byte_estimate_routing=False)
    assert out_of_core_not_ready_reason(inputs) != "vault_writer_requested"
    inputs = replace(inputs, vault_writer_present=True)
    assert out_of_core_not_ready_reason(inputs) == "vault_writer_requested"


def test_out_of_core_not_ready_reason_generate_plus_mask_disqualifies(tmp_path: Path) -> None:
    """Kills mutants that drop `has_generate_table` from the forwarded call.
    `has_generate_table` is a property derived from `table_kinds`, so a
    generate-kind table is added to an otherwise-unchanged FK snapshot."""
    from dataclasses import replace

    inputs = _fk_inputs(tmp_path, use_byte_estimate_routing=False)
    assert out_of_core_not_ready_reason(inputs) != "generate_plus_mask"
    mutated_kinds = dict(inputs.table_kinds)
    mutated_kinds["extra_generated"] = "generate"
    inputs = replace(inputs, table_kinds=mutated_kinds)
    assert inputs.has_generate_table is True
    assert out_of_core_not_ready_reason(inputs) == "generate_plus_mask"


def test_out_of_core_not_ready_reason_non_pandas_substrate_disqualifies(tmp_path: Path) -> None:
    """Kills mutants that drop `resolved_substrate` from the forwarded call
    (the compiler's own `resolved_substrate=inputs.resolved_substrate`
    kwarg omitted falls back to `_sequential_eligible`'s `"pandas"`
    default, silently un-disqualifying a non-pandas job). Pandas is the only
    valid substrate now, so this exercises the retained fail-closed guard with
    a synthetic non-pandas value."""
    from dataclasses import replace

    inputs = _fk_inputs(tmp_path, use_byte_estimate_routing=False)
    assert out_of_core_not_ready_reason(inputs) != "non_pandas_substrate_requested"
    inputs = replace(inputs, resolved_substrate="future_substrate")
    assert out_of_core_not_ready_reason(inputs) == "non_pandas_substrate_requested"


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


def test_select_driver_full_frame_records_attempted_chunked(
    tmp_path: Path,
) -> None:
    # auto_chunk defaults True (`_pipeline_finalize.AUTO_CHUNK_DEFAULT`), so
    # classify_job runs and declines chunked for the 2-row table (below the
    # default 100k auto-chunk threshold) -- chunked IS attempted here, just
    # not admitted.
    inputs = _flat_inputs(tmp_path)
    selection = select_driver(inputs)
    assert selection.driver == DriverId.FULL_FRAME
    by_driver = {alt.driver: alt for alt in selection.rejected_alternatives}
    assert by_driver[DriverId.CHUNKED].attempted is True
    assert by_driver[DriverId.CHUNKED].reason == "chunked_source_below_threshold"
    # A non-FK table never had out_of_core/sequential as plausible
    # alternatives, so neither appears.
    assert DriverId.OUT_OF_CORE not in by_driver
    assert DriverId.SEQUENTIAL not in by_driver


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
# OutOfCoreRoutingFacts construction sanity
# ---------------------------------------------------------------------------


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


def test_synthesis_stage_config_digest_is_sha256_of_generation_config_json(tmp_path: Path) -> None:
    """Task 4.6 slice 5a (r5 finding-2): `SynthesisStage.config_digest` must
    equal `sha256` of the EXACT UTF-8 bytes of `Plan.generation.config_json`
    -- the identity bind the generation-dispatch admission gate recomputes
    at dispatch time (`_shadow_coordinator._require_generation_shadowable`)."""
    import hashlib

    from decoy_engine.execution.physical._compiler import compile_physical_plan

    config = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 1},
            "sources": {},
            "targets": {"people": {"type": "file", "format": "csv", "path": "out.csv"}},
            "tables": [
                {
                    "name": "people",
                    "row_count": 3,
                    "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
                }
            ],
        }
    ).model_dump()
    inputs = capture_physical_plan_inputs(
        config, {}, engine_version="unit-test", execution_mode="full_frame"
    )
    plan = compile_physical_plan(inputs)

    assert plan.synthesis is not None
    assert inputs.plan.generation is not None
    expected = hashlib.sha256(inputs.plan.generation.config_json.encode("utf-8")).hexdigest()
    assert plan.synthesis.config_digest == expected
