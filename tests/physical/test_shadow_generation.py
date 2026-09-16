"""Task 4.6 slice 5a: the coordinator OWNS a pure-generate job's dispatch
(`ShadowCoordinator._dispatch_synthesis`), proven by a PHASE-BOUND
DIFFERENTIAL PARITY proof against the public `run_pipeline` oracle (plan
section 0) -- never a leaf-knob re-validation. Four groups (plan section 4):

(a) coordinator wiring -- the adapter is called exactly once, with the
    forwarded (pinned-None) args, and the coordinator adapts its result
    faithfully.
(b) the positive differential matrix -- every admitted shape (sequence +
    categorical, weighted/unweighted, multi-column/table/seed) is Arrow-
    IPC-byte-equal to the oracle, and deterministic across repeated runs.
(c) the malformed differential matrix -- a leaf-knob value the gate never
    inspects reaches `generate_tables` unfiltered on both sides and becomes
    a faithfully IDENTICAL rejection (same fingerprint, both phase-entry
    spies fired), plus a bounded Hypothesis sweep over garbage knob values.
(d) decline/inertness -- every out-of-domain shape declines with
    `GENERATION_SHAPE_UNSUPPORTED` before the adapter is ever constructed.

The existing seam/disconnection sentries (`test_shadow_disconnection.py`,
`test_disconnection.py`) already prove production `run_pipeline` never calls
`SynthesisStageAdapter`; nothing here touches or weakens that.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.config import PipelineConfig
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._plan import PhysicalPlan, SynthesisStage
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_diff_codes import (
    GENERATION_SHAPE_UNSUPPORTED,
    MIXED_DRIVER_UNSUPPORTED,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._types import DriverId, ExecutionScope
from decoy_engine.execution.physical.drivers._synthesis import SynthesisStageAdapter
from decoy_engine.plan import compile_plan
from decoy_engine.plan._types import Plan
from decoy_engine.profile import Profile
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_generation_failures_match,
    assert_generation_outputs_arrow_ipc_equal,
    run_generation_shadow_and_oracle,
    run_shadow_and_oracle,
)

_DEFAULT_COLUMNS: list[dict[str, Any]] = [
    {"name": "id", "type": "sequence", "start": 1, "step": 1},
    {"name": "tier", "type": "categorical", "categories": ["A", "B", "C"]},
]


def _generate_config(
    *,
    row_count: int = 5,
    seed: int = 20260916,
    columns: list[dict[str, Any]] | None = None,
    table_name: str = "people",
) -> dict[str, Any]:
    """A minimal admitted pure-generate config: one generate table, sequence
    + categorical columns only, no sources/relationships/namespaces/subset/
    validators/quarantine/run_storm/mask_secret_ref."""
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {},
        "targets": {table_name: {"type": "file", "format": "csv", "path": f"{table_name}.out.csv"}},
        "tables": [
            {
                "name": table_name,
                "row_count": row_count,
                "generate_columns": columns if columns is not None else _DEFAULT_COLUMNS,
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _empty_profile() -> Profile:
    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="test",
    )


def _compiled_plan(config: dict[str, Any]) -> Plan:
    return compile_plan(config, _empty_profile(), decoy_engine_version="test")


def _generate_table_names(config: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for table in config.get("tables", []):
        if isinstance(table, dict) and table.get("generate_columns"):
            name = table.get("name")
            assert isinstance(name, str)
            names.append(name)
    return tuple(sorted(names))


def _physical_synthesis_for(plan: Plan) -> SynthesisStage:
    assert plan.generation is not None
    digest = hashlib.sha256(plan.generation.config_json.encode("utf-8")).hexdigest()
    decoded = json.loads(plan.generation.config_json)
    return SynthesisStage(tables=_generate_table_names(decoded), config_digest=digest)


def _admitted_dispatch(
    config: dict[str, Any],
) -> tuple[PhysicalPlan, ShadowContext, Any]:
    """A real compiled, admitted pure-generate dispatch triple: a `PhysicalPlan`
    with an empty `tables` and a matching `synthesis`, a `ShadowContext`
    pinned to every admitted (None/False) runtime setting, and an empty
    snapshot."""
    plan = _compiled_plan(config)
    physical_synthesis = _physical_synthesis_for(plan)
    physical_plan = PhysicalPlan(
        engine_version=ENGINE_VERSION,
        plan_hash="h",
        synthesis=physical_synthesis,
        tables=(),
    )
    ctx = ShadowContext.from_key_provider(plan=plan, key_provider=None)
    snapshot = capture_shadow_snapshot({})
    return physical_plan, ctx, snapshot


def _spy_adapter_run(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Wraps `SynthesisStageAdapter.run` to record every call
    `(plan, derive_key, instance_default_locale)` while still delegating to
    the real implementation -- callers assert on the recorded calls."""
    calls: list[tuple[Any, ...]] = []
    original = SynthesisStageAdapter.run

    def _spy(
        self: SynthesisStageAdapter,
        plan: Any,
        derive_key: Any = None,
        instance_default_locale: str | None = None,
    ) -> dict[str, pa.Table]:
        calls.append((plan, derive_key, instance_default_locale))
        return original(self, plan, derive_key, instance_default_locale)

    monkeypatch.setattr(SynthesisStageAdapter, "run", _spy)
    return calls


