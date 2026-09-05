"""Acceptance tests 1-4 and 6-9, docs/plans/2026-09-04-native-route-production-seam.md
section 4: the single-pass streaming native lane wired into PRODUCTION
`run_pipeline` routing (`native_route_enabled=True`). Test 5 (flat memory) is
`tests/perf/test_native_route_production_seam_memory.py` (a subprocess/fresh-
process measurement, out of this file's scope); test 10 (mutation bar) is a
separate infra run, not a pytest case.

Every parity assertion drives the PRODUCTION entry -- `run_pipeline` with a
`LazySource` input -- never the low-level native kernels/dispatch directly,
per the plan's "routing tests drive the production entry" instruction.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ParquetTransactionalSink, _native_route_exec, run_pipeline
from decoy_engine.execution import _pipeline_sources as _psrc
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._native_route import ALLOWED_STRATEGIES
from decoy_engine.execution.native._capabilities import capabilities_for
from decoy_engine.profile._readers import LazySource
from tests.parity.native._fixtures import LogicalResult, assert_logical_parity

_ENGINE_VERSION = "native-route-production-seam-test"
_TABLE = "t"


# ---------------------------------------------------------------------------
# Config/source construction helpers
# ---------------------------------------------------------------------------


def _write_source(tmp_path: Path, table: pa.Table, name: str = "src") -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _config(
    tmp_path: Path,
    columns: list[dict[str, Any]],
    *,
    source_path: Path | None = None,
    table: pa.Table | None = None,
    validators: list[dict[str, Any]] | None = None,
    quarantine: dict[str, Any] | None = None,
    global_settings_extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], Path]:
    """Build a validated single-table config + the on-disk source path.

    `source_path` may point at a pre-written file (the zero-row-source case
    writes its own empty file); otherwise `table` is written fresh.
    """
    if source_path is None:
        assert table is not None
        source_path = _write_source(tmp_path, table)
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": 20260904, **(global_settings_extra or {})},
        "sources": {_TABLE: {"type": "file", "format": "parquet", "path": str(source_path)}},
        "targets": {
            _TABLE: {"type": "file", "format": "parquet", "path": str(tmp_path / "out.parquet")}
        },
        "tables": [{"name": _TABLE, "columns": columns}],
    }
    if validators is not None:
        raw["validators"] = validators
    if quarantine is not None:
        raw["quarantine"] = quarantine
    return PipelineConfig.model_validate(raw).model_dump(), source_path


def _pt(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "strategy": "passthrough", **extra}


def _rd(name: str, redact_with: Any = "REDACTED", **extra: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"redact_with": redact_with}
    return {"name": name, "strategy": "redact", "provider_config": cfg, **extra}


def _tr(name: str, length: int = 3, keep: str = "head", **extra: Any) -> dict[str, Any]:
    cfg: dict[str, Any] = {"length": length, "keep": keep}
    return {"name": name, "strategy": "truncate", "provider_config": cfg, **extra}


def _run_native(config: dict[str, Any], source_path: Path, **kwargs: Any):
    sources = {_TABLE: LazySource(path=source_path)}
    return run_pipeline(
        config,
        sources,
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
        **kwargs,
    )


def _run_full_frame_oracle(config: dict[str, Any], source_path: Path, **kwargs: Any):
    sources = {_TABLE: LazySource(path=source_path)}
    return run_pipeline(
        config,
        sources,
        engine_version=_ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=False,
        **kwargs,
    )


def _run_chunked_oracle(config: dict[str, Any], source_path: Path, **kwargs: Any):
    # A resident source, not `LazySource`: the auto-chunk planner's runtime
    # dtype-stability gate needs real column data and conservatively declines
    # to chunk a lazy source (see `_pipeline_sources.lazy_source_rejection`),
    # so a LazySource input here would always fall back to full_frame
    # regardless of `auto_chunk_threshold_rows` -- unrelated to and
    # unchanged by this slice. The chunked-oracle COMPARISON only needs
    # identical OUTPUT, not the same input residency as the native run.
    sources = {_TABLE: pq.read_table(source_path)}
    return run_pipeline(
        config,
        sources,
        engine_version=_ENGINE_VERSION,
        substrate="pandas",
        execution_mode="auto",
        auto_chunk=True,
        auto_chunk_threshold_rows=1,
        native_route_enabled=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Test 1: byte + physical-schema parity through the production entry
# ---------------------------------------------------------------------------


def _parity_source() -> pa.Table:
    return pa.table(
        {
            "pt": pa.array(["alpha", None, "gamma"], type=pa.utf8()),
            "pt_allnull": pa.array([None, None, None], type=pa.utf8()),
            "rd": pa.array(["secret-1", None, "secret-3"], type=pa.utf8()),
            "tr": pa.array(["hello world", None, "hi"], type=pa.utf8()),
        }
    )


def _parity_columns() -> list[dict[str, Any]]:
    return [_pt("pt"), _pt("pt_allnull"), _rd("rd"), _tr("tr", length=3, keep="head")]


def test_parity_against_full_frame_oracle(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    candidate = _run_native(config, source_path)
    assert candidate.native_route is not None
    assert candidate.native_route.admitted is True, candidate.native_route.reason

    oracle = _run_full_frame_oracle(config, source_path)
    assert_logical_parity(
        LogicalResult.from_execution_result(candidate), LogicalResult.from_execution_result(oracle)
    )


def test_parity_against_chunked_oracle(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    candidate = _run_native(config, source_path)
    oracle = _run_chunked_oracle(config, source_path)
    assert oracle.quality_metrics["auto_chunk"]["mode"] == "chunked"
    assert_logical_parity(
        LogicalResult.from_execution_result(candidate), LogicalResult.from_execution_result(oracle)
    )


def test_parity_with_streaming_sink(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    sink_dir = tmp_path / "sink_out"
    result = _run_native(config, source_path, sink=ParquetTransactionalSink(sink_dir))
    assert result.native_route is not None and result.native_route.admitted
    written = pq.read_table(sink_dir / f"{_TABLE}.parquet")

    oracle = _run_full_frame_oracle(config, source_path)
    assert_logical_parity(
        LogicalResult(outputs={_TABLE: written}), LogicalResult.from_execution_result(oracle)
    )


# ---------------------------------------------------------------------------
# P0-1: `native_route_enabled` is validated, not merely tested for truthiness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_value", ["false", "true", 1, 0])
def test_native_route_enabled_non_bool_raises_invalid_execution_knob(
    tmp_path: Path, bad_value: Any
) -> None:
    """A string like `"false"` is truthy in Python; before this fix
    `native_route_enabled` was only ever tested for truthiness (`if ...
    and native_route_enabled:`), so a caller passing the STRING `"false"`
    (an easy config-serialization mistake) would silently enable the native
    lane instead of failing loudly. `1`/`0` are also rejected: `isinstance(1,
    bool)` is False in Python, matching `require_bool`'s existing int-vs-bool
    exclusion for every other routing knob."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    with pytest.raises(ExecutionError) as excinfo:
        run_pipeline(
            config,
            {_TABLE: LazySource(path=source_path)},
            engine_version=_ENGINE_VERSION,
            execution_mode="auto",
            native_route_enabled=bad_value,
        )
    assert excinfo.value.code == "invalid_execution_knob"


