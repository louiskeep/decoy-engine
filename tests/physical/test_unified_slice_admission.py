"""Task 4.5 D3/D6: the unified-slice admission predicate + activation
overlay, unit-tested directly against the private helpers (no full
`run_pipeline` round-trip needed for these -- the differential harness in
`test_unified_slice_parity.py` covers the end-to-end behavior).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.execution import _unified_slice_admission
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.physical._activation import build_unified_slice_activation
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._live_inputs import build_live_physical_plan_inputs
from decoy_engine.execution.physical._plan import PhysicalTable
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.plan import compile_plan
from decoy_engine.profile import profile_source
from decoy_engine.providers_v2 import ProviderRegistry, get_default_registry
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


def test_cheap_admission_admits_inert_sink_on_full_frame(tmp_path: Path) -> None:
    # Route activation (2026-09-20): a sink is inert on the full-frame route
    # (both legacy and admitted paths ignore it; the sink's fate is decided by
    # route). Since the route check above already established route=="full_frame",
    # a golden-shape candidate carrying a sink is now ADMITTED -- this is what
    # lets the platform worker (which always attaches a ParquetTransactionalSink)
    # reach the certified lane.
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    candidate = _cheap_ok(config, profile, source, sink=object())
    assert candidate is not None
    assert candidate.table == "t"


def test_cheap_admission_still_declines_sink_on_non_full_frame(tmp_path: Path) -> None:
    # The route check, not the sink, is what guards streaming: a sink present
    # together with a non-full-frame route (or chunked, or native) still declines,
    # because the streaming/OOC route is the only one that writes the sink.
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source, sink=object(), route="sequential") is None
    assert _cheap_ok(config, profile, source, sink=object(), route_chunked=True) is None
    assert _cheap_ok(config, profile, source, sink=object(), native_route_enabled=True) is None


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
# CHANGE 1: dominating resident admission -- the cheap-admission half.
# ---------------------------------------------------------------------------


class _FakeProfileNoRelationships:
    relationships: tuple[object, ...] = ()


def test_cheap_admission_declines_duplicate_resident_field_names(tmp_path: Path) -> None:
    """Root-cause fix: the coordinator dispatches by NAME-keyed lookup
    (`_shadow_coordinator.py:133-160`), so a duplicate resident field name is
    unsafe regardless of config, and must be caught BEFORE any set-equality
    comparison trusts the resident names as a set. `profile_source` itself
    already rejects a duplicate-named DataFrame (`profile/_walk.py:98`)
    before `run_pipeline` ever reaches admission, so this exercises
    `cheap_admission` directly against a stub profile -- a defense-in-depth
    invariant for any future caller that supplies its OWN Profile alongside
    a duplicate-named resident table, not a reachable real-pipeline shape."""
    source = pa.table([pa.array(["a", "b", "c"]), pa.array(["x", "y", "z"])], names=["c", "c"])
    table_cfg = {"name": "t", "columns": [{"name": "c", "strategy": "passthrough"}]}
    config = {"tables": [table_cfg]}
    assert _cheap_ok(config, _FakeProfileNoRelationships(), source) is None


def test_cheap_admission_declines_metadata_only_named_index(tmp_path: Path) -> None:
    """A resident table whose `b"pandas"` schema metadata designates a
    RANGE index carrying a NAME (`index_columns: [{"kind": "range", "name":
    "myidx", ...}]`) reconstructs a named index on `to_pandas()` with no
    physical index column at all -- `source.column_names` and
    `frame.columns` both still equal the configured surface exactly, so
    only an explicit index-name check catches it. A named index would
    become an extra output column on the legacy route."""
    base = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    meta = {
        "index_columns": [{"kind": "range", "name": "myidx", "start": 0, "stop": 3, "step": 1}],
        "column_indexes": [],
        "columns": [
            {
                "name": "c",
                "field_name": "c",
                "pandas_type": "unicode",
                "numpy_type": "object",
                "metadata": None,
            }
        ],
        "attributes": {},
        "creator": {"library": "pyarrow", "version": "24.0.0"},
        "pandas_version": "2.3.3",
    }
    schema = base.schema.with_metadata({b"pandas": json.dumps(meta).encode()})
    source = base.cast(schema)
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_declines_physical_named_index(tmp_path: Path) -> None:
    """A resident table whose metadata designates an EXISTING data column as
    the index (`index_columns: ["c"]`, no extra physical field): `source.
    column_names` still equals the configured surface exactly (nothing
    "extra" to catch via the undeclared-column check), but `to_pandas()`
    pulls `c` out of `frame.columns` entirely, leaving zero columns."""
    base = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    meta = {
        "index_columns": ["c"],
        "column_indexes": [],
        "columns": [
            {
                "name": "c",
                "field_name": "c",
                "pandas_type": "unicode",
                "numpy_type": "object",
                "metadata": None,
            }
        ],
        "attributes": {},
        "creator": {"library": "pyarrow", "version": "24.0.0"},
        "pandas_version": "2.3.3",
    }
    schema = base.schema.with_metadata({b"pandas": json.dumps(meta).encode()})
    source = base.cast(schema)
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    assert _cheap_ok(config, profile, source) is None


def test_cheap_admission_admits_a_default_range_index(tmp_path: Path) -> None:
    """The positive control for the two decline tests above: an ordinary
    resident table (no pandas index metadata at all) must still admit."""
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)
    candidate = _cheap_ok(config, profile, source)
    assert candidate is not None
    assert list(candidate.source_frame.columns) == ["c"]
    assert candidate.source_frame.index.equals(pd.RangeIndex(3))
    assert candidate.source_frame.index.name is None


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


def _resident_contract(
    physical_plan,
    plan,
    source: pa.Table,
    *,
    registry: ProviderRegistry | None = None,
    table: str = "t",
) -> PhysicalTable | None:
    return _unified_slice_admission.resident_contract_admission(
        physical_plan,
        table=table,
        source=source,
        plan=plan,
        registry=registry if registry is not None else get_default_registry(),
        graph=RelationshipGraph(edges=(), ordering=()),
    )


def test_resident_contract_admission_admits_the_golden_shape(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, registry = _compile(config, profile, plan, source)
    admitted = _resident_contract(physical_plan, plan, source, registry=registry)
    assert admitted is not None
    assert admitted.driver == DriverId.FULL_FRAME
    assert len(admitted.nodes) == 1
    assert admitted.nodes[0].execution is not None


def test_resident_contract_admission_declines_an_unsupported_strategy(tmp_path: Path) -> None:
    """`fpe` has no entry in the four-operator allowlist, so its node
    compiles with `execution is None` -- the resident-contract check must
    decline, not admit a partial slice."""
    source = pa.table({"c": pa.array(["12345", "23456", "34567"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "fpe"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, registry = _compile(config, profile, plan, source)
    assert _resident_contract(physical_plan, plan, source, registry=registry) is None


def test_null_bearing_int_declines_admission(tmp_path: Path) -> None:
    source = pa.table({"c": pa.array([1, None, 3], type=pa.int64())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "hash", "namespace": "n"}], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, registry = _compile(config, profile, plan, source)
    assert _resident_contract(physical_plan, plan, source, registry=registry) is None


def _admitted_column(
    strategy: str, provider_config: dict[str, Any] | None = None
) -> dict[str, Any]:
    col: dict[str, Any] = {"name": "c", "strategy": strategy}
    if strategy == "hash":
        col["namespace"] = "n"
    if provider_config is not None:
        col["provider_config"] = provider_config
    return col


def _admission_with_mismatched_resident_source(
    tmp_path: Path,
    strategy: str,
    live_source: pa.Table,
    *,
    provider_config: dict[str, Any] | None = None,
) -> PhysicalTable | None:
    """Builds config/profile/plan from a NORMAL string-column fixture (so the
    profile-driven compiler binds the node's `input_schema` off a plain
    string label), then compiles the LIVE physical plan against
    `live_source` instead -- the exact shape a resident caller-supplied
    table with an exotic or mismatched Arrow type produces: the compiler's
    operator binding is profile-driven, so it cannot see that the RESIDENT
    array is not what the profile sample showed. Mirrors `resident_contract_
    admission`'s real call site in `_unified_slice._execute_admitted`, which
    compiles against the live source too."""
    profile_source_table = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, _ = _build(
        tmp_path, [_admitted_column(strategy, provider_config)], profile_source_table
    )
    profile, plan = _profile_and_plan(config, profile_source_table)
    physical_plan, registry = _compile(config, profile, plan, live_source)
    return _resident_contract(physical_plan, plan, live_source, registry=registry)


@pytest.mark.parametrize(
    "strategy,provider_config",
    [("passthrough", None), ("redact", None), ("truncate", {"length": 2}), ("hash", None)],
)
def test_profile_resident_mismatch_declines_for_every_strategy(
    tmp_path: Path, strategy: str, provider_config: dict[str, Any] | None
) -> None:
    """CHANGE 3: a resident/profile TYPE mismatch declines regardless of
    which of the four strategies is bound -- not just hash (the prior
    guard's only check)."""
    live_source = pa.table({"c": pa.array(["a", "b", "a"], type=pa.string()).dictionary_encode()})
    assert (
        _admission_with_mismatched_resident_source(
            tmp_path, strategy, live_source, provider_config=provider_config
        )
        is None
    )


_FIXED_SIZE_LIST = pa.array([[1, 2], [3, 4], [5, 6]], type=pa.list_(pa.int64(), 2))
_DICTIONARY = pa.array(["a", "b", "a"], type=pa.string()).dictionary_encode()
_SPARSE_UNION = pa.UnionArray.from_sparse(
    pa.array([0, 1, 0], type=pa.int8()),
    [pa.array([1.1, 2.2, 3.3], type=pa.float64()), pa.array([True, False, True], type=pa.bool_())],
)
_DENSE_UNION = pa.UnionArray.from_dense(
    pa.array([0, 1, 0], type=pa.int8()),
    pa.array([0, 0, 1], type=pa.int32()),
    [pa.array([1.1, 2.2], type=pa.float64()), pa.array([True], type=pa.bool_())],
)
_NESTED_STRUCT = pa.array([{"a": 1}, {"a": 2}, {"a": 3}], type=pa.struct([("a", pa.int64())]))
_NESTED_LIST = pa.array([[1], [2], [3]], type=pa.list_(pa.int64()))


@pytest.mark.parametrize(
    "live_array",
    [_FIXED_SIZE_LIST, _DICTIONARY, _SPARSE_UNION, _DENSE_UNION, _NESTED_STRUCT, _NESTED_LIST],
    ids=[
        "fixed_size_list",
        "dictionary",
        "sparse_union",
        "dense_union",
        "nested_struct",
        "nested_list",
    ],
)
def test_reject_representative_exotic_types(tmp_path: Path, live_array: pa.Array) -> None:
    """CHANGE 1: every one of these types must decline against passthrough
    (the type-preserving strategy, so a same-type profile/resident agreement
    is the ONLY thing at stake) -- none is in the fixed admitted matrix, and
    none equals the compiled `input_schema` type (plain `pa.string()`)
    either."""
    live_source = pa.table({"c": live_array})
    assert _admission_with_mismatched_resident_source(tmp_path, "passthrough", live_source) is None


@pytest.mark.parametrize(
    "strategy,provider_config,arrow_type,values",
    [
        ("redact", None, pa.int64(), [1, 2, 3]),
        ("truncate", {"length": 2}, pa.int64(), [100, 200, 300]),
        ("passthrough", None, pa.float64(), [1.1, 2.2, 3.3]),
        ("passthrough", None, pa.timestamp("us"), [1, 2, 3]),
        ("hash", None, pa.bool_(), [True, False, True]),
    ],
    ids=[
        "redact_int64",
        "truncate_int64",
        "passthrough_float64",
        "passthrough_timestamp",
        "hash_bool",
    ],
)
def test_domain_matrix_declines_out_of_domain_agreement(
    tmp_path: Path,
    strategy: str,
    provider_config: dict[str, Any] | None,
    arrow_type: pa.DataType,
    values: list[Any],
) -> None:
    """CHANGE 1: the fixed per-strategy matrix declines even when the
    resident type EXACTLY matches the compiled `input_schema` type (both
    profile and resident genuinely agree on the same out-of-domain type) --
    the exact-match check alone is not sufficient (the compiler gates
    redact/truncate on CONFIG only, never on input type; `hash_bool` proves
    the matrix is STRICTER than the prior `is_admitted_native_hash_type`
    check, which admitted bool)."""
    source = pa.table({"c": pa.array(values, type=arrow_type)})
    config, source = _build(tmp_path, [_admitted_column(strategy, provider_config)], source)
    profile, plan = _profile_and_plan(config, source)
    physical_plan, registry = _compile(config, profile, plan, source)
    assert _resident_contract(physical_plan, plan, source, registry=registry) is None


@pytest.mark.parametrize(
    "values",
    [[], [None, None, None], ["a@x.com", "b@x.com", "c@x.com"]],
    ids=["empty", "all_null", "value_bearing"],
)
def test_unencodable_hash_namespace_declines(tmp_path: Path, values: list[str | None]) -> None:
    """CHANGE 1: the compiled kernel consumes the namespace at every batch
    invocation, even for an empty/all-null column (unlike the legacy
    per-value `derive()` call, which only ever sees the namespace for a
    non-null value) -- so an unencodable namespace must decline in all
    three data shapes, not only the value-bearing one."""
    source = pa.table({"c": pa.array(values, type=pa.string())})
    config, source = _build(
        tmp_path, [{"name": "c", "strategy": "hash", "namespace": "\ud800bad"}], source
    )
    profile, plan = _profile_and_plan(config, source)
    physical_plan, registry = _compile(config, profile, plan, source)
    assert _resident_contract(physical_plan, plan, source, registry=registry) is None


@pytest.mark.skipif(
    not native_companion_status().ok,
    reason="compiled decoy-engine-native companion unavailable",
)
def test_encodable_hash_namespace_admits(tmp_path: Path) -> None:
    """The positive control for the unencodable-namespace tests above.

    Hash admission preflights the compiled kernel, so it only admits with the
    companion installed; skip on the companion-absent legs.
    """
    source = pa.table({"c": pa.array(["a@x.com", "b@x.com", "c@x.com"], type=pa.string())})
    config, source = _build(
        tmp_path, [{"name": "c", "strategy": "hash", "namespace": "ns"}], source
    )
    profile, plan = _profile_and_plan(config, source)
    physical_plan, registry = _compile(config, profile, plan, source)
    assert _resident_contract(physical_plan, plan, source, registry=registry) is not None


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


def _lying_metadata() -> bytes:
    """The `b"pandas"` schema metadata pandas writes for a StringDtype column,
    for transplanting onto a physically-int64 table."""
    df = pd.DataFrame({"c": pd.array(["1", "2", "3"], dtype="string")})
    return pa.Table.from_pandas(df, preserve_index=False).schema.metadata


def test_cheap_admission_declines_transplanted_lying_pandas_metadata(tmp_path: Path) -> None:
    # A resident int64 column carrying StringDtype metadata reconstructs to
    # strings under to_pandas(); the legacy route would hash those strings while
    # the native kernel hashes the physical integers, so identical schemas
    # produce different tokens. The physical-consistency guard must decline it.
    plain = pa.table({"c": pa.array([1, 2, 3], type=pa.int64())})
    config, _ = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], plain)
    profile, _ = _profile_and_plan(config, plain)
    lying = pa.table({"c": pa.array([1, 2, 3], type=pa.int64())}).replace_schema_metadata(
        _lying_metadata()
    )
    # Sanity: the metadata really does make to_pandas reinterpret the ints as strings.
    assert list(lying.to_pandas()["c"]) == ["1", "2", "3"]
    assert _cheap_ok(config, profile, lying) is None