def _spy_adapter_never_called(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Wraps `SynthesisStageAdapter.run` to record calls WITHOUT delegating
    -- used by decline tests, which must never reach the real generator."""
    calls: list[tuple[Any, ...]] = []

    def _bomb(self: SynthesisStageAdapter, *args: Any, **kwargs: Any) -> dict[str, pa.Table]:
        calls.append((args, kwargs))
        raise AssertionError("SynthesisStageAdapter.run must not be invoked for a declined plan")

    monkeypatch.setattr(SynthesisStageAdapter, "run", _bomb)
    return calls


# ---------------------------------------------------------------------------
# (a) Coordinator wiring
# ---------------------------------------------------------------------------


def test_dispatch_calls_adapter_exactly_once_with_forwarded_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _generate_config()
    plan, ctx, snapshot = _admitted_dispatch(config)
    calls = _spy_adapter_run(monkeypatch)

    ShadowCoordinator(ctx=ctx).run(plan, snapshot)

    assert len(calls) == 1
    called_plan, derive_key, instance_default_locale = calls[0]
    assert called_plan is ctx.plan
    assert derive_key is ctx.derive_key is None
    assert instance_default_locale is ctx.instance_default_locale is None


def test_dispatch_returns_adapter_tables_by_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config()
    plan, ctx, snapshot = _admitted_dispatch(config)

    captured: dict[str, dict[str, pa.Table]] = {}
    original = SynthesisStageAdapter.run

    def _capture(self: SynthesisStageAdapter, *args: Any, **kwargs: Any) -> dict[str, pa.Table]:
        result = original(self, *args, **kwargs)
        captured["outputs"] = result
        return result

    monkeypatch.setattr(SynthesisStageAdapter, "run", _capture)

    result = ShadowCoordinator(ctx=ctx).run(plan, snapshot)

    assert set(result.outputs) == {"people"}
    assert result.outputs["people"] is captured["outputs"]["people"]


def test_dispatch_records_synthesis_seam_context(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config()
    plan, ctx, snapshot = _admitted_dispatch(config)

    result = ShadowCoordinator(ctx=ctx).run(plan, snapshot)

    assert isinstance(result.driver_invocation, SeamContext)
    assert result.driver_invocation.driver_id == DriverId.SYNTHESIS
    assert result.driver_invocation.scope == ExecutionScope.SYNTHESIS_STAGE
    assert result.route_evidence == {}
    assert result.warnings == ()
    assert result.row_errors == ()
    assert result.quality_metrics == {}


def test_dispatch_rejects_table_key_set_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the adapter ever returned a table set that does not exactly equal
    `physical_synthesis.tables`, the coordinator must catch it rather than
    silently accepting a partial or extra result."""
    config = _generate_config()
    plan, ctx, snapshot = _admitted_dispatch(config)

    def _wrong_tables(
        self: SynthesisStageAdapter, *args: Any, **kwargs: Any
    ) -> dict[str, pa.Table]:
        self.last_invocation = SeamContext(
            driver_id=DriverId.SYNTHESIS, scope=ExecutionScope.SYNTHESIS_STAGE, tables=()
        )
        return {"not_people": pa.table({"x": [1]})}

    monkeypatch.setattr(SynthesisStageAdapter, "run", _wrong_tables)

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx).run(plan, snapshot)
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED


# ---------------------------------------------------------------------------
# (b) Positive differential matrix -- Arrow-IPC-byte-equal vs the oracle.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("row_count", [0, 1, 7])
def test_positive_row_counts(row_count: int) -> None:
    config = _generate_config(row_count=row_count)
    run = run_shadow_and_oracle(config, sources={})
    assert_generation_outputs_arrow_ipc_equal(run)