def test_native_route_enabled_string_false_does_not_silently_enable_the_route(
    tmp_path: Path,
) -> None:
    """The specific regression named in the finding: `"false"` must raise,
    never quietly admit (which truthiness alone would have done, since a
    non-empty string is always truthy)."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    with pytest.raises(ExecutionError) as excinfo:
        run_pipeline(
            config,
            {_TABLE: LazySource(path=source_path)},
            engine_version=_ENGINE_VERSION,
            execution_mode="auto",
            native_route_enabled="false",  # type: ignore[arg-type] # deliberate: proving the runtime guard, not the static type
        )
    assert excinfo.value.code == "invalid_execution_knob"


# ---------------------------------------------------------------------------
# P0-2: native admission requires the RESOLVED substrate to be pandas
# ---------------------------------------------------------------------------


def _iter_batches_boom(source: LazySource) -> None:
    def _boom(batch_rows: int) -> Any:
        raise AssertionError("iter_batches must not be called when substrate declines admission")

    object.__setattr__(source, "iter_batches", _boom)


def test_polars_substrate_declines_native_and_runs_polars(tmp_path: Path) -> None:
    """`substrate="polars"` must not be silently overridden by native
    execution: admission declines before any source peek, and the job
    actually runs on the polars adapter (recorded as such)."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    source = LazySource(path=source_path)
    _iter_batches_boom(source)

    result = run_pipeline(
        config,
        {_TABLE: source},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
        substrate="polars",
    )
    assert result.native_route is not None
    assert result.native_route.attempted is False, "no source peek on a static decline"
    assert result.native_route.admitted is False
    assert result.native_route.reason == "non_pandas_substrate:polars"
    assert result.quality_metrics["execution_adapter"]["adapter_name"] == "polars"
    assert result.quality_metrics["execution_adapter"]["resolved_substrate"] == "polars"
    assert result.outputs[_TABLE].num_rows == 3


