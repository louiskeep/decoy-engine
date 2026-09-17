"""5a-faker: admits `type: faker` generate columns to the shadow-parity
coordinator, restricted to GP2's reviewed deterministic allowlist
(`generation._faker_pool.POOL_ELIGIBLE_FAKER_TYPES`, re-exported here as
`SHADOW_ADMISSIBLE_FAKER_TYPES`). Mirrors `test_shadow_generation.py`'s
structure, scoped to the faker-specific admission surface it does not cover:

(a) byte-parity for every allowlisted type, per-row and pooled (>=50k rows,
    where GP2 pooling engages on both sides identically).
(b) decline: a non-allowlisted `faker_type`, a custom-overridden allowlisted
    type, `determinism: fresh`, and a malformed `faker_type` (adapter never
    constructed).
(c) the registry-snapshot race: a custom provider registered between the
    snapshot capture and the shadow run, then unregistered before the oracle
    run, must not desync either side from the captured (built-in) snapshot.
(d) the default-`None` contract: `generate_tables(plan)` ==
    `generate_tables(plan, provider_snapshot=None)`, and `None` still
    observes live registrations (not an accidentally-empty snapshot).
(e) mixed jobs: an allowlisted faker column alongside a sequence column is
    admitted; a non-allowlisted faker column declines the whole table.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import PhysicalPlan, SynthesisStage
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
from decoy_engine.execution.physical._shadow_diff_codes import (
    GENERATION_SHAPE_UNSUPPORTED,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_generation import SHADOW_ADMISSIBLE_FAKER_TYPES
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.execution.physical.drivers._synthesis import SynthesisStageAdapter
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.internal.faker_setup import snapshot_custom_faker_providers
from decoy_engine.plan import compile_plan
from decoy_engine.plan._types import Plan
from decoy_engine.profile import Profile
from decoy_engine.providers import register_faker_provider, unregister_faker_provider
from tests.physical._shadow_helpers import (
    ENGINE_VERSION,
    assert_generation_outputs_arrow_ipc_equal,
    assert_generation_tables_arrow_ipc_equal,
    run_shadow_and_oracle,
)

# GP2 pooling engages at N_THRESHOLD (50_000); PER_ROW stays well under it so
# both sides exercise `_faker`'s unpooled per-row loop.
PER_ROW_ROW_COUNT = 5
POOLED_ROW_COUNT = 60_000

_ALLOWLISTED_TYPES = sorted(SHADOW_ADMISSIBLE_FAKER_TYPES)


def _generate_config(
    *,
    row_count: int = PER_ROW_ROW_COUNT,
    seed: int = 20260917,
    columns: list[dict[str, Any]] | None = None,
    table_name: str = "people",
) -> dict[str, Any]:
    """A minimal pure-generate config carrying one `type: faker` column by
    default. Mirrors `test_shadow_generation.py`'s `_generate_config`."""
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {},
        "targets": {table_name: {"type": "file", "format": "csv", "path": f"{table_name}.out.csv"}},
        "tables": [
            {
                "name": table_name,
                "row_count": row_count,
                "generate_columns": columns
                if columns is not None
                else [{"name": "f", "type": "faker", "faker_type": "city"}],
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


def _dispatch_triple(config: dict[str, Any]) -> tuple[PhysicalPlan, ShadowContext, Any]:
    """A compiled pure-generate dispatch triple, admitted or not -- callers
    decide which via `config`'s contents. Mirrors `test_shadow_generation.
    py`'s `_admitted_dispatch` (same shape, neutral name since several
    callers here build a triple they expect to DECLINE)."""
    plan = _compiled_plan(config)
    physical_synthesis = _physical_synthesis_for(plan)
    physical_plan = PhysicalPlan(
        engine_version=ENGINE_VERSION, plan_hash="h", synthesis=physical_synthesis, tables=()
    )
    ctx = ShadowContext.from_key_provider(plan=plan, key_provider=None)
    snapshot = capture_shadow_snapshot({})
    return physical_plan, ctx, snapshot


def _decode_config_json(plan: Plan) -> dict[str, Any]:
    assert plan.generation is not None
    decoded = json.loads(plan.generation.config_json)
    assert isinstance(decoded, dict)
    return decoded


def _dispatch_from_mutated_config(
    base_plan: Plan, mutated_config: dict[str, Any]
) -> tuple[PhysicalPlan, ShadowContext, Any]:
    """Rebuild the dispatch triple from a hand-mutated decoded config,
    bypassing `PipelineConfig` validation -- the only way to reach the
    admission gate with a `faker_type` shape (`None`/a list/a mapping) the
    schema itself would reject. Mirrors `test_shadow_generation.py`'s
    helper of the same name."""
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


def _spy_adapter_never_called(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    calls: list[Any] = []

    def _bomb(self: SynthesisStageAdapter, *args: Any, **kwargs: Any) -> dict[str, pa.Table]:
        calls.append((args, kwargs))
        raise AssertionError("SynthesisStageAdapter.run must not be invoked for a declined plan")

    monkeypatch.setattr(SynthesisStageAdapter, "run", _bomb)
    return calls


def _assert_declines(
    physical_plan: PhysicalPlan, ctx: ShadowContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_adapter_never_called(monkeypatch)
    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx).run(physical_plan, capture_shadow_snapshot({}))
    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED
    assert calls == []


def _write_source(tmp_path: Path, table: pa.Table, name: str) -> Path:
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    return path


def _mixed_config(
    tmp_path: Path,
    *,
    mask_source: pa.Table,
    mask_columns: list[dict[str, Any]],
    generate_columns: list[dict[str, Any]],
    mask_table: str = "accounts",
    generate_table: str = "people",
    row_count: int = 6,
    seed: int = 20260917,
) -> dict[str, Any]:
    """One generate table + one mask table, no crossing FK edge. Mirrors
    `test_shadow_mixed.py`'s `_mixed_config`, narrowed to the single-mask-
    table shape these tests need."""
    path = _write_source(tmp_path, mask_source, mask_table)
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {mask_table: {"type": "file", "format": "parquet", "path": str(path)}},
        "targets": {
            generate_table: {
                "type": "file",
                "format": "csv",
                "path": f"{generate_table}.out.csv",
            },
            mask_table: {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / f"{mask_table}.out.parquet"),
            },
        },
        "tables": [
            {"name": generate_table, "row_count": row_count, "generate_columns": generate_columns},
            {"name": mask_table, "columns": mask_columns},
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump()


def _accounts_source(n: int = 6) -> pa.Table:
    return pa.table({"acct_id": pa.array([f"a{i}" for i in range(n)], type=pa.string())})


# ---------------------------------------------------------------------------
# (a) Byte-parity: every allowlisted type, per-row and pooled.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row_count", [PER_ROW_ROW_COUNT, POOLED_ROW_COUNT], ids=["per_row", "pooled"]
)
@pytest.mark.parametrize("faker_type", _ALLOWLISTED_TYPES)
def test_positive_allowlisted_faker_types_byte_parity(faker_type: str, row_count: int) -> None:
    config = _generate_config(
        columns=[{"name": "f", "type": "faker", "faker_type": faker_type}],
        row_count=row_count,
    )
    run = run_shadow_and_oracle(config, sources={})
    assert_generation_outputs_arrow_ipc_equal(run)


# ---------------------------------------------------------------------------
# (b) Decline.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("faker_type", ["email", "pyint", "uuid4"])
def test_decline_non_allowlisted_faker_type(
    faker_type: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _generate_config(columns=[{"name": "f", "type": "faker", "faker_type": faker_type}])
    physical_plan, ctx, _ = _dispatch_triple(config)
    _assert_declines(physical_plan, ctx, monkeypatch)


def test_decline_custom_overridden_allowlisted_type(monkeypatch: pytest.MonkeyPatch) -> None:
    register_faker_provider("city", lambda fake: "OVERRIDDEN-CITY")
    try:
        config = _generate_config(columns=[{"name": "f", "type": "faker", "faker_type": "city"}])
        physical_plan, ctx, _ = _dispatch_triple(config)
        _assert_declines(physical_plan, ctx, monkeypatch)
    finally:
        unregister_faker_provider("city")


def test_decline_faker_determinism_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _generate_config(
        columns=[{"name": "f", "type": "faker", "faker_type": "city", "determinism": "fresh"}]
    )
    physical_plan, ctx, _ = _dispatch_triple(config)
    _assert_declines(physical_plan, ctx, monkeypatch)


@pytest.mark.parametrize(
    "faker_type",
    [None, [123], {"a": 1}],
    ids=["none", "list", "mapping"],
)
def test_decline_malformed_faker_type(faker_type: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    base_plan = _compiled_plan(_generate_config())
    decoded = _decode_config_json(base_plan)
    decoded["tables"][0]["generate_columns"][0]["faker_type"] = faker_type
    physical_plan, ctx, _ = _dispatch_from_mutated_config(base_plan, decoded)
    _assert_declines(physical_plan, ctx, monkeypatch)


# ---------------------------------------------------------------------------
# (c) Registry-snapshot race: a mutation between the shadow and oracle calls
# must not desync either side from the captured snapshot.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row_count", [PER_ROW_ROW_COUNT, POOLED_ROW_COUNT], ids=["per_row", "pooled"]
)
def test_race_custom_provider_mutation_uses_captured_snapshot_not_live(row_count: int) -> None:
    config = _generate_config(
        columns=[{"name": "f", "type": "faker", "faker_type": "city"}],
        row_count=row_count,
    )
    inputs = capture_physical_plan_inputs(
        config, {}, engine_version=ENGINE_VERSION, execution_mode="full_frame"
    )
    physical_plan = compile_physical_plan(inputs)

    # The built-in reference, captured before the live registry is touched.
    builtin_outputs = generate_tables(inputs.plan)

    provider_snapshot = snapshot_custom_faker_providers()
    assert "city" not in provider_snapshot
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan,
        key_provider=None,
        relationship_graph=inputs.graph,
        provider_snapshot=provider_snapshot,
    )
    snapshot = capture_shadow_snapshot({})

    register_faker_provider("city", lambda fake: "RACE-OVERRIDDEN-CITY")
    try:
        shadow_result = ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(
            physical_plan, snapshot
        )
        shadow_outputs = dict(shadow_result.outputs)
    finally:
        unregister_faker_provider("city")

    oracle_result = run_pipeline(
        config,
        {},
        engine_version=ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        key_provider=None,
        registry=inputs.registry,
        sink=None,
        _provider_snapshot=provider_snapshot,
    )
    oracle_outputs = dict(oracle_result.outputs)

    assert_generation_tables_arrow_ipc_equal(builtin_outputs, shadow_outputs)
    assert_generation_tables_arrow_ipc_equal(builtin_outputs, oracle_outputs)


# ---------------------------------------------------------------------------
# (d) Default-None contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row_count", [PER_ROW_ROW_COUNT, POOLED_ROW_COUNT], ids=["per_row", "pooled"]
)
def test_default_none_provider_snapshot_is_byte_unchanged(row_count: int) -> None:
    config = _generate_config(
        columns=[{"name": "f", "type": "faker", "faker_type": "city"}],
        row_count=row_count,
    )
    plan = _compiled_plan(config)
    implicit = generate_tables(plan)
    explicit_none = generate_tables(plan, provider_snapshot=None)
    assert_generation_tables_arrow_ipc_equal(implicit, explicit_none)


def test_default_none_still_observes_live_registry() -> None:
    """`None` means "read the live registry", not "an empty snapshot" --
    a custom registration must still be visible with no `provider_snapshot`
    argument at all."""
    config = _generate_config(
        columns=[{"name": "f", "type": "faker", "faker_type": "city"}],
        row_count=PER_ROW_ROW_COUNT,
    )
    plan = _compiled_plan(config)
    builtin = generate_tables(plan)

    register_faker_provider("city", lambda fake: "LIVE-OBSERVED-CITY")
    try:
        with_custom = generate_tables(plan)
    finally:
        unregister_faker_provider("city")

    assert (
        with_custom["people"].column("f").to_pylist() == ["LIVE-OBSERVED-CITY"] * PER_ROW_ROW_COUNT
    )
    assert (
        with_custom["people"].column("f").to_pylist() != builtin["people"].column("f").to_pylist()
    )


# ---------------------------------------------------------------------------
# (e) Mixed jobs.
# ---------------------------------------------------------------------------


def test_positive_mixed_faker_plus_sequence_admitted(tmp_path: Path) -> None:
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_source=accounts,
        mask_columns=[{"name": "acct_id", "strategy": "passthrough"}],
        generate_columns=[
            {"name": "id", "type": "sequence", "start": 1, "step": 1},
            {"name": "hometown", "type": "faker", "faker_type": "city"},
        ],
    )
    run = run_shadow_and_oracle(config, sources={"accounts": accounts})
    assert_generation_outputs_arrow_ipc_equal(run)


def test_decline_mixed_non_allowlisted_faker_column(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = _accounts_source()
    config = _mixed_config(
        tmp_path,
        mask_source=accounts,
        mask_columns=[{"name": "acct_id", "strategy": "passthrough"}],
        generate_columns=[
            {"name": "id", "type": "sequence", "start": 1, "step": 1},
            {"name": "contact", "type": "faker", "faker_type": "email"},
        ],
    )
    inputs = capture_physical_plan_inputs(
        config,
        {"accounts": accounts},
        engine_version=ENGINE_VERSION,
        execution_mode="full_frame",
    )
    physical_plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan, key_provider=None, relationship_graph=inputs.graph
    )
    snapshot = capture_shadow_snapshot({"accounts": accounts})
    calls = _spy_adapter_never_called(monkeypatch)

    with pytest.raises(ShadowDifference) as excinfo:
        ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(physical_plan, snapshot)

    assert excinfo.value.code == GENERATION_SHAPE_UNSUPPORTED
    assert calls == []