def test_positive_sequence_formatting_and_nulls() -> None:
    config = _generate_config(
        columns=[
            {
                "name": "code",
                "type": "sequence",
                "start": 100,
                "step": 3,
                "prefix": "C-",
                "suffix": "!",
                "pad_length": 6,
                "null_probability": 0.4,
            }
        ],
        row_count=25,
    )
    run = run_shadow_and_oracle(config, sources={})
    assert_generation_outputs_arrow_ipc_equal(run)


@pytest.mark.parametrize(
    "categorical_column",
    [
        {"name": "tier", "type": "categorical", "categories": ["A", "B", "C"]},
        {
            "name": "tier",
            "type": "categorical",
            "categories": ["A", "B", "C"],
            "weights": [5, 3, 1],
        },
        {
            "name": "tier",
            "type": "categorical",
            "categories": ["A", "B"],
            "weights": [1, 1],
            "null_probability": 0.25,
        },
    ],
)
def test_positive_categorical_weighted_and_unweighted(categorical_column: dict[str, Any]) -> None:
    config = _generate_config(columns=[categorical_column], row_count=40)
    run = run_shadow_and_oracle(config, sources={})
    assert_generation_outputs_arrow_ipc_equal(run)


def test_positive_multiple_columns() -> None:
    config = _generate_config(
        columns=[
            {"name": "id", "type": "sequence", "start": 1, "step": 1},
            {"name": "code", "type": "sequence", "start": 900, "step": -1, "pad_length": 4},
            {
                "name": "tier",
                "type": "categorical",
                "categories": ["A", "B", "C"],
                "weights": [2, 1, 1],
            },
            {"name": "flag", "type": "categorical", "categories": ["Y", "N"]},
        ],
        row_count=12,
    )
    run = run_shadow_and_oracle(config, sources={})
    assert_generation_outputs_arrow_ipc_equal(run)


def test_positive_multiple_generate_tables() -> None:
    raw = {
        "version": 1,
        "global_settings": {"seed": 20260916},
        "sources": {},
        "targets": {
            "people": {"type": "file", "format": "csv", "path": "people.out.csv"},
            "orders": {"type": "file", "format": "csv", "path": "orders.out.csv"},
        },
        "tables": [
            {
                "name": "people",
                "row_count": 6,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 1, "step": 1},
                    {"name": "tier", "type": "categorical", "categories": ["A", "B"]},
                ],
            },
            {
                "name": "orders",
                "row_count": 9,
                "generate_columns": [
                    {"name": "order_id", "type": "sequence", "start": 500, "step": 1},
                    {
                        "name": "status",
                        "type": "categorical",
                        "categories": ["open", "closed"],
                        "weights": [1, 3],
                    },
                ],
            },
        ],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    run = run_shadow_and_oracle(config, sources={})
    assert_generation_outputs_arrow_ipc_equal(run)


@pytest.mark.parametrize("seed", [1, 2026, 999999])
def test_positive_multiple_seeds(seed: int) -> None:
    config = _generate_config(seed=seed, row_count=15)
    run = run_shadow_and_oracle(config, sources={})
    assert_generation_outputs_arrow_ipc_equal(run)


def test_positive_cross_run_determinism() -> None:
    """The SAME config run twice through the shadow side alone must produce
    byte-identical output -- generation determinism is a property of the
    config/seed, independent of the oracle comparison."""
    config = _generate_config(row_count=30)
    run_a = run_shadow_and_oracle(config, sources={})
    run_b = run_shadow_and_oracle(config, sources={})
    assert run_a.shadow.outputs["people"].equals(run_b.shadow.outputs["people"])