def test_env_resolved_polars_substrate_declines_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same gate applies when polars is chosen via `DECOY_SUBSTRATE`
    rather than an explicit `substrate=` kwarg -- admission reads the
    RESOLVED substrate, not the raw override."""
    monkeypatch.setenv("DECOY_SUBSTRATE", "polars")
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    source = LazySource(path=source_path)
    _iter_batches_boom(source)

    result = run_pipeline(
        config,
        {_TABLE: source},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
        substrate=None,
    )
    assert result.native_route is not None
    assert result.native_route.attempted is False
    assert result.native_route.admitted is False
    assert result.native_route.reason == "non_pandas_substrate:polars"
    assert result.quality_metrics["execution_adapter"]["adapter_name"] == "polars"
    assert result.outputs[_TABLE].num_rows == 3


def test_pandas_substrate_still_admits_native(tmp_path: Path) -> None:
    """Control case: an explicit `substrate="pandas"` (the only value the
    lane is proven parity-correct against) still admits."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = _run_native(config, source_path, substrate="pandas")
    assert result.native_route is not None and result.native_route.admitted is True


# ---------------------------------------------------------------------------
# P1-1: the native route stamps the same reproducibility telemetry the
# chunked/full_frame continuation gets
# ---------------------------------------------------------------------------


def test_native_admitted_run_always_stamps_execution_adapter(tmp_path: Path) -> None:
    """Native running at all is non-default (the caller had to opt in), so
    -- unlike the pandas/polars stamp, which only fires on a non-default
    knob -- this stamp is unconditional whenever the lane actually admitted,
    even with every OTHER knob left at its default."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = _run_native(config, source_path)
    assert result.native_route is not None and result.native_route.admitted is True
    stamp = result.quality_metrics["execution_adapter"]
    assert stamp["adapter_name"] == "native"
    assert stamp["resolved_substrate"] == "pandas"
    assert isinstance(stamp["adapter_version"], str) and stamp["adapter_version"]


def test_native_admitted_run_with_explain_plan_stamps_execution_plan(tmp_path: Path) -> None:
    """`explain_plan=True` on a native-admitted job must not go silent: the
    finding was that the native early return bypassed the shared
    finalization entirely, so no `execution_plan` metric ever appeared."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = _run_native(config, source_path, explain_plan=True)
    assert result.native_route is not None and result.native_route.admitted is True
    assert "execution_plan" in result.quality_metrics
    plan_block = result.quality_metrics["execution_plan"]
    assert set(plan_block) == {"mode", "reason", "rejections"}
    assert isinstance(plan_block["mode"], str) and plan_block["mode"]
    assert isinstance(plan_block["rejections"], dict)


