"""Task 4.5 D3/D6: the unified-slice admission predicate + activation
overlay, unit-tested directly against the private helpers (no full
`run_pipeline` round-trip needed for these -- the differential harness in
`test_unified_slice_parity.py` covers the end-to-end behavior).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from decoy_engine.execution import _unified_slice_admission
from decoy_engine.execution.physical._activation import build_unified_slice_activation
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._live_inputs import build_live_physical_plan_inputs
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.plan import compile_plan
from decoy_engine.profile import profile_source
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships import RelationshipGraph
from tests.physical._shadow_helpers import build_config, write_read_only_fixture

ENGINE_VERSION = "unified-slice-admission-test"


def _build(tmp_path: Path, columns: list[dict], source: pa.Table) -> tuple[dict, pa.Table]:
    path = write_read_only_fixture(tmp_path, source, "fixture")
    config = build_config(tmp_path, "t", path, columns)
    return config, source


def _profile_and_plan(config: dict, source: pa.Table):
    profile = profile_source(config, seed=20260914)
    plan = compile_plan(config, profile, decoy_engine_version=ENGINE_VERSION)
    return profile, plan


def _cheap_ok(config, profile, source, **overrides):
    kwargs = dict(
        route="full_frame",
        route_chunked=False,
        native_route_enabled=False,
        resolved_substrate="pandas",
        sink=None,
        source_loader=None,
        fidelity_report=False,
        vault_writer=None,
        config=config,
        profile=profile,
        table_kinds={"t": "mask"},
        caller_sources={"t": source},
    )
    kwargs.update(overrides)
    return _unified_slice_admission.cheap_admission(**kwargs)


def test_cheap_admission_admits_the_golden_shape(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    candidate = _cheap_ok(config, profile, source)
    assert candidate is not None
    assert candidate.table == "t"


def test_cheap_admission_declines_non_full_frame_route(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, route="sequential") is None


def test_cheap_admission_declines_when_chunked(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, route_chunked=True) is None


def test_cheap_admission_declines_when_native_route_enabled(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, native_route_enabled=True) is None


def test_cheap_admission_declines_non_pandas_substrate(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    # The lane returns a result identical to the PANDAS full-frame route only.
    # A polars-substrate job runs a different legacy adapter that stamps its own
    # provenance telemetry (executed_substrate, pa<->pl conversion timings), so
    # admitting it would diverge on the caller-consumed quality_metrics. It must
    # fall through to the unchanged old route.
    assert _cheap_ok(config, profile, source, resolved_substrate="polars") is None


def test_cheap_admission_declines_sink_present(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, sink=object()) is None


def test_cheap_admission_declines_source_loader_present(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, source_loader=lambda name: source) is None


def test_cheap_admission_declines_fidelity_report(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, fidelity_report=True) is None


def test_cheap_admission_declines_vault_writer(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, vault_writer=object()) is None


def test_cheap_admission_declines_validators(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    config = dict(config)
    config["validators"] = [{"name": "x"}]
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_relationships(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)

    class _FakeProfile:
        relationships = ({"fake": True},)

    assert _cheap_ok(config, _FakeProfile(), source) is None


def test_cheap_admission_declines_generate_table_present(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, table_kinds={"t": "mask", "g": "generate"}) is None


def test_cheap_admission_declines_non_resident_source(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, caller_sources={}) is None


def test_cheap_admission_declines_transforms_configured(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    config = dict(config)
    config["tables"] = [dict(config["tables"][0], transforms=[{"op": "filter_rows"}])]
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_undeclared_passthrough_column(tmp_path: Path) -> None:
    """D3: configured columns must equal the source schema EXACTLY -- an
    extra source column the config never declares must decline, not admit
    with a silent passthrough."""
    source = pa.table(
        {
            "c": pa.array(["a", "b", "c"], type=pa.string()),
            "extra": pa.array(["x", "y", "z"], type=pa.string()),
        }
    )
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_duplicate_column_declaration(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    columns = [
        {"name": "c", "strategy": "passthrough"},
        {"name": "c", "strategy": "redact"},
    ]
    path = write_read_only_fixture(tmp_path, source, "fixture")
    # build_config's PipelineConfig validation may itself reject a duplicate
    # column name; construct the raw dict directly to exercise the admission
    # check in isolation regardless.
    from decoy_engine.config import PipelineConfig

    raw = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
        "targets": {
            "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "out.parquet")}
        },
        "tables": [{"name": "t", "columns": [{"name": "c", "strategy": "passthrough"}]}],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    # Mutate post-validation to inject the duplicate the schema itself would
    # reject at submit time -- proving the admission check is a real,
    # independent guard, not merely inherited from schema validation.
    config["tables"][0]["columns"] = columns
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_vault_column(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(
        tmp_path, [{"name": "c", "strategy": "hash", "namespace": "n", "vault": True}], source
    )
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_when_gated_column(tmp_path: Path) -> None:
    """Codex final-gate BLOCKER: a `when:` predicate gates masking to only
    the matching rows (`_pandas_adapter.py:405`'s `run_with_when_gate`); the
    coordinator masks the whole array with no row gate, so an admitted
    `when:` column would over-mask. `when` is not a declared `ColumnConfig`
    field (unreachable through `PipelineConfig.model_validate` today, same
    gap the `dtype` field docstring in `config/_tables.py` documents), so
    the raw dict is mutated post-validation to exercise the admission check
    in isolation, matching `test_cheap_admission_declines_duplicate_column_
    declaration`'s established pattern for this exact situation."""
    source = pa.table(
        {
            "c": pa.array(["a", "b", "c"], type=pa.string()),
            "flag": pa.array([1, 0, 1], type=pa.int64()),
        }
    )
    config, source = _build(
        tmp_path,
        [{"name": "c", "strategy": "redact"}, {"name": "flag", "strategy": "passthrough"}],
        source,
    )
    config = dict(config)
    config["tables"][0]["columns"][0]["when"] = "flag == 1"
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_blank_when_left_admitted(tmp_path: Path) -> None:
    """A present-but-blank `when` (the compiler's own `None`-normalization
    shape, `_seed_envelope.py`'s `when_raw if isinstance(...) and when_raw.
    strip() else None`) is not a real gate and must not spuriously decline."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    config = dict(config)
    config["tables"][0]["columns"][0]["when"] = "   "
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is not None


def test_cheap_admission_declines_extra_caller_source(tmp_path: Path) -> None:
    """Codex final-gate BLOCKER: the legacy adapter echoes every resident
    source frame in `outputs` (`_pipeline.py:588`), so a caller that loaded
    an extra table alongside the configured mask table keeps that extra
    table (plus its projection warning) on the old route. This lane returns
    only the admitted table, so it must decline rather than silently drop
    the extra source."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    extra = pa.table({"x": pa.array([1, 2, 3], type=pa.int64())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is not None
    assert _cheap_ok(config, profile, source, caller_sources={"t": source, "extra": extra}) is None


def test_cheap_admission_declines_quarantine_configured(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    config = dict(config)
    config["quarantine"] = {"enabled": True, "triggers": ["validation_fail"]}
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_run_storm(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    config = dict(config)
    config["run_storm"] = True
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


# ---------------------------------------------------------------------------
# Compiled-plan-level admission (needs a real compiled PhysicalPlan).
# ---------------------------------------------------------------------------


def _compile(config: dict, profile, plan, source: pa.Table):
    registry = get_default_registry()
    inputs = build_live_physical_plan_inputs(
        config=config,
        plan=plan,
        profile=profile,
        registry=registry,
        graph=RelationshipGraph(edges=(), ordering=()),
        table_kinds={"t": "mask"},
        caller_sources={"t": source},
        resolved_substrate="pandas",
        execution_mode="auto",
        fidelity_report=False,
        vault_writer_present=False,
        validators=(),
        auto_chunk=True,
        chunk_size_rows=50_000,
        auto_chunk_threshold_rows=100_000,
        out_of_core_threshold_rows=5_000_000,
        full_frame_reject_rows=7_500_000,
        use_byte_estimate_routing=True,
        use_probe_routing=True,
        native_route_enabled=False,
        fpe_chunk_count=4,
        max_workers=4,
        fallback_to_pandas=True,
        out_of_core_reorder_threshold_rows=None,
        out_of_core_budget_bytes=None,
        engine_version=ENGINE_VERSION,
    )
    return compile_physical_plan(inputs), registry


def test_compiled_plan_admission_admits_the_golden_shape(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, _registry = _compile(config, profile, plan, source)
    admitted = _unified_slice_admission.compiled_plan_admission(
        physical_plan, table="t", source=source
    )
    assert admitted is not None
    assert admitted.driver == DriverId.FULL_FRAME
    assert len(admitted.nodes) == 1
    assert admitted.nodes[0].execution is not None


def test_compiled_plan_admission_declines_an_unsupported_strategy(tmp_path: Path) -> None:
    """`fpe` has no entry in the four-operator allowlist, so its node
    compiles with `execution is None` -- the compiled-plan check must
    decline, not admit a partial slice."""
    source = pa.table({"c": pa.array(["12345", "23456", "34567"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "fpe"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, _registry = _compile(config, profile, plan, source)
    assert (
        _unified_slice_admission.compiled_plan_admission(physical_plan, table="t", source=source)
        is None
    )


def test_null_bearing_int_declines_admission(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([1, None, 3], type=pa.int64())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "hash", "namespace": "n"}], source)
    profile, plan = _profile_and_plan(config, source)
    registry = get_default_registry()
    ok = _unified_slice_admission.keyed_hash_and_null_int_admission(
        _compile(config, profile, plan, source)[0].tables[0],
        plan=plan,
        source=source,
        registry=registry,
        graph=RelationshipGraph(edges=(), ordering=()),
        table="t",
    )
    assert ok is False


def _hash_admission_with_mismatched_resident_source(tmp_path: Path, live_source: pa.Table) -> bool:
    """Builds config/profile/plan from a NORMAL string-column fixture (so the
    profile-driven compiler binds the hash node to `native_keyed_hash`, per
    the compiler's own coarse dtype label -> admitted-type mapping), then
    compiles the LIVE physical plan against `live_source` instead -- the
    exact shape a resident caller-supplied table with an exotic Arrow type
    produces: the compiler's operator binding is profile-driven, so it
    cannot see that the RESIDENT array is not what the profile sample
    showed. Mirrors `keyed_hash_and_null_int_admission`'s real call site in
    `_unified_slice._execute_admitted`, which compiles against the live
    source too."""
    profile_source_table = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, _ = _build(
        tmp_path, [{"name": "c", "strategy": "hash", "namespace": "n"}], profile_source_table
    )
    profile, plan = _profile_and_plan(config, profile_source_table)
    registry = get_default_registry()
    physical_plan, _registry = _compile(config, profile, plan, live_source)
    return _unified_slice_admission.keyed_hash_and_null_int_admission(
        physical_plan.tables[0],
        plan=plan,
        source=live_source,
        registry=registry,
        graph=RelationshipGraph(edges=(), ordering=()),
        table="t",
    )


def test_all_null_resident_hash_column_declines_admission(tmp_path: Path) -> None:
    """Codex final-gate BLOCKER: a real all-null `pa.null()` resident column
    profiles as "object" (admitted) but is NOT in the compiled hash kernel's
    admitted-type set (`_requirements._ADMITTED_NATIVE_HASH_TYPES`); the
    profile-only admission check cannot see this, only a check against the
    RESIDENT Arrow dtype can."""
    live_source = pa.table({"c": pa.array([None, None, None], type=pa.null())})
    assert _hash_admission_with_mismatched_resident_source(tmp_path, live_source) is False


def test_dictionary_encoded_resident_hash_column_declines_admission(tmp_path: Path) -> None:
    """Codex final-gate BLOCKER: a dictionary-encoded (categorical) Parquet
    hash column profiles as "category" -> "object" (admitted) but the
    compiled kernel's allowlist has no dictionary type entry."""
    live_source = pa.table({"c": pa.array(["a", "b", "a"], type=pa.string()).dictionary_encode()})
    assert _hash_admission_with_mismatched_resident_source(tmp_path, live_source) is False


def test_resolved_substrate_env_change_after_resolution_does_not_flip_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex final-gate BLOCKER (TOCTOU): `cheap_admission` takes
    `resolved_substrate` as an already-resolved value, never re-reading
    `DECOY_SUBSTRATE` itself. Proves an env change AFTER resolution cannot
    flip the admission decision either way -- the caller's ONE resolution is
    the only thing that matters."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)

    monkeypatch.delenv("DECOY_SUBSTRATE", raising=False)
    admitted_pre = _cheap_ok(config, profile, source, resolved_substrate="pandas")
    assert admitted_pre is not None

    # Flip the env AFTER the caller already resolved "pandas"; admission
    # must still admit -- it never reads the env itself.
    monkeypatch.setenv("DECOY_SUBSTRATE", "polars")
    admitted_post = _cheap_ok(config, profile, source, resolved_substrate="pandas")
    assert admitted_post is not None

    # And the reverse: a resolved "polars" value must still decline even
    # though the env now (again) says something else.
    monkeypatch.setenv("DECOY_SUBSTRATE", "pandas")
    declined = _cheap_ok(config, profile, source, resolved_substrate="polars")
    assert declined is None


# ---------------------------------------------------------------------------
# D6: the activation overlay hash.
# ---------------------------------------------------------------------------


def test_activation_hash_is_deterministic(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, _registry = _compile(config, profile, plan, source)

    a = build_unified_slice_activation(
        physical_plan, table="t", legacy_disposition="full_frame", unified_slice_enabled=True
    )
    b = build_unified_slice_activation(
        physical_plan, table="t", legacy_disposition="full_frame", unified_slice_enabled=True
    )
    assert a.activation_hash == b.activation_hash
    assert a.admitted_nodes == b.admitted_nodes


def test_activation_hash_includes_the_flag(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, _registry = _compile(config, profile, plan, source)

    on = build_unified_slice_activation(
        physical_plan, table="t", legacy_disposition="full_frame", unified_slice_enabled=True
    )
    off = build_unified_slice_activation(
        physical_plan, table="t", legacy_disposition="full_frame", unified_slice_enabled=False
    )
    assert on.activation_hash != off.activation_hash