# ---------------------------------------------------------------------------
# (c) Malformed differential matrix: identical rejection (fingerprint +
# phase spies), plus a bounded Hypothesis sweep -- never enumerating valid
# shapes, only garbage leaf-knob values the gate never inspects.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "columns",
    [
        [{"name": "id", "type": "sequence", "start": "abc"}],
        [{"name": "id", "type": "sequence", "start": [1, 2]}],
        [{"name": "id", "type": "sequence", "start": float("inf")}],
        [{"name": "id", "type": "sequence", "start": 1, "prefix": "\ud800"}],
        [{"name": "id", "type": "sequence", "start": 1, "null_probability": "abc"}],
        [{"name": "tier", "type": "categorical", "categories": ["A", "B"], "weights": [1]}],
        [{"name": "tier", "type": "categorical", "categories": [1, "a", {"x": 1}]}],
    ],
    ids=[
        "sequence-start-non-numeric-string",
        "sequence-start-list",
        "sequence-start-infinite",
        "sequence-prefix-surrogate",
        "sequence-null-probability-non-numeric",
        "categorical-weights-length-mismatch",
        "categorical-mixed-category-types",
    ],
)
def test_malformed_leaf_knob_is_identical_rejection(columns: list[dict[str, Any]]) -> None:
    config = _generate_config(columns=columns)
    diff = run_generation_shadow_and_oracle(config)
    assert_generation_failures_match(diff)


_GARBAGE_LEAF_VALUES = st.one_of(
    st.text(min_size=0, max_size=6),
    st.floats(allow_nan=True, allow_infinity=True, width=32),
    st.integers(min_value=-1000, max_value=1000),
    st.lists(st.integers(min_value=-5, max_value=5), max_size=3),
    st.none(),
)
# `start` alone excludes `None`: `GenerateColumnConfig._type_params_present`
# requires a sequence column's `start` be present (schema-level, not this
# gate's concern) -- `None` there fails config VALIDATION, never reaches
# generation, so it is not a knob this differential sweep can exercise.
_START_GARBAGE_VALUES = st.one_of(
    st.text(min_size=0, max_size=6),
    st.floats(allow_nan=True, allow_infinity=True, width=32),
    st.integers(min_value=-1000, max_value=1000),
    st.lists(st.integers(min_value=-5, max_value=5), max_size=3),
)


def _assert_success_or_matching_failure(config: dict[str, Any]) -> None:
    diff = run_generation_shadow_and_oracle(config)
    if diff.shadow_exception is None and diff.oracle_exception is None:
        assert diff.shadow_tables is not None
        assert diff.oracle_tables is not None
        for name, oracle_table in diff.oracle_tables.items():
            assert diff.shadow_tables[name].equals(oracle_table)
        return
    assert_generation_failures_match(diff)


@given(start=_START_GARBAGE_VALUES, step=_GARBAGE_LEAF_VALUES, pad_length=_GARBAGE_LEAF_VALUES)
@settings(max_examples=40, deadline=None)
def test_bounded_sequence_knob_sweep_is_success_or_identical_rejection(
    start: Any, step: Any, pad_length: Any
) -> None:
    config = _generate_config(
        columns=[
            {
                "name": "id",
                "type": "sequence",
                "start": start,
                "step": step,
                "pad_length": pad_length,
            }
        ]
    )
    _assert_success_or_matching_failure(config)


@given(weights=st.lists(_GARBAGE_LEAF_VALUES, max_size=4), null_probability=_GARBAGE_LEAF_VALUES)
@settings(max_examples=40, deadline=None)
def test_bounded_categorical_knob_sweep_is_success_or_identical_rejection(
    weights: list[Any], null_probability: Any
) -> None:
    config = _generate_config(
        columns=[
            {
                "name": "tier",
                "type": "categorical",
                "categories": ["A", "B", "C"],
                "weights": weights,
                "null_probability": null_probability,
            }
        ]
    )
    _assert_success_or_matching_failure(config)


# ---------------------------------------------------------------------------
# (d) Decline / inertness: every out-of-domain shape declines with
# GENERATION_SHAPE_UNSUPPORTED before the adapter is ever constructed.
# ---------------------------------------------------------------------------


def _decode_config_json(plan: Plan) -> dict[str, Any]:
    assert plan.generation is not None
    decoded = json.loads(plan.generation.config_json)
    assert isinstance(decoded, dict)
    return decoded