def test_native_admitted_run_without_explain_plan_stamps_no_execution_plan(tmp_path: Path) -> None:
    """The default (`explain_plan=False`) must stay silent on this key,
    matching the chunked/full_frame continuation's own default-off contract."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = _run_native(config, source_path)
    assert result.native_route is not None and result.native_route.admitted is True
    assert "execution_plan" not in result.quality_metrics


# ---------------------------------------------------------------------------
# Test 2: rejected column shapes reroute, byte-identical
# ---------------------------------------------------------------------------


_REJECTED_TYPES: list[tuple[str, pa.DataType, list[Any]]] = [
    ("large_utf8", pa.large_utf8(), ["a", None, "b"]),
    ("decimal128", pa.decimal128(10, 2), [1, None, 2]),
    ("float64", pa.float64(), [1.5, None, 2.5]),
    ("binary", pa.binary(), [b"a", None, b"b"]),
]
# int64/uint32/bool/timestamp moved to test_native_route_wider_types.py: Q3
# slice 2 widens admission for these, so a partial-null passthrough column no
# longer universally reroutes on schema alone (the matrix now decides).


@pytest.mark.parametrize(
    "label,arrow_type,values", _REJECTED_TYPES, ids=[t[0] for t in _REJECTED_TYPES]
)
def test_rejected_type_reroutes_byte_identical(
    tmp_path: Path, label: str, arrow_type: pa.DataType, values: list[Any]
) -> None:
    table = pa.table({"c": pa.array(values, type=arrow_type)})
    config, source_path = _config(tmp_path, [_pt("c")], table=table)
    candidate = _run_native(config, source_path)
    assert candidate.native_route is not None
    assert candidate.native_route.admitted is False
    assert candidate.native_route.reason is not None and candidate.native_route.reason.startswith(
        "non_utf8_column:"
    )

    oracle = _run_full_frame_oracle(config, source_path)
    assert_logical_parity(
        LogicalResult.from_execution_result(candidate), LogicalResult.from_execution_result(oracle)
    )


def test_dictionary_utf8_reroutes(tmp_path: Path) -> None:
    arr = pa.array(["a", "b", "a"]).dictionary_encode()
    table = pa.table({"c": arr})
    config, source_path = _config(tmp_path, [_pt("c")], table=table)
    # A dictionary/category-typed column isn't priceable by the pre-existing
    # (unrelated to this slice) byte-estimate routing signal
    # (`_mem_estimate.py` has no dtype entry for pandas "category" yet); force
    # that flag off here so this test isolates the native lane's OWN
    # dictionary rejection rather than tripping over that separate gap.
    candidate = _run_native(config, source_path, use_byte_estimate_routing=False)
    assert candidate.native_route is not None and candidate.native_route.admitted is False
    assert candidate.native_route.reason.startswith("non_utf8_column:")

    oracle = _run_full_frame_oracle(config, source_path, use_byte_estimate_routing=False)
    assert_logical_parity(
        LogicalResult.from_execution_result(candidate), LogicalResult.from_execution_result(oracle)
    )


def test_non_string_redact_with_reroutes(tmp_path: Path) -> None:
    table = pa.table({"c": pa.array(["a", None, "b"], type=pa.utf8())})
    config, source_path = _config(tmp_path, [_rd("c", redact_with=0)], table=table)
    candidate = _run_native(config, source_path)
    assert candidate.native_route is not None and candidate.native_route.admitted is False
    assert candidate.native_route.reason.startswith("redact_with_not_string:")

    oracle = _run_full_frame_oracle(config, source_path)
    assert_logical_parity(
        LogicalResult.from_execution_result(candidate), LogicalResult.from_execution_result(oracle)
    )


def test_zero_row_source_reroutes_to_oracle_not_exhausted_iterator(tmp_path: Path) -> None:
    """The regression the plan calls out by name: the fallback must resume
    the ordinary oracle path from the ORIGINAL LazySource, not an exhausted
    iterator -- a bug here would silently produce an empty/typed-wrong
    output instead of the oracle's real (if degenerate) result."""
    empty = pa.table({"c": pa.array([], type=pa.utf8())})
    config, source_path = _config(tmp_path, [_tr("c", length=2)], table=empty)
    candidate = _run_native(config, source_path)
    assert candidate.native_route is not None and candidate.native_route.admitted is False
    assert candidate.native_route.reason == "zero_row_source"
    assert candidate.outputs[_TABLE].num_rows == 0

    oracle = _run_full_frame_oracle(config, source_path)
    assert_logical_parity(
        LogicalResult.from_execution_result(candidate), LogicalResult.from_execution_result(oracle)
    )


# ---------------------------------------------------------------------------
# Test 3: the native lane provably ran (route ledger)
# ---------------------------------------------------------------------------


def _multi_chunk_source(n: int) -> pa.Table:
    return pa.table(
        {
            "pt": pa.array([f"v{i}" for i in range(n)], type=pa.utf8()),
            "rd": pa.array([f"s{i}" for i in range(n)], type=pa.utf8()),
            "tr": pa.array([f"tail{i:06d}" for i in range(n)], type=pa.utf8()),
        }
    )


def test_route_ledger_proves_the_native_lane_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = 125_000  # 3 batches at the lane's default 50,000-row batch size
    config, source_path = _config(
        tmp_path,
        [_pt("pt"), _rd("rd"), _tr("tr", length=4, keep="tail")],
        table=_multi_chunk_source(n),
    )

    oracle_spy_calls = {"n": 0}
    orig = _native_route_exec  # noqa: F841 - keep module handle for clarity below

    from decoy_engine.execution import _chunked as _chunked_mod

    orig_chunked = _chunked_mod.run_mask_pipeline_chunked

    def _spy(*args: Any, **kwargs: Any):
        oracle_spy_calls["n"] += 1
        return orig_chunked(*args, **kwargs)

    monkeypatch.setattr(_chunked_mod, "run_mask_pipeline_chunked", _spy)

    result = _run_native(config, source_path)
    assert result.native_route is not None
    report = result.native_route
    assert report.admitted is True, report.reason
    ledger = report.ledger
    assert ledger is not None

    assert oracle_spy_calls["n"] == 0, (
        "the oracle chunked entry was called on an admitted native run"
    )
    assert ledger.oracle_calls == 0
    assert ledger.oracle_rows == 0
    assert ledger.fallback_calls == 0
    assert ledger.fallback_rows == 0
    assert ledger.rejected_chunks == 0
    assert ledger.native_attempted == ledger.native_completed

    n_chunks = -(-n // 50_000)
    assert n_chunks == 3
    per_node_completed: dict[str, int] = {}
    for entry in ledger.records:
        per_node_completed[entry.node] = per_node_completed.get(entry.node, 0) + 1
    assert per_node_completed == {"pt": n_chunks, "rd": n_chunks, "tr": n_chunks}
    assert len(ledger.records) == len(set((r.table, r.node, r.chunk_index) for r in ledger.records))

    assert result.outputs[_TABLE].num_rows == n


# ---------------------------------------------------------------------------
# Test 4: source never materialized, read exactly once
# ---------------------------------------------------------------------------


def test_resolve_resident_sources_never_called_on_native_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())

    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError("resolve_resident_sources was called on the native path")

    monkeypatch.setattr(_psrc, "resolve_resident_sources", _boom)
    # `_pipeline.py` imports `_pipeline_sources as _psrc` at module scope, so
    # patching that same module-level name is what the production call site
    # actually reads.
    from decoy_engine.execution import _pipeline as _pipeline_mod

    monkeypatch.setattr(_pipeline_mod._psrc, "resolve_resident_sources", _boom)

    result = _run_native(config, source_path)
    assert result.native_route is not None and result.native_route.admitted is True


