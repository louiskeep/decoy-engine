"""Track A Option 2 acceptance tests: CSV / fixed_width admission to the
unified-slice lane.

Before this change, `cheap_admission` admitted only a parquet file source
(`_unified_slice_admission.py`'s old format check): a CSV or fixed_width job
declined to the pandas oracle even though both routes mask the SAME resident
Arrow table the platform already loaded. The reason a bare format widening was
unsafe is that the unified-slice compiler used to bind each node's physical
type from the PROFILER's own descriptor-backed re-read, not from that resident
table -- and a loosely-typed reader (a number-looking CSV column read as text,
a fixed-width column the platform has not yet typed) disagrees with the
profiler's own inferred type for the identical data. Making the compiler
resident-Arrow-authoritative (`resolve_input_arrow_type`'s `resident_sources`
argument, threaded from `execution_binding_for_slice_node` through
`requirements_for` and the `*_config_rejection` gates) closes that gap: the
compiled plan and the admission gate always agree with the table both routes
actually mask, for every format.

Layout:
  - Direct, targeted unit tests of `resolve_input_arrow_type` and the
    `*_config_rejection` resolvers, proving resident overrides profile.
  - End-to-end `run_pipeline` admit+parity tests for CSV (number-looking
    columns) and fixed_width sources.
  - Route evidence (`compiled_kernel_executed`) + a poison-pandas non-vacuity
    check under a native-companion-present environment.
  - A passthrough case proving both the input AND output binding schema come
    from resident Arrow, not the profile.
  - The safe parquet route-widening case: a direct caller whose resident table
    differs from the file's own profile now admits instead of declining.
  - A resident-null CSV column, which must still decline.

Scope: engine only (per the plan, the platform LocalRef fixed-width layout fix
is a separate tracked follow-up); the fixed_width case here feeds a resident
Arrow table directly, exactly as a real (already-fixed) platform caller would.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import _pandas_adapter, run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY
from decoy_engine.execution._unified_slice_admission import resident_contract_admission
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.execution.native._operator_config_rejections import (
    bucket_perturb_config_rejection,
    date_shift_config_rejection,
    group_key_config_rejection,
)
from decoy_engine.execution.native._requirements import (
    hash_config_rejection,
    resolve_input_arrow_type,
)
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._live_inputs import build_live_physical_plan_inputs
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.plan import compile_plan
from decoy_engine.profile import ColumnProfile, Profile, TableProfile, profile_source
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships import RelationshipGraph

ENGINE_VERSION = "unified-input-formats-test"
_MASK_KEY = bytes(range(32))


def _key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=_MASK_KEY, key_version="v1")


def _validate(raw: dict[str, Any]) -> dict[str, Any]:
    return PipelineConfig.model_validate(raw).model_dump()


# ---------------------------------------------------------------------------
# Config / fixture builders (csv + fixed_width; `_shadow_helpers.build_config`
# is parquet-only).
# ---------------------------------------------------------------------------


def _csv_config(
    tmp_path: Path, table: str, csv_path: Path, columns: list[dict[str, Any]]
) -> dict[str, Any]:
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260929},
        "sources": {table: {"type": "file", "format": "csv", "path": str(csv_path)}},
        "targets": {
            "t": {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / f"{table}.out.parquet"),
            }
        },
        "tables": [{"name": table, "columns": columns}],
    }
    raw["targets"] = {table: raw["targets"]["t"]}
    return _validate(raw)


def _write_csv(path: Path, rows: dict[str, list[str]]) -> None:
    pd.DataFrame(rows).to_csv(path, index=False)


def _fixed_width_config(
    tmp_path: Path,
    table: str,
    fw_path: Path,
    layout_columns: list[dict[str, Any]],
    columns: list[dict[str, Any]],
) -> dict[str, Any]:
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260929},
        "sources": {
            table: {
                "type": "file",
                "format": "fixed_width",
                "path": str(fw_path),
                "layout": {"columns": layout_columns},
            }
        },
        "targets": {
            table: {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / f"{table}.out.parquet"),
            }
        },
        "tables": [{"name": table, "columns": columns}],
    }
    return _validate(raw)


def _render_fixed_width_row(layout_columns: list[dict[str, Any]], values: dict[str, str]) -> str:
    record_width = max(c["start"] + c["width"] for c in layout_columns)
    chars = [" "] * record_width
    for c in layout_columns:
        raw = str(values[c["name"]])
        width = c["width"]
        pad = c.get("pad", " ")
        align = c.get("align", "left")
        if len(raw) > width:
            raise ValueError(f"{raw!r} does not fit in width {width}")
        field = (
            raw + pad * (width - len(raw)) if align == "left" else pad * (width - len(raw)) + raw
        )
        chars[c["start"] : c["start"] + width] = list(field)
    return "".join(chars)


def _write_fixed_width(
    path: Path, layout_columns: list[dict[str, Any]], rows: list[dict[str, str]]
) -> None:
    with open(path, "w") as fh:
        for row in rows:
            fh.write(_render_fixed_width_row(layout_columns, row) + "\n")


def _run_both(
    config: dict[str, Any],
    sources: dict[str, pa.Table],
    *,
    use_byte_estimate_routing: bool = True,
) -> tuple[ExecutionResult, ExecutionResult]:
    off = run_pipeline(
        config,
        dict(sources),
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=False,
        use_byte_estimate_routing=use_byte_estimate_routing,
    )
    on = run_pipeline(
        config,
        dict(sources),
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=True,
        use_byte_estimate_routing=use_byte_estimate_routing,
    )
    return off, on


def _assert_full_parity(off: ExecutionResult, on: ExecutionResult) -> dict[str, Any]:
    assert set(off.outputs) == set(on.outputs)
    for table in off.outputs:
        off_table, on_table = off.outputs[table], on.outputs[table]
        assert off_table.column_names == on_table.column_names
        assert off_table.num_rows == on_table.num_rows
        for name in off_table.column_names:
            assert off_table.schema.field(name).type == on_table.schema.field(name).type
            assert off_table.column(name).to_pylist() == on_table.column(name).to_pylist()
        assert off_table.schema.equals(on_table.schema, check_metadata=True)
    assert tuple(off.warnings) == tuple(on.warnings)
    assert off.table_kinds == on.table_kinds
    assert tuple(off.row_errors) == tuple(on.row_errors)
    assert QUALITY_METRICS_KEY not in off.quality_metrics
    assert QUALITY_METRICS_KEY in on.quality_metrics, "the unified slice did not admit this job"
    leaf = on.quality_metrics[QUALITY_METRICS_KEY]
    on_without_leaf = {k: v for k, v in on.quality_metrics.items() if k != QUALITY_METRICS_KEY}
    assert on_without_leaf == off.quality_metrics
    assert leaf["activated"] is True
    assert leaf["nodes"], "activation evidence must cover at least one node"
    for evidence in leaf["nodes"].values():
        assert evidence["executed"] is True
    return leaf


_NEEDS_COMPANION = pytest.mark.skipif(
    not native_companion_status().ok, reason="compiled decoy-engine-native companion unavailable"
)


# ---------------------------------------------------------------------------
# Targeted unit tests: resident overrides profile at each physical-type site.
# ---------------------------------------------------------------------------


def _fake_profile(table: str, column: str, dtype: str) -> Profile:
    return Profile(
        schema_version=1,
        tables=(
            TableProfile(
                name=table,
                row_count=3,
                columns=(
                    ColumnProfile(
                        name=column,
                        dtype=dtype,
                        row_count=3,
                        null_count=0,
                        distinct_count=3,
                        sampled=False,
                        is_candidate_key_sampled=False,
                        declared_pk=False,
                        is_fk=False,
                        fk_target=None,
                        pii_class=None,
                    ),
                ),
            ),
        ),
        relationships=(),
        profiled_at=datetime(2026, 9, 29, 0, 0, 0),
        decoy_engine_version="0.1.0",
    )


def test_resolve_input_arrow_type_prefers_resident_over_profile() -> None:
    # The profile says float64 (as a decimal-looking CSV column profiles under
    # pandas' own type inference); the resident source is the platform's actual
    # dtype=str read. Resident wins.
    profile = _fake_profile("t", "amount", "float64")
    resident = {"t": pa.table({"amount": pa.array(["12.50", "8.00"], type=pa.string())})}
    assert resolve_input_arrow_type("t", "amount", profile) == pa.float64()
    assert (
        resolve_input_arrow_type("t", "amount", profile, resident_sources=resident) == pa.string()
    )


def test_resolve_input_arrow_type_falls_back_to_profile_when_resident_absent() -> None:
    profile = _fake_profile("t", "c", "int64")
    # `resident_sources` given but without this table: falls back to profile,
    # not "unknowable" -- only an ABSENT column/table skips the resident path.
    assert resolve_input_arrow_type("t", "c", profile, resident_sources={}) == pa.int64()


def test_hash_config_rejection_admits_via_resident_type_ignoring_stale_profile() -> None:
    # Profile says timedelta64 (unrecognized dtype label -> normally declines
    # as mixed_object_not_native); the resident source is a plain admitted
    # string, which resident-authoritative resolution now sees instead.
    profile = _fake_profile("t", "c", "timedelta64[ns]")
    resident = {"t": pa.table({"c": pa.array(["a", "b"], type=pa.string())})}
    assert hash_config_rejection("c", "t", profile) == "mixed_object_not_native:c"
    assert hash_config_rejection("c", "t", profile, resident_sources=resident) is None


def test_bucket_perturb_config_rejection_admits_via_resident_type() -> None:
    profile = _fake_profile("t", "c", "float64")  # a non-string profile type
    resident = {"t": pa.table({"c": pa.array(["2024-01-01"], type=pa.string())})}
    kwargs = dict(namespace="ns", provider_config={"bucket": "month", "date_format": "%Y-%m-%d"})
    assert bucket_perturb_config_rejection("c", "t", profile, **kwargs) is not None
    assert (
        bucket_perturb_config_rejection("c", "t", profile, resident_sources=resident, **kwargs)
        is None
    )


def test_date_shift_config_rejection_admits_via_resident_type() -> None:
    profile = _fake_profile("t", "c", "float64")
    resident = {"t": pa.table({"c": pa.array(["2024-01-01"], type=pa.string())})}
    kwargs = dict(namespace="ns", provider_config={"date_format": "%Y-%m-%d"})
    assert date_shift_config_rejection("c", "t", profile, **kwargs) is not None
    assert (
        date_shift_config_rejection("c", "t", profile, resident_sources=resident, **kwargs) is None
    )


def test_group_key_config_rejection_admits_via_resident_sibling_type() -> None:
    profile = _fake_profile("t", "gb", "float64")  # float declines
    resident = {"t": pa.table({"gb": pa.array(["a", "b"], type=pa.string())})}
    kwargs: dict[str, Any] = dict(provider_config={"group_by": "gb", "length": 16})
    assert group_key_config_rejection("k", "t", profile, **kwargs) is not None
    assert (
        group_key_config_rejection("k", "t", profile, resident_sources=resident, **kwargs) is None
    )


# ---------------------------------------------------------------------------
# End-to-end: CSV, number-looking columns, hash/redact/truncate/passthrough.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
def test_csv_number_columns_admit_and_match_pandas(tmp_path: Path) -> None:
    """The end-goal case: a real CSV job with an integer-looking column AND a
    decimal-looking column, masked with `hash` (the config strategy name, not
    `keyed_hash`), plus redact/truncate/passthrough on text -- admits to the
    unified-slice lane and matches the pandas oracle exactly, because both
    routes mask the identical resident (string-typed) table."""
    csv_path = tmp_path / "src.csv"
    _write_csv(
        csv_path,
        {
            "id": ["1001", "1002", "1003"],
            "amount": ["12.50", "8.00", "19.99"],
            "name": ["alice", "bob", "carol"],
            "note": ["hello", "world", "there"],
            "raw": ["p0", "p1", "p2"],
        },
    )
    # The platform loads CSV as dtype=str (all columns string), which is what
    # creates the profile/resident divergence for id/amount -- pandas' own CSV
    # type inference (used by the profiler) would otherwise see int64/float64.
    resident = pa.table(
        {
            "id": pa.array(["1001", "1002", "1003"], type=pa.string()),
            "amount": pa.array(["12.50", "8.00", "19.99"], type=pa.string()),
            "name": pa.array(["alice", "bob", "carol"], type=pa.string()),
            "note": pa.array(["hello", "world", "there"], type=pa.string()),
            "raw": pa.array(["p0", "p1", "p2"], type=pa.string()),
        }
    )
    columns = [
        {"name": "id", "strategy": "hash", "namespace": "ns_id"},
        {"name": "amount", "strategy": "hash", "namespace": "ns_amount"},
        {"name": "name", "strategy": "redact"},
        {"name": "note", "strategy": "truncate", "provider_config": {"length": 2}},
        {"name": "raw", "strategy": "passthrough"},
    ]
    config = _csv_config(tmp_path, "t", csv_path, columns)

    # Sanity: the profiler really does disagree with the resident table here
    # (the crux this fix closes), so this test is exercising the real gap.
    profile = profile_source(config, seed=20260929)
    (id_col,) = [c for c in profile.tables[0].columns if c.name == "id"]
    (amount_col,) = [c for c in profile.tables[0].columns if c.name == "amount"]
    assert id_col.dtype == "int64"
    assert amount_col.dtype == "float64"

    off, on = _run_both(config, {"t": resident})
    leaf = _assert_full_parity(off, on)
    if native_companion_status().ok:
        for evidence in leaf["nodes"].values():
            if evidence["operator"] == "native_keyed_hash":
                assert evidence["compiled_kernel_executed"] is True


@_NEEDS_COMPANION
def test_csv_number_column_hash_route_evidence_and_non_vacuity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route evidence + poison-pandas non-vacuity, isolated to the hash node:
    proves the compiled kernel actually ran (not a silent fallback) and that
    the flag-on admitted path never calls the legacy pandas adapter."""
    csv_path = tmp_path / "src.csv"
    _write_csv(csv_path, {"id": ["1001", "1002", "1003"]})
    resident = pa.table({"id": pa.array(["1001", "1002", "1003"], type=pa.string())})
    columns = [{"name": "id", "strategy": "hash", "namespace": "ns_id"}]
    config = _csv_config(tmp_path, "t", csv_path, columns)

    def _poisoned_run(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy PandasExecutionAdapter.run must not run on an admitted job")

    monkeypatch.setattr(_pandas_adapter.PandasExecutionAdapter, "run", _poisoned_run)

    on = run_pipeline(
        config,
        {"t": resident},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=True,
    )
    assert QUALITY_METRICS_KEY in on.quality_metrics
    leaf = on.quality_metrics[QUALITY_METRICS_KEY]
    assert leaf["activated"] is True
    (evidence,) = leaf["nodes"].values()
    assert evidence["operator"] == "native_keyed_hash"
    assert evidence["executed"] is True
    assert evidence["compiled_kernel_executed"] is True


# ---------------------------------------------------------------------------
# End-to-end: CSV, wider native-strategy coverage (categorical, bucket_perturb,
# date_shift, group_key) over plain-string columns.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
def test_csv_wide_strategy_coverage_admits_and_matches_pandas(tmp_path: Path) -> None:
    csv_path = tmp_path / "src.csv"
    _write_csv(
        csv_path,
        {
            "cat": ["red", "green", "blue", "red"],
            "event_date": ["2024-03-15", "2024-06-01", "2024-11-20", "2024-01-05"],
            "signup_date": ["2023-05-10", "2023-08-22", "2023-12-01", "2023-02-14"],
            "gb": ["g1", "g2", "g1", "g2"],
            "gk": ["seed", "seed", "seed", "seed"],
        },
    )
    resident = pa.table(
        {
            "cat": pa.array(["red", "green", "blue", "red"], type=pa.string()),
            "event_date": pa.array(
                ["2024-03-15", "2024-06-01", "2024-11-20", "2024-01-05"], type=pa.string()
            ),
            "signup_date": pa.array(
                ["2023-05-10", "2023-08-22", "2023-12-01", "2023-02-14"], type=pa.string()
            ),
            "gb": pa.array(["g1", "g2", "g1", "g2"], type=pa.string()),
            "gk": pa.array(["seed", "seed", "seed", "seed"], type=pa.string()),
        }
    )
    columns = [
        {
            "name": "cat",
            "strategy": "categorical",
            "namespace": "ns_cat",
            "deterministic": True,
            "provider_config": {"categories": ["red", "green", "blue"]},
        },
        {
            "name": "event_date",
            "strategy": "bucket_perturb",
            "namespace": "ns_bp",
            "provider_config": {"bucket": "month", "date_format": "%Y-%m-%d"},
        },
        {
            "name": "signup_date",
            "strategy": "date_shift",
            "namespace": "ns_ds",
            "provider_config": {"date_format": "%Y-%m-%d"},
        },
        {"name": "gb", "strategy": "passthrough"},
        {
            "name": "gk",
            "strategy": "group_key",
            "provider_config": {"group_by": "gb", "length": 16},
        },
    ]
    config = _csv_config(tmp_path, "t", csv_path, columns)
    off, on = _run_both(config, {"t": resident})
    leaf = _assert_full_parity(off, on)
    if native_companion_status().ok:
        operators = {ev["operator"] for ev in leaf["nodes"].values()}
        assert {
            "native_categorical",
            "native_bucket_perturb",
            "native_date_shift",
            "native_group_key",
            "native_passthrough",
        } <= operators


# ---------------------------------------------------------------------------
# End-to-end: fixed_width, feeding a resident Arrow table directly (the
# platform LocalRef layout fix is a separate tracked follow-up; this proves
# the engine-level admission + typing works once that platform fix lands).
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
def test_fixed_width_number_column_admits_and_matches_pandas(tmp_path: Path) -> None:
    layout_columns = [
        {"name": "id", "start": 0, "width": 6, "type": "int"},
        {"name": "name", "start": 6, "width": 10, "type": "str"},
    ]
    fw_path = tmp_path / "src.txt"
    _write_fixed_width(
        fw_path,
        layout_columns,
        [
            {"id": "100001", "name": "Alice"},
            {"id": "100002", "name": "Bob"},
            {"id": "100003", "name": "Carol"},
        ],
    )
    # The platform has not yet typed this column (or, per the plan, the
    # LocalRef layout fix is a separate follow-up): the resident table is
    # string-typed even though the layout declares `id` as `int`.
    resident = pa.table(
        {
            "id": pa.array(["100001", "100002", "100003"], type=pa.string()),
            "name": pa.array(["Alice", "Bob", "Carol"], type=pa.string()),
        }
    )
    columns = [
        {"name": "id", "strategy": "hash", "namespace": "ns_fw"},
        {"name": "name", "strategy": "passthrough"},
    ]
    config = _fixed_width_config(tmp_path, "t", fw_path, layout_columns, columns)

    # Sanity: the profiler really does read `id` as int64 per the declared
    # layout type, disagreeing with the string-typed resident table.
    profile = profile_source(config, seed=20260929)
    (id_col,) = [c for c in profile.tables[0].columns if c.name == "id"]
    assert id_col.dtype == "int64"

    off, on = _run_both(config, {"t": resident})
    leaf = _assert_full_parity(off, on)
    if native_companion_status().ok:
        for evidence in leaf["nodes"].values():
            if evidence["operator"] == "native_keyed_hash":
                assert evidence["compiled_kernel_executed"] is True


# ---------------------------------------------------------------------------
# Passthrough: both input AND output binding schema come from resident Arrow.
# ---------------------------------------------------------------------------


def test_passthrough_input_and_output_schema_are_resident_not_profile(tmp_path: Path) -> None:
    csv_path = tmp_path / "src.csv"
    _write_csv(csv_path, {"flag": ["1", "0", "1"]})
    # The profiler infers int64 for this int-looking column; the resident
    # table is the platform's actual dtype=str read.
    resident = pa.table({"flag": pa.array(["1", "0", "1"], type=pa.string())})
    columns = [{"name": "flag", "strategy": "passthrough"}]
    config = _csv_config(tmp_path, "t", csv_path, columns)

    profile = profile_source(config, seed=20260929)
    (flag_col,) = [c for c in profile.tables[0].columns if c.name == "flag"]
    assert flag_col.dtype == "int64"  # the profile's own (unused) type

    plan = compile_plan(config, profile, decoy_engine_version=ENGINE_VERSION)
    registry = get_default_registry()
    inputs = build_live_physical_plan_inputs(
        config=config,
        plan=plan,
        profile=profile,
        registry=registry,
        graph=RelationshipGraph(edges=(), ordering=()),
        table_kinds={"t": "mask"},
        caller_sources={"t": resident},
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
        fpe_chunk_count=4,
        max_workers=4,
        fallback_to_pandas=True,
        out_of_core_reorder_threshold_rows=None,
        out_of_core_budget_bytes=None,
        engine_version=ENGINE_VERSION,
    )
    physical_plan = compile_physical_plan(inputs)
    admitted = resident_contract_admission(
        physical_plan,
        table="t",
        source=resident,
        plan=plan,
        registry=registry,
        graph=RelationshipGraph(edges=(), ordering=()),
    )
    assert admitted is not None
    (node,) = admitted.nodes
    binding = node.execution
    assert binding is not None
    # Resident-authoritative on BOTH sides: string (the resident type), never
    # int64 (the profile's type, which is unused for this lane).
    assert binding.input_schema.field("flag").type == pa.string()
    assert binding.output_schema.field("flag").type == pa.string()


# ---------------------------------------------------------------------------
# Safe parquet route-widening: a direct caller's resident table differs from
# the descriptor file's own profile. This now admits (Track A Option 2, folded
# plan-gate item 3); it is a route-widening, not a regression, because both
# routes still mask the identical caller-supplied resident table.
# ---------------------------------------------------------------------------


def test_parquet_direct_caller_resident_mismatch_admits_and_matches(tmp_path: Path) -> None:
    from tests.physical._shadow_helpers import build_config, write_read_only_fixture

    # The file on disk (and thus the profile) is string-typed...
    file_source = pa.table({"c": pa.array(["1", "2", "3"], type=pa.string())})
    path = write_read_only_fixture(tmp_path, file_source, "fixture")
    columns = [{"name": "c", "strategy": "passthrough"}]
    config = build_config(tmp_path, "t", path, columns)

    # ...but the caller supplies a DIFFERENT resident table for this run: int64,
    # not string. Both routes mask this SAME caller-supplied table (neither
    # route re-reads the file), so this is safe to admit.
    resident = pa.table({"c": pa.array([1, 2, 3], type=pa.int64())})

    # `use_byte_estimate_routing` reads the resident sample as the PROFILE's own
    # declared type (`_mem_estimate_schema.sample_average_string_bytes` expects
    # a string column whenever the profile calls the column variable-width);
    # this is a separate, pre-existing byte-estimate-routing limitation,
    # unrelated to unified-slice admission, that a deliberately-mismatched
    # direct-caller resident type trips regardless of which route runs. Not
    # this plan's scope (it is not part of `_unified_slice_admission.py` or the
    # physical compiler); disabled here so the test isolates what this plan DID
    # change.
    off, on = _run_both(config, {"t": resident}, use_byte_estimate_routing=False)
    _assert_full_parity(off, on)
    assert on.outputs["t"].column("c").to_pylist() == [1, 2, 3]


# ---------------------------------------------------------------------------
# MUST-DECLINE: a resident null-typed CSV column (no declared dtype at all,
# e.g. a wholly-empty column) is outside every strategy's admitted-type
# domain and must decline the whole table, fail-closed.
# ---------------------------------------------------------------------------


def test_csv_all_null_column_declines_to_pandas_route(tmp_path: Path) -> None:
    csv_path = tmp_path / "src.csv"
    _write_csv(csv_path, {"x": ["", "", ""], "y": ["a", "b", "c"]})
    resident = pa.table(
        {
            # No explicit type: pyarrow infers `null` for an all-None array,
            # exactly the "no declared dtype" resident shape this guards.
            "x": pa.array([None, None, None]),
            "y": pa.array(["a", "b", "c"], type=pa.string()),
        }
    )
    assert resident.schema.field("x").type == pa.null()
    columns = [
        {"name": "x", "strategy": "passthrough"},
        {"name": "y", "strategy": "passthrough"},
    ]
    config = _csv_config(tmp_path, "t", csv_path, columns)
    off, on = _run_both(config, {"t": resident})
    assert off.outputs["t"].column("y").to_pylist() == on.outputs["t"].column("y").to_pylist()
    assert QUALITY_METRICS_KEY not in on.quality_metrics


# ---------------------------------------------------------------------------
# MUST-DECLINE: binding types come from the table the plan compiled against,
# so if that is ever not the exact object admission hands the lane to mask,
# the lane must decline rather than trust a type it did not check.
# ---------------------------------------------------------------------------


@_NEEDS_COMPANION
def test_compiled_source_not_admitted_source_declines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decoy_engine.execution.physical import _live_inputs

    csv_path = tmp_path / "src.csv"
    _write_csv(csv_path, {"id": ["1001", "1002", "1003"]})
    resident = pa.table({"id": pa.array(["1001", "1002", "1003"], type=pa.string())})
    config = _csv_config(
        tmp_path, "t", csv_path, [{"name": "id", "strategy": "hash", "namespace": "ns_id"}]
    )

    _, admitted = _run_both(config, {"t": resident})
    assert QUALITY_METRICS_KEY in admitted.quality_metrics

    real_build = _live_inputs.build_live_physical_plan_inputs

    def _swapped_build(*args: Any, **kwargs: Any) -> Any:
        kwargs["caller_sources"] = {
            name: pa.table(table.columns, schema=table.schema)
            for name, table in kwargs["caller_sources"].items()
        }
        return real_build(*args, **kwargs)

    monkeypatch.setattr(_live_inputs, "build_live_physical_plan_inputs", _swapped_build)
    off, on = _run_both(config, {"t": resident})
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert off.outputs["t"].equals(on.outputs["t"])


# ---------------------------------------------------------------------------
# Edge values through the platform's own readers: CSV via read_csv(dtype=str),
# fixed-width via the engine reader the platform calls. Leading zeros, blank
# and whitespace-only cells, Unicode, and padded fields must stay byte-parity.
# ---------------------------------------------------------------------------


def _assert_hash_kernels_ran(leaf: dict[str, Any], expected: int) -> None:
    if not native_companion_status().ok:
        return
    hashed = [e for e in leaf["nodes"].values() if e["operator"] == "native_keyed_hash"]
    assert len(hashed) == expected
    assert all(e["compiled_kernel_executed"] is True for e in hashed)


@_NEEDS_COMPANION
def test_csv_edge_values_from_platform_reader_admit_and_match(tmp_path: Path) -> None:
    csv_path = tmp_path / "edge.csv"
    csv_path.write_text(
        'id,name,note,raw\n00123,  José ,héllo wörld,x\n00456,,  ,\n07890,Zoë,"a,b",  lead\n',
        encoding="utf-8",
    )
    resident = pa.Table.from_pandas(pd.read_csv(csv_path, dtype=str), preserve_index=False)
    assert resident.column("id").to_pylist() == ["00123", "00456", "07890"]
    assert resident.column("name").to_pylist()[1] is None
    columns = [
        {"name": "id", "strategy": "hash", "namespace": "ns_id"},
        {"name": "name", "strategy": "redact"},
        {"name": "note", "strategy": "truncate", "provider_config": {"length": 3}},
        {"name": "raw", "strategy": "passthrough"},
    ]
    config = _csv_config(tmp_path, "t", csv_path, columns)
    off, on = _run_both(config, {"t": resident})
    leaf = _assert_full_parity(off, on)
    _assert_hash_kernels_ran(leaf, expected=1)
    out = on.outputs["t"]
    assert out.column("note").to_pylist() == ["hél", "  ", "a,b"]
    assert out.column("raw").to_pylist() == ["x", None, "  lead"]


@_NEEDS_COMPANION
def test_fixed_width_padded_fields_from_engine_reader_admit_and_match(tmp_path: Path) -> None:
    from decoy_engine.profile._fixed_width_reader import read_fixed_width

    layout_columns = [
        {"name": "id", "start": 0, "width": 6, "type": "int", "align": "right", "pad": "0"},
        {"name": "code", "start": 6, "width": 6, "type": "str", "align": "right"},
        {"name": "name", "start": 12, "width": 8, "type": "str"},
    ]
    fw_path = tmp_path / "edge.txt"
    _write_fixed_width(
        fw_path,
        layout_columns,
        [
            {"id": "42", "code": "007", "name": "Zoë"},
            {"id": "100001", "code": "A1", "name": "  x"},
            {"id": "7", "code": "", "name": ""},
        ],
    )
    resident = pa.Table.from_pandas(
        read_fixed_width(str(fw_path), {"columns": layout_columns}), preserve_index=False
    )
    assert resident.schema.field("id").type == pa.int64()
    columns = [
        {"name": "id", "strategy": "hash", "namespace": "ns_fw_id"},
        {"name": "code", "strategy": "hash", "namespace": "ns_fw_code"},
        {"name": "name", "strategy": "passthrough"},
    ]
    config = _fixed_width_config(tmp_path, "t", fw_path, layout_columns, columns)
    off, on = _run_both(config, {"t": resident})
    leaf = _assert_full_parity(off, on)
    _assert_hash_kernels_ran(leaf, expected=2)
    assert on.outputs["t"].column("name").to_pylist() == resident.column("name").to_pylist()