def _dispatch_from_mutated_config(
    base_plan: Plan, mutated_config: dict[str, Any]
) -> tuple[PhysicalPlan, ShadowContext, Any]:
    """Rebuild the dispatch triple from a HAND-MUTATED decoded config, re-
    serialized the same way `plan._generation.build_generation_plan` does
    (`json.dumps(config, sort_keys=True, default=str)`), keeping every other
    `Plan` field (seed envelope, relationships, ordering, ...) from the real
    compiled `base_plan` -- only `generation.config_json` changes. This
    exercises the admission gate's SHALLOW check directly against a shape a
    real end-to-end job could never validly reach (e.g. `subset` requires an
    actual mask table the pure-generate shape structurally cannot have), per
    the gate's own "TOTAL-guarded" defensiveness contract.
    """
    assert base_plan.generation is not None
    mutated_json = json.dumps(mutated_config, sort_keys=True, default=str)
    mutated_generation = dataclasses.replace(base_plan.generation, config_json=mutated_json)
    mutated_plan = dataclasses.replace(base_plan, generation=mutated_generation)
    digest = hashlib.sha256(mutated_json.encode("utf-8")).hexdigest()
    physical_synthesis = SynthesisStage(
        tables=_generate_table_names(mutated_config), config_digest=digest
    )
    physical_plan = PhysicalPlan(
        engine_version=ENGINE_VERSION, plan_hash="h", synthesis=physical_synthesis, tables=()
    )
    ctx = ShadowContext.from_key_provider(plan=mutated_plan, key_provider=None)
    return physical_plan, ctx, capture_shadow_snapshot({})