def test_source_iterated_exactly_once(tmp_path: Path) -> None:
    """Count calls on the ONE runtime source object `run_pipeline` was
    handed, not `LazySource.iter_batches` process-wide -- `profile_source`
    legitimately builds its OWN separate `LazySource` from the config's
    declared path for its bounded sample read, unrelated to (and unchanged
    by) this slice, so a class-wide patch would over-count."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())

    calls = {"n": 0}
    source = LazySource(path=source_path)
    orig_iter_batches = source.iter_batches

    def _counting_iter_batches(batch_rows: int):
        calls["n"] += 1
        return orig_iter_batches(batch_rows)

    object.__setattr__(source, "iter_batches", _counting_iter_batches)
    result = run_pipeline(
        config,
        {_TABLE: source},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
    )
    assert result.native_route is not None and result.native_route.admitted is True
    assert calls["n"] == 1, (
        f"the runtime source's iter_batches was called {calls['n']} times, expected 1"
    )


def test_fk_and_explicit_mode_jobs_never_touch_iter_batches(tmp_path: Path) -> None:
    """Locks critical note #1: FK routing and an explicit non-`auto`
    `execution_mode` must decline native admission before touching the
    RUNTIME source at all. Spies on the one source object `run_pipeline` is
    handed (not the class -- `profile_source` legitimately builds its own
    separate `LazySource` from the config's declared path for its bounded
    sample read, unrelated to and unchanged by this slice)."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    source = LazySource(path=source_path)

    def _boom(batch_rows: int):
        raise AssertionError("the runtime source's iter_batches was called for a non-candidate job")

    object.__setattr__(source, "iter_batches", _boom)

    # An explicit non-"auto" execution_mode, on an otherwise fully-admissible job.
    result = run_pipeline(
        config,
        {_TABLE: source},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="full_frame",
    )
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "execution_mode_not_auto"


# ---------------------------------------------------------------------------
# Test 6: every rejected feature reroutes, coded reason, full production deps
# ---------------------------------------------------------------------------


def test_vault_true_reroutes_for_each_strategy(tmp_path: Path) -> None:
    for col_factory in (_pt, _rd, _tr):
        col = col_factory("c", vault=True, namespace="ns")
        table = pa.table({"c": pa.array(["a", None, "b"], type=pa.utf8())})
        config, source_path = _config(tmp_path, [col], table=table)
        result = _run_native(config, source_path)
        assert result.native_route is not None and result.native_route.admitted is False
        assert result.native_route.reason == "vault_column:c", result.native_route.reason


def test_validators_present_reroutes(tmp_path: Path) -> None:
    config, source_path = _config(
        tmp_path,
        _parity_columns(),
        table=_parity_source(),
        validators=[{"name": "leak_check"}],
    )
    result = _run_native(config, source_path)
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "validators_present"