def test_cheap_admission_declines_non_parquet_source(tmp_path: Path) -> None:
    # D3 scopes the slice to a single non-FK PARQUET file source; csv,
    # fixed_width, and non-file sources profile under a different reader than the
    # resident Arrow table and must decline to the legacy route.
    source = pa.table({"c": pa.array(["a", "b", "c"], type=pa.string())})
    config, source = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], source)
    profile, _ = _profile_and_plan(config, source)

    def _with_source(**changes: Any) -> dict:
        cfg = dict(config)
        cfg["sources"] = {t: {**config["sources"][t], **changes} for t in config["sources"]}
        return cfg

    assert _cheap_ok(config, profile, source) is not None  # baseline: parquet admits
    assert _cheap_ok(_with_source(format="csv"), profile, source) is None
    assert _cheap_ok(_with_source(format="fixed_width"), profile, source) is None
    assert _cheap_ok(_with_source(type="s3"), profile, source) is None
    # A missing source descriptor for the table also declines.
    cfg_no_src = dict(config)
    cfg_no_src["sources"] = {}
    assert _cheap_ok(cfg_no_src, profile, source) is None


def test_cheap_admission_declines_when_to_pandas_raises_on_invalid_metadata(
    tmp_path: Path,
) -> None:
    # Malformed b"pandas" metadata makes to_pandas() raise a JSONDecodeError.
    # Admission must be total-to-decline (fall through to the legacy route's own
    # coded guards), never let that raw error escape.
    plain = pa.table({"c": pa.array([1, 2, 3], type=pa.int64())})
    config, _ = _build(tmp_path, [{"name": "c", "strategy": "passthrough"}], plain)
    profile, _ = _profile_and_plan(config, plain)
    invalid = pa.table({"c": pa.array([1, 2, 3], type=pa.int64())}).replace_schema_metadata(
        {b"pandas": b"{invalid json"}
    )
    assert _cheap_ok(config, profile, invalid) is None