def _assert_declines(
    physical_plan: PhysicalPlan, ctx: ShadowContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_adapter_never_called(monkeypatch)
    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx).run(physical_plan, capture_shadow_snapshot({}))
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED
    assert calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sources", {"people": {"type": "file", "format": "parquet", "path": "x.parquet"}}),
        (
            "relationships",
            [
                {
                    "parent": {"table": "people", "columns": ["id"]},
                    "children": [{"table": "other", "columns": ["pid"]}],
                    "orphan_policy": "preserve",
                }
            ],
        ),
        ("namespaces", {"ns1": {"declared_by": ["people.id"]}}),
        (
            "subset",
            {"seeds": [{"table": "people", "mode": "sample", "key_columns": ["id"], "count": 1}]},
        ),
        ("validators", [{"name": "fk_intact"}]),
        ("quarantine", {"output_path": "q.parquet"}),
        ("run_storm", True),
    ],
    ids=[
        "non-empty-sources",
        "non-empty-relationships",
        "non-empty-namespaces",
        "non-none-subset",
        "non-empty-validators",
        "non-none-quarantine",
        "run-storm-true",
    ],
)
def test_decline_job_scope_field(field: str, value: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config()
    base_plan = _compiled_plan(config)
    decoded = _decode_config_json(base_plan)
    decoded[field] = value
    physical_plan, ctx, _ = _dispatch_from_mutated_config(base_plan, decoded)
    _assert_declines(physical_plan, ctx, monkeypatch)


def test_decline_mask_secret_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config()
    base_plan = _compiled_plan(config)
    decoded = _decode_config_json(base_plan)
    decoded["global_settings"]["mask_secret_ref"] = "env:SOME_SECRET"
    physical_plan, ctx, _ = _dispatch_from_mutated_config(base_plan, decoded)
    _assert_declines(physical_plan, ctx, monkeypatch)


def test_decline_generate_table_transforms(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config()
    base_plan = _compiled_plan(config)
    decoded = _decode_config_json(base_plan)
    decoded["tables"][0]["transforms"] = [{"op": "drop_column", "columns": ["tier"]}]
    physical_plan, ctx, _ = _dispatch_from_mutated_config(base_plan, decoded)
    _assert_declines(physical_plan, ctx, monkeypatch)


def test_decline_determinism_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config(
        columns=[{"name": "id", "type": "sequence", "start": 1, "determinism": "fresh"}]
    )
    physical_plan, ctx, _ = _admitted_dispatch(config)
    _assert_declines(physical_plan, ctx, monkeypatch)


@pytest.mark.parametrize(
    "column",
    [
        {"name": "f", "type": "faker", "faker_type": "email"},
        {"name": "s", "type": "statistical", "snapshot_file": "does-not-matter.json"},
        {"name": "d", "type": "derived", "expression": "1"},
        {"name": "fo", "type": "formula", "formula": "1"},
        {"name": "gs", "type": "grouped_series", "group_by": "id", "order_by": "id"},
        {"name": "wd", "type": "windowed_date", "anchor": "2020-01-01", "max_days": 5},
        {"name": "gk", "type": "group_key", "group_by": "id"},
    ],
    ids=[
        "faker",
        "statistical",
        "derived",
        "formula",
        "grouped_series",
        "windowed_date",
        "group_key",
    ],
)
def test_decline_unsupported_generate_column_type(
    column: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The extra column is appended AFTER compile (never through
    # PipelineConfig/compile_plan): a real `statistical`/`windowed_date`
    # column has its own compile-time prerequisites (a readable snapshot, a
    # same-table anchor column) unrelated to what this gate is proving --
    # that the SHAPE alone (an unadmitted `type` token) declines, regardless
    # of whether the column would otherwise compile.
    base_plan = _compiled_plan(_generate_config())
    decoded = _decode_config_json(base_plan)
    decoded["tables"][0]["generate_columns"].append(column)
    physical_plan, ctx, _ = _dispatch_from_mutated_config(base_plan, decoded)
    _assert_declines(physical_plan, ctx, monkeypatch)


def test_decline_ctx_plan_none(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config()
    plan = _compiled_plan(config)
    physical_synthesis = _physical_synthesis_for(plan)
    physical_plan = PhysicalPlan(
        engine_version=ENGINE_VERSION, plan_hash="h", synthesis=physical_synthesis, tables=()
    )
    ctx = ShadowContext(mask_key=b"\x00" * 32)  # ctx.plan defaults to None
    _assert_declines(physical_plan, ctx, monkeypatch)


def test_decline_digest_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config()
    base_plan = _compiled_plan(config)
    physical_synthesis = _physical_synthesis_for(base_plan)
    tampered_synthesis = dataclasses.replace(physical_synthesis, config_digest="0" * 64)
    physical_plan = PhysicalPlan(
        engine_version=ENGINE_VERSION, plan_hash="h", synthesis=tampered_synthesis, tables=()
    )
    ctx = ShadowContext.from_key_provider(plan=base_plan, key_provider=None)
    _assert_declines(physical_plan, ctx, monkeypatch)


def test_decline_table_name_set_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config()
    base_plan = _compiled_plan(config)
    physical_synthesis = _physical_synthesis_for(base_plan)
    tampered_synthesis = dataclasses.replace(physical_synthesis, tables=("not_people",))
    physical_plan = PhysicalPlan(
        engine_version=ENGINE_VERSION, plan_hash="h", synthesis=tampered_synthesis, tables=()
    )
    ctx = ShadowContext.from_key_provider(plan=base_plan, key_provider=None)
    _assert_declines(physical_plan, ctx, monkeypatch)


def test_decline_mixed_synthesis_and_mask_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    """A synthesis stage paired with ANY mask tables is 5b territory (the
    coordinator never stitches generate+mask output in this slice)."""
    from decoy_engine.execution.physical._plan import PhysicalTable

    config = _generate_config()
    plan, ctx, snapshot = _admitted_dispatch(config)
    mask_table = PhysicalTable(
        table="masked",
        driver=DriverId.FULL_FRAME,
        driver_reason="test",
        driver_reason_detail=None,
        rejected_alternatives=(),
        relationship_role="independent",
        substrate="pandas",
        nodes=(),
    )
    mixed_plan = dataclasses.replace(plan, tables=(mask_table,))
    calls = _spy_adapter_never_called(monkeypatch)
    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx).run(mixed_plan, snapshot)
    assert excinfo.value.code == MIXED_DRIVER_UNSUPPORTED
    assert calls == []


@pytest.mark.parametrize(
    "ctx_kwargs",
    [
        {"derive_key": lambda label: b"x" * 32},
        {"instance_default_locale": "en_US"},
        {"sink_requested": True},
        {"source_loader_requested": True},
        {"vault_writer_requested": True},
        {"fidelity_report": True},
    ],
    ids=[
        "derive_key",
        "instance_default_locale",
        "sink_requested",
        "source_loader_requested",
        "vault_writer_requested",
        "fidelity_report",
    ],
)
def test_decline_non_admitted_runtime_carrier(
    ctx_kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _generate_config()
    plan, admitted_ctx, snapshot = _admitted_dispatch(config)
    ctx = dataclasses.replace(admitted_ctx, **ctx_kwargs)
    _assert_declines(plan, ctx, monkeypatch)


def test_decline_non_admitted_key_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from decoy_engine.keyprovider import SecretKeyProvider

    config = _generate_config()
    plan = _compiled_plan(config)
    physical_synthesis = _physical_synthesis_for(plan)
    physical_plan = PhysicalPlan(
        engine_version=ENGINE_VERSION, plan_hash="h", synthesis=physical_synthesis, tables=()
    )
    provider = SecretKeyProvider(secret=b"\x02" * 32, key_version="v1")
    ctx = ShadowContext.from_key_provider(plan=plan, key_provider=provider)
    _assert_declines(physical_plan, ctx, monkeypatch)