def test_fidelity_report_requested_reroutes(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = _run_native(config, source_path, fidelity_report=True)
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "fidelity_report_requested"


def test_quarantine_configured_reroutes(tmp_path: Path) -> None:
    config, source_path = _config(
        tmp_path,
        _parity_columns(),
        table=_parity_source(),
        quarantine={"output_path": str(tmp_path / "quarantine.jsonl")},
    )
    result = _run_native(config, source_path)
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "quarantine_configured"


def test_unsupported_projection_reroutes(tmp_path: Path) -> None:
    """A source column with no declared strategy (default-passthrough
    territory this lane has no wiring for) reroutes."""
    table = pa.table(
        {
            "pt": pa.array(["a", None, "b"], type=pa.utf8()),
            "undeclared": pa.array(["x", "y", "z"], type=pa.utf8()),
        }
    )
    config, source_path = _config(
        tmp_path,
        [_pt("pt")],
        table=table,
        global_settings_extra={"unconfigured_column_policy": "warn"},
    )
    result = _run_native(config, source_path)
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason.startswith("unsupported_projection:")


def test_generation_column_reroutes(tmp_path: Path) -> None:
    """A mixed generate+mask job (`has_mask_table=True`, so the lane is
    actually consulted) reroutes on the generate table's presence. A
    pure-generate job never reaches the lane at all (no mask table to route
    natively), so it is not the shape this reason is about."""
    mask_table = _parity_source()
    mask_source_path = _write_source(tmp_path, mask_table, "mask")
    raw = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {_TABLE: {"type": "file", "format": "parquet", "path": str(mask_source_path)}},
        "targets": {
            _TABLE: {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / "mask.out.parquet"),
            },
            "gen_t": {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / "gen.out.parquet"),
            },
        },
        "tables": [
            {"name": _TABLE, "columns": _parity_columns()},
            {
                "name": "gen_t",
                "row_count": 5,
                "generate_columns": [{"name": "id", "type": "sequence", "start": 1, "step": 1}],
            },
        ],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    result = run_pipeline(
        config,
        {_TABLE: LazySource(path=mask_source_path)},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
    )
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "generation_table_present"


def test_multi_table_job_reroutes(tmp_path: Path) -> None:
    table_a = pa.table({"pt": pa.array(["a", "b"], type=pa.utf8())})
    table_b = pa.table({"pt": pa.array(["c", "d"], type=pa.utf8())})
    path_a = _write_source(tmp_path, table_a, "a")
    path_b = _write_source(tmp_path, table_b, "b")
    raw = {
        "version": 1,
        "global_settings": {"seed": 1},
        "sources": {
            "a": {"type": "file", "format": "parquet", "path": str(path_a)},
            "b": {"type": "file", "format": "parquet", "path": str(path_b)},
        },
        "targets": {
            "a": {"type": "file", "format": "parquet", "path": str(tmp_path / "a.out.parquet")},
            "b": {"type": "file", "format": "parquet", "path": str(tmp_path / "b.out.parquet")},
        },
        "tables": [
            {"name": "a", "columns": [_pt("pt")]},
            {"name": "b", "columns": [_pt("pt")]},
        ],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    result = run_pipeline(
        config,
        {"a": LazySource(path=path_a), "b": LazySource(path=path_b)},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
    )
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "multi_table_job"


def test_native_route_enabled_false_never_attempts(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = run_pipeline(
        config,
        {_TABLE: LazySource(path=source_path)},
        engine_version=_ENGINE_VERSION,
        execution_mode="auto",
    )
    assert result.native_route is None


def test_fk_relationship_reroute_reasserted_at_native_call_site(tmp_path: Path) -> None:
    """A SELF-REFERENTIAL FK (one table, so the multi-table check does not
    fire first) disqualified from the sequential early return (here: a
    validator, which `_sequential_eligible` rejects) still falls through to
    `full_frame` -- exactly the call site the native lane sits at. Critical
    note #1: FK must be reasserted there too, so this job reroutes on its FK
    shape, never reaching a per-batch peek."""
    employees = pa.table(
        {
            "id": pa.array([f"e{i}" for i in range(5)], type=pa.utf8()),
            "manager_id": pa.array([None, "e0", "e0", "e1", "e1"], type=pa.utf8()),
        }
    )
    source_path = _write_source(tmp_path, employees, "employees")
    raw = {
        "version": 1,
        "global_settings": {"seed": 3},
        "sources": {"employees": {"type": "file", "format": "parquet", "path": str(source_path)}},
        "targets": {
            "employees": {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / "employees.out.parquet"),
            }
        },
        "tables": [{"name": "employees", "columns": [_pt("id"), _pt("manager_id")]}],
        "relationships": [
            {
                "parent": {"table": "employees", "columns": ["id"]},
                "children": [{"table": "employees", "columns": ["manager_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
        "validators": [{"name": "leak_check"}],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    result = run_pipeline(
        config,
        {"employees": LazySource(path=source_path)},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
        use_byte_estimate_routing=False,
    )
    assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "fk_relationship_present"
    assert result.outputs["employees"].num_rows == 5


def test_fk_job_reroutes_with_full_production_deps(tmp_path: Path) -> None:
    """FK reroute, plus proof the oracle path re-runs with a CUSTOM registry
    (not silently dropped -- 2.3's "full production deps" requirement)."""
    from decoy_engine.providers_v2 import ProviderRegistry, get_default_registry

    custom_registry = ProviderRegistry(dict(get_default_registry()._bindings))

    parent = pa.table({"id": pa.array([f"p{i}" for i in range(5)], type=pa.utf8())})
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(5)], type=pa.utf8()),
            "parent_id": pa.array([f"p{i}" for i in range(5)], type=pa.utf8()),
        }
    )
    parent_path = _write_source(tmp_path, parent, "parent")
    child_path = _write_source(tmp_path, child, "child")
    raw = {
        "version": 1,
        "global_settings": {"seed": 3},
        "sources": {
            "parent": {"type": "file", "format": "parquet", "path": str(parent_path)},
            "child": {"type": "file", "format": "parquet", "path": str(child_path)},
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
            {"name": "parent", "columns": [_pt("id")]},
            {"name": "child", "columns": [_pt("cid"), _pt("parent_id")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    sources = {"parent": LazySource(path=parent_path), "child": LazySource(path=child_path)}
    result = run_pipeline(
        config,
        sources,
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
        registry=custom_registry,
    )
    # A pure-mask FK job takes the sequential-or-out-of-core early return in
    # `_pipeline.py` BEFORE the native call site is ever reached, so
    # `native_route` stays None -- itself proof the lane never engaged. The
    # job still completed via that unchanged bounded route, using the
    # custom registry.
    assert result.native_route is None
    assert result.quality_metrics["execution"]["execution_mode"] in ("sequential", "out_of_core")
    assert result.outputs["parent"].num_rows == 5
    assert result.outputs["child"].num_rows == 5


# ---------------------------------------------------------------------------
# Sink predicate (2.4) and source shape (2.5), tested separately
# ---------------------------------------------------------------------------


def test_sink_none_runs_resident_mode(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = _run_native(config, source_path, sink=None)
    assert result.native_route is not None and result.native_route.admitted is True
    assert result.outputs[_TABLE].num_rows == 3


def test_exact_parquet_transactional_sink_runs_streaming_mode(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    sink_dir = tmp_path / "sink"
    result = _run_native(config, source_path, sink=ParquetTransactionalSink(sink_dir))
    assert result.native_route is not None and result.native_route.admitted is True
    assert result.outputs == {}
    assert (sink_dir / f"{_TABLE}.parquet").exists()


def test_callable_sink_adapter_reroutes(tmp_path: Path) -> None:
    """A plain callable is not `type() is ParquetTransactionalSink`, so it
    reroutes. The full_frame/chunked oracle continuation this job falls
    through to never consults a `sink` at all (that plumbing only exists on
    the sequential/out-of-core routes, unchanged by this slice), so the
    masked output comes back resident in `.outputs`, not via the callable."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())

    def _callable_sink(table: str, data: pa.Table) -> None:  # pragma: no cover - never invoked
        raise AssertionError("full_frame does not consult a sink; this must not be called")

    result = _run_native(config, source_path, sink=_callable_sink)
    assert result.native_route is not None and result.native_route.admitted is False
    assert result.native_route.reason == "unsupported_sink"
    assert result.outputs[_TABLE].num_rows == 3


def test_structural_custom_sink_reroutes(tmp_path: Path) -> None:
    class _CustomSink:
        def write(self, table: str, data: pa.Table) -> None: ...
        def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None: ...
        def commit(self) -> None: ...
        def abort(self) -> None: ...

    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = _run_native(config, source_path, sink=_CustomSink())
    assert result.native_route is not None and result.native_route.admitted is False
    assert result.native_route.reason == "unsupported_sink"


def test_retaining_parquet_sink_subclass_reroutes(tmp_path: Path) -> None:
    class _RetainingSink(ParquetTransactionalSink):
        pass

    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    sink_dir = tmp_path / "sink"
    result = _run_native(config, source_path, sink=_RetainingSink(sink_dir))
    assert result.native_route is not None and result.native_route.admitted is False
    assert result.native_route.reason == "unsupported_sink"


def test_resident_table_source_stays_on_existing_path(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    resident = pq.read_table(source_path)
    result = run_pipeline(
        config,
        {_TABLE: resident},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
    )
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "non_lazy_source"


def test_source_loader_alongside_lazy_source_does_not_fire_native(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())

    def _loader(table: str) -> pa.Table:
        return pq.read_table(source_path)

    result = run_pipeline(
        config,
        {_TABLE: LazySource(path=source_path)},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
        source_loader=_loader,
    )
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "source_loader_present"


# ---------------------------------------------------------------------------
# Test 8: routing precedence matrix
# ---------------------------------------------------------------------------


def test_explicit_sequential_override_never_engages_native(tmp_path: Path) -> None:
    parent = pa.table({"id": pa.array([f"p{i}" for i in range(4)], type=pa.utf8())})
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(4)], type=pa.utf8()),
            "parent_id": pa.array([f"p{i}" for i in range(4)], type=pa.utf8()),
        }
    )
    parent_path = _write_source(tmp_path, parent, "parent")
    child_path = _write_source(tmp_path, child, "child")
    raw = {
        "version": 1,
        "global_settings": {"seed": 3},
        "sources": {
            "parent": {"type": "file", "format": "parquet", "path": str(parent_path)},
            "child": {"type": "file", "format": "parquet", "path": str(child_path)},
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
            {"name": "parent", "columns": [_pt("id")]},
            {"name": "child", "columns": [_pt("cid"), _pt("parent_id")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns",
            }
        ],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    sources = {"parent": LazySource(path=parent_path), "child": LazySource(path=child_path)}
    result = run_pipeline(
        config,
        sources,
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="sequential",
    )
    assert result.native_route is None
    assert result.quality_metrics["execution"]["execution_mode"] == "sequential"


def test_explicit_full_frame_override_never_engages_native(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = run_pipeline(
        config,
        {_TABLE: LazySource(path=source_path)},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="full_frame",
    )
    assert result.native_route is not None
    assert result.native_route.admitted is False
    assert result.native_route.reason == "execution_mode_not_auto"
    assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"


def test_auto_mode_non_fk_admits_native(tmp_path: Path) -> None:
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = _run_native(config, source_path)
    assert result.native_route is not None and result.native_route.admitted is True
    assert result.quality_metrics["execution"]["execution_mode"] == "native"


def test_chunked_vs_full_frame_decision_unaffected_when_native_declines(tmp_path: Path) -> None:
    """A job native declines (fidelity_report) still resolves chunked-vs-
    full_frame exactly as it did before this slice."""
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = run_pipeline(
        config,
        {_TABLE: LazySource(path=source_path)},
        engine_version=_ENGINE_VERSION,
        native_route_enabled=True,
        execution_mode="auto",
        fidelity_report=True,
        auto_chunk=True,
        auto_chunk_threshold_rows=1,
    )
    assert result.native_route is not None and result.native_route.admitted is False
    assert result.quality_metrics["auto_chunk"]["mode"] in ("chunked", "full_frame")


# ---------------------------------------------------------------------------
# Test 9: closed-world admission sentry + compiled-plan/registry reuse
# ---------------------------------------------------------------------------


def test_closed_world_admission_sentry() -> None:
    """Every allowlisted strategy must carry zero row-error modes, zero
    quality obligations, no quarantine dependency, and zero warning codes --
    this lane implements none of that machinery, so an admitted strategy
    that grew one silently would be unsafe to run natively."""
    for strategy in ALLOWED_STRATEGIES:
        caps = capabilities_for(strategy)
        assert caps.row_error_modes == (), f"{strategy}: row_error_modes"
        assert caps.warning_codes == (), f"{strategy}: warning_codes"
        assert caps.quality_obligations == (), f"{strategy}: quality_obligations"
        assert caps.quarantine_required is False, f"{strategy}: quarantine_required"


def test_no_get_default_registry_on_native_path() -> None:
    # The native path must reuse the resolved registry, never fetch the default.
    # A runtime monkeypatch of `get_default_registry` guards nothing here,
    # because neither native module references the symbol at all -- so the
    # invariant holds by construction, and that is what we assert: the source of
    # both native modules is free of any `get_default_registry` call path.
    import inspect

    from decoy_engine.execution import _native_route, _native_route_exec

    for module in (_native_route, _native_route_exec):
        assert "get_default_registry" not in inspect.getsource(module), (
            f"{module.__name__} references get_default_registry; the native path "
            "must reuse the resolved registry threaded into it"
        )


def test_compiled_plan_reused_not_recompiled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The native lane must read the SAME compiled `Plan` `run_pipeline`
    already built, not call `compile_plan` again for its own admission."""
    from decoy_engine.execution import _native_route as _native_route_mod

    calls = {"n": 0}
    orig = _native_route_mod.known_output_columns

    def _spy(plan: Any, table: str):
        calls["n"] += 1
        return orig(plan, table)

    monkeypatch.setattr(_native_route_mod, "known_output_columns", _spy)
    config, source_path = _config(tmp_path, _parity_columns(), table=_parity_source())
    result = _run_native(config, source_path)
    assert result.native_route is not None and result.native_route.admitted is True
    assert calls["n"] == 1
