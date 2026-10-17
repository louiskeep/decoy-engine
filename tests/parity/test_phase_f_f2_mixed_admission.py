"""F2: two-stage admission and routing for the production mixed route.

STATIC declines happen before anything is generated and leave the job on the
existing full-frame path. RUNTIME declines look at the materialized generate output,
happen before any sink write, and reuse the outputs already generated (generation
never runs twice). Forced modes keep their existing meaning.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.errors import ConfigError
from decoy_engine.execution import _mixed_static
from decoy_engine.generation import synthesize
from decoy_engine.providers_v2 import get_default_registry
from tests.parity._f2_mixed_support import (
    CHILD,
    FK,
    PARENT,
    RecordingSink,
    assert_outputs_identical,
    child_table,
    mixed_config,
    oracle,
    run,
    warning_signature,
)

KEYS = [str(i % 5) for i in range(40)]


@pytest.fixture
def static_reasons(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The reason string of every static admission, in call order."""
    seen: list[str] = []
    real = _mixed_static.admit_mixed_static

    def spy(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        seen.append(result[1])
        return result

    monkeypatch.setattr(_mixed_static, "admit_mixed_static", spy)
    return seen


@pytest.fixture
def generate_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """One entry per `generate_tables` call; both routes import it at call time."""
    calls: list[int] = []
    real = synthesize.generate_tables

    def spy(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(synthesize, "generate_tables", spy)
    return calls


def _revalidate(config: dict[str, Any]) -> dict[str, Any]:
    return PipelineConfig.model_validate(config).model_dump()


def _assert_static_decline(
    config: dict[str, Any],
    sources: dict[str, Any],
    reasons: list[str],
    expected: str,
    **kwargs: Any,
) -> Any:
    result = run(config, sources, **kwargs)
    assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
    assert "mixed_route" not in result.quality_metrics
    assert reasons and expected in reasons[-1], reasons
    return result


# ---------------------------------------------------------------------------
# STATIC declines: the route is never entered, nothing is generated for it.
# ---------------------------------------------------------------------------


def test_composite_fk_key_declines(tmp_path: Path, static_reasons: list[str]) -> None:
    child = pa.table(
        {
            FK: pa.array(KEYS, pa.string()),
            "person_id2": pa.array(KEYS, pa.string()),
            "email": pa.array([f"u{i}@x.com" for i in range(40)], pa.string()),
        }
    )
    config = mixed_config(
        tmp_path, child, child_columns=[{"name": "person_id2", "strategy": "passthrough"}]
    )
    config["tables"][0]["generate_columns"].append(
        {"name": "id2", "type": "sequence", "start": 0, "step": 1}
    )
    config["relationships"][0]["parent"]["columns"] = ["id", "id2"]
    config["relationships"][0]["children"][0]["columns"] = [FK, "person_id2"]
    _assert_static_decline(
        _revalidate(config), {CHILD: child}, static_reasons, "mixed-fk-cross-generate-unsupported"
    )


def test_more_than_one_crossing_edge_declines(tmp_path: Path, static_reasons: list[str]) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    orders = pa.table({"buyer_id": pa.array(KEYS, pa.string())})
    orders_path = tmp_path / "orders.parquet"
    import pyarrow.parquet as pq

    pq.write_table(orders, orders_path)
    config["sources"]["orders"] = {"type": "file", "format": "parquet", "path": str(orders_path)}
    config["targets"]["orders"] = {"type": "file", "format": "csv", "path": "orders.out.csv"}
    config["targets"]["buyers"] = {"type": "file", "format": "csv", "path": "buyers.out.csv"}
    config["tables"] += [
        {
            "name": "buyers",
            "row_count": 5,
            "generate_columns": [{"name": "id", "type": "sequence", "start": 0, "step": 1}],
        },
        {"name": "orders", "columns": [{"name": "buyer_id", "strategy": "passthrough"}]},
    ]
    config["relationships"].append(
        {
            "parent": {"table": "buyers", "columns": ["id"]},
            "children": [{"table": "orders", "columns": ["buyer_id"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_buyers",
        }
    )
    _assert_static_decline(
        _revalidate(config),
        {CHILD: child, "orders": orders},
        static_reasons,
        "mixed-fk-topology-unsupported",
    )


def test_a_mask_to_mask_edge_declines(tmp_path: Path, static_reasons: list[str]) -> None:
    """Independent mixed job whose mask child also feeds another mask table: the
    per-table chunk config drops relationships, so the static stage must refuse it."""
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child, coupled=False)
    notes = pa.table({"email": pa.array([f"u{i}@x.com" for i in range(40)], pa.string())})
    import pyarrow.parquet as pq

    path = tmp_path / "notes.parquet"
    pq.write_table(notes, path)
    config["sources"]["notes"] = {"type": "file", "format": "parquet", "path": str(path)}
    config["targets"]["notes"] = {"type": "file", "format": "csv", "path": "notes.out.csv"}
    config["tables"].append(
        {"name": "notes", "columns": [{"name": "email", "strategy": "passthrough"}]}
    )
    config["relationships"] = [
        {
            "parent": {"table": CHILD, "columns": ["email"]},
            "children": [{"table": "notes", "columns": ["email"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_email",
        }
    ]
    _assert_static_decline(
        _revalidate(config),
        {CHILD: child, "notes": notes},
        static_reasons,
        "mixed-fk-topology-unsupported",
    )


def test_a_float_child_key_declines(tmp_path: Path, static_reasons: list[str]) -> None:
    child = child_table(pa.array([float(i % 5) for i in range(40)], pa.float64()))
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=[{"name": "id", "type": "categorical", "categories": [1.5, 2.5]}],
    )
    _assert_static_decline(
        config, {CHILD: child}, static_reasons, "mixed-fk-cross-generate-unsupported"
    )


def test_a_nullable_int_key_declines_to_full_frame(
    tmp_path: Path, static_reasons: list[str]
) -> None:
    """The planner's chunk gate refuses an integer column with nulls (its pandas
    round trip is not chunk-stable); the mixed route inherits that refusal."""
    keys = pa.array([None if i % 7 == 3 else 10 + i % 4 for i in range(40)], pa.int64())
    child = child_table(keys)
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=[{"name": "id", "type": "categorical", "categories": [10, 11, 12, 13]}],
    )
    result = _assert_static_decline(config, {CHILD: child}, static_reasons, "not_chunk_admissible")
    assert_outputs_identical(result.outputs, oracle(config, {CHILD: child}).outputs)


def test_a_transform_bearing_mask_table_declines(tmp_path: Path, static_reasons: list[str]) -> None:
    """The planner's chunk gate refuses per-table transforms, so the mixed route does too;
    the job keeps its existing outcome (here: the full-frame oracle's)."""
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    config["tables"][1]["transforms"] = [{"op": "dedupe", "columns": ["email"]}]
    result = _assert_static_decline(
        _revalidate(config), {CHILD: child}, static_reasons, "per_table_transforms_present"
    )
    assert result.quality_metrics["execution"]["route_reason"] == "generate_plus_mask"


def test_a_table_below_the_chunk_threshold_declines(
    tmp_path: Path, static_reasons: list[str]
) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    _assert_static_decline(
        config,
        {CHILD: child},
        static_reasons,
        "not_chunk_admissible",
        auto_chunk_threshold_rows=10_000,
    )


@pytest.mark.parametrize(
    ("kwargs", "token"),
    [
        ({"fidelity_report": True}, "fidelity_report_requested"),
        ({"post_validation": True}, "post_validation_requested"),
        ({"auto_chunk": False}, "auto_chunk_disabled"),
        ({"chunked_dispatcher_enabled": False}, "chunked_dispatcher_disabled"),
    ],
)
def test_job_settings_and_kill_switches_decline(
    tmp_path: Path, static_reasons: list[str], kwargs: dict[str, Any], token: str
) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    _assert_static_decline(config, {CHILD: child}, static_reasons, token, **kwargs)


def test_the_module_kill_switch_turns_the_route_off(
    tmp_path: Path, static_reasons: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    monkeypatch.setattr(_mixed_static, "MIXED_ROUTE_ENABLED", False)
    result = _assert_static_decline(config, {CHILD: child}, static_reasons, "mixed_route_disabled")
    assert_outputs_identical(result.outputs, oracle(config, {CHILD: child}).outputs)


def test_quarantine_declines(tmp_path: Path, static_reasons: list[str]) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    config["quarantine"] = {
        "enabled": True,
        "output_path": str(tmp_path / "quarantine.parquet"),
        "triggers": ["format_error"],
    }
    _assert_static_decline(
        _revalidate(config), {CHILD: child}, static_reasons, "quarantine_configured"
    )


def test_a_sibling_that_reads_the_fk_column_declines() -> None:
    """The FK column is carried as a pre-resolved value, so a sibling that reads it
    would see the resolved key where the oracle shows the source key."""
    edge = type("E", (), {"child_table": CHILD, "child_columns": (FK,)})()
    config = {
        "tables": [
            {
                "name": CHILD,
                "columns": [
                    {"name": FK, "strategy": "passthrough"},
                    {"name": "email", "strategy": "hash", "coherent_with": [FK]},
                ],
            }
        ]
    }
    narrowed, reason = _mixed_static._mask_table_config(config, CHILD, edge, get_default_registry())  # type: ignore[arg-type]
    assert narrowed is None and reason.startswith("fk_key_column_read_by_sibling")


def test_the_per_table_config_rewrites_only_the_fk_column() -> None:
    edge = type("E", (), {"child_table": CHILD, "child_columns": (FK,)})()
    config = {
        "global_settings": {"seed": 1},
        "relationships": [{"parent": {}}],
        "sources": {CHILD: {}, "other": {}},
        "targets": {CHILD: {}, "other": {}},
        "tables": [
            {"name": PARENT, "generate_columns": [{"name": "id"}]},
            {
                "name": CHILD,
                "columns": [
                    {"name": FK, "strategy": "hash", "namespace": "ns", "vault": True},
                    {"name": "email", "strategy": "hash", "namespace": "ns_email"},
                ],
            },
        ],
    }
    narrowed, _ = _mixed_static._mask_table_config(config, CHILD, edge, get_default_registry())  # type: ignore[arg-type]
    assert narrowed is not None
    assert narrowed["relationships"] == []
    assert [t["name"] for t in narrowed["tables"]] == [CHILD]
    assert list(narrowed["sources"]) == [CHILD] and list(narrowed["targets"]) == [CHILD]
    fk, email = narrowed["tables"][0]["columns"]
    assert fk["strategy"] == "passthrough" and fk["namespace"] is None and fk["vault"] is False
    assert email == config["tables"][1]["columns"][1]
    assert config["tables"][1]["columns"][0]["strategy"] == "hash"  # input untouched


# ---------------------------------------------------------------------------
# Forced modes keep their existing meaning.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["out_of_core", "sequential"])
@pytest.mark.parametrize("coupled", [True, False], ids=["coupled", "independent"])
def test_forced_bounded_modes_reject_a_mixed_job_with_a_typed_config_error(
    tmp_path: Path, mode: str, coupled: bool
) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child, coupled=coupled)
    with pytest.raises(ConfigError):
        run(config, {CHILD: child}, mode=mode)


def test_forced_full_frame_runs_the_oracle_and_never_raises(
    tmp_path: Path, static_reasons: list[str]
) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    result = run(config, {CHILD: child}, mode="full_frame")
    assert result.quality_metrics["execution"]["route_reason"] == "override_full_frame"
    assert result.quality_metrics["execution"]["execution_mode"] == "full_frame"
    assert static_reasons == []  # a forced mode never offers the job to the mixed route


def test_a_large_coupled_job_no_bounded_route_can_take_keeps_the_oom_reject(
    tmp_path: Path,
) -> None:
    """When the mixed route declines, the existing fail-closed reject still fires for
    a coupled job past the full-frame threshold; it is not routed to an unbounded path."""
    from decoy_engine.execution import ExecutionError

    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    with pytest.raises(ExecutionError) as excinfo:
        run(config, {CHILD: child}, chunked_dispatcher_enabled=False, full_frame_reject_rows=10)
    assert excinfo.value.code.startswith("fk_full_frame_oom_risk_rejected")


# ---------------------------------------------------------------------------
# RUNTIME declines: after generation, before any sink write, no regeneration.
# ---------------------------------------------------------------------------

DIRTY = {
    "null_probability": [
        {"name": "tier", "type": "categorical", "categories": ["A", "B"], "null_probability": 1.0}
    ],
    "none_category": [
        {"name": "tier", "type": "categorical", "categories": ["A", None], "weights": [0, 1]}
    ],
    "nan_category": [
        {"name": "amt", "type": "categorical", "categories": [float("nan"), 1.0], "weights": [1, 0]}
    ],
    "nested_category": [{"name": "tags", "type": "categorical", "categories": [[1, 2], [3, 4]]}],
}


@pytest.mark.parametrize("dirt", sorted(DIRTY))
def test_a_dirty_non_key_column_on_a_coupled_parent_falls_back_without_regenerating(
    tmp_path: Path, generate_calls: list[int], dirt: str
) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=[*[{"name": "id", "type": "sequence", "start": 0, "step": 1}], *DIRTY[dirt]],
    )
    want = oracle(config, {CHILD: child})
    generate_calls.clear()
    sink = RecordingSink()
    got = run(config, {CHILD: child}, sink=sink)
    assert len(generate_calls) == 1  # the fallback reuses the generated outputs
    execution = got.quality_metrics["execution"]
    assert execution["execution_mode"] == "full_frame"
    assert execution["route_reason"] == "mixed_runtime_declined:generation-shape-unsupported"
    assert sink.calls == []  # declined before any sink write
    assert_outputs_identical(got.outputs, want.outputs)


@pytest.mark.parametrize("dirt", sorted(DIRTY))
def test_a_dirty_column_on_an_independent_generate_table_falls_back_without_regenerating(
    tmp_path: Path, generate_calls: list[int], dirt: str
) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child, coupled=False, parent_columns=DIRTY[dirt])
    want = oracle(config, {CHILD: child})
    generate_calls.clear()
    sink = RecordingSink()
    got = run(config, {CHILD: child}, sink=sink)
    assert len(generate_calls) == 1
    assert got.quality_metrics["execution"]["route_reason"].startswith("mixed_runtime_declined:")
    assert sink.calls == []
    assert_outputs_identical(got.outputs, want.outputs)


def test_a_non_int_non_string_parent_key_falls_back_after_generation(
    tmp_path: Path, generate_calls: list[int]
) -> None:
    child = child_table(pa.array([i % 2 for i in range(40)], pa.int64()))
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=[{"name": "id", "type": "categorical", "categories": [1.5, 2.5]}],
    )
    generate_calls.clear()
    got = run(config, {CHILD: child})
    assert len(generate_calls) == 1
    assert (
        got.quality_metrics["execution"]["route_reason"]
        == "mixed_runtime_declined:mixed-fk-cross-generate-unsupported"
    )
    assert_outputs_identical(got.outputs, oracle(config, {CHILD: child}).outputs)


@pytest.mark.filterwarnings("ignore:Mask-table row count disagrees")
def test_a_changed_lazy_source_declines_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The footer is re-read after generation; if it moved since static admission the
    route declines to full-frame (which reads the new file) instead of streaming."""
    from tests.parity._f2_mixed_support import lazy_source

    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    lazy = lazy_source(tmp_path, child, row_group_size=9)
    real = _mixed_static.admit_mixed_static

    def admit_then_change(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        lazy_source(tmp_path, child_table(pa.array(KEYS[:30], pa.string())), row_group_size=9)
        return result

    monkeypatch.setattr(_mixed_static, "admit_mixed_static", admit_then_change)
    sink = RecordingSink()
    got = run(config, {CHILD: lazy}, sink=sink)
    assert (
        got.quality_metrics["execution"]["route_reason"]
        == "mixed_runtime_declined:lazy_source_changed"
    )
    assert sink.calls == []


# ---------------------------------------------------------------------------
# Fail-closed sibling reads and extra caller sources.
# ---------------------------------------------------------------------------

BIG = 2**63 + 1


def _uint64_keys() -> pa.Array:
    return pa.array([BIG if i % 3 == 0 else 1 + i % 4 for i in range(40)], pa.uint64())


def _uint64_job(tmp_path: Path, email_when: str | None) -> tuple[dict[str, Any], pa.Table]:
    child = child_table(_uint64_keys())
    email: dict[str, Any] = {"name": "email", "strategy": "hash", "namespace": "ns_email"}
    if email_when is not None:
        email["when"] = email_when
    config = mixed_config(
        tmp_path,
        child,
        parent_columns=[
            {"name": "id", "type": "categorical", "categories": [str(BIG), "1", "2", "3"]}
        ],
        child_columns=[email, {"name": "note", "strategy": "redact"}],
    )
    return config, child


def test_a_when_predicate_reading_the_fk_column_declines_and_masks_like_the_oracle(
    tmp_path: Path, static_reasons: list[str]
) -> None:
    """Numexpr would read the carried uint64 key as signed and flip `person_id > 0`, leaving
    the gated email raw; the declared read set catches the parsed predicate."""
    config, child = _uint64_job(tmp_path, f"{FK} > 0")
    result = _assert_static_decline(
        config, {CHILD: child}, static_reasons, "fk_key_column_read_by_sibling"
    )
    want = oracle(config, {CHILD: child})
    assert_outputs_identical(result.outputs, want.outputs)
    assert not set(result.outputs[CHILD].column("email").to_pylist()) & set(
        child.column("email").to_pylist()
    )


def test_the_same_shape_without_the_fk_reading_when_routes_mixed(
    tmp_path: Path, static_reasons: list[str]
) -> None:
    config, child = _uint64_job(tmp_path, None)
    result = run(config, {CHILD: child})
    want = oracle(config, {CHILD: child})
    assert static_reasons and static_reasons[-1] == "admitted", static_reasons
    assert "mixed_route" in result.quality_metrics
    assert_outputs_identical(result.outputs, want.outputs)


def test_an_unparseable_sibling_when_declines_rather_than_guessing() -> None:
    edge = type("E", (), {"child_table": CHILD, "child_columns": (FK,)})()
    config = {
        "tables": [
            {
                "name": CHILD,
                "columns": [
                    {"name": FK, "strategy": "passthrough"},
                    {"name": "email", "strategy": "hash", "when": "f'{x}' == 'a'"},
                ],
            }
        ]
    }
    narrowed, reason = _mixed_static._mask_table_config(
        config,
        CHILD,
        edge,
        get_default_registry(),  # type: ignore[arg-type]
    )
    assert narrowed is None and reason.startswith("fk_key_column_read_unknown")


@pytest.mark.parametrize("policy", ["warn", "error"])
def test_an_extra_unconfigured_caller_source_declines_to_the_oracle(
    tmp_path: Path, static_reasons: list[str], policy: str
) -> None:
    child = child_table(pa.array(KEYS, pa.string()))
    config = mixed_config(tmp_path, child)
    config["global_settings"]["unconfigured_column_policy"] = policy
    sources = {CHILD: child, "extra": pa.table({"x": pa.array([1, 2, 3])})}

    def outcome(mode: str) -> Any:
        try:
            return run(config, sources, mode=mode)
        except Exception as exc:
            return exc

    got, want = outcome("auto"), outcome("full_frame")
    assert static_reasons and "unconfigured_extra_source_present" in static_reasons[-1]
    if isinstance(want, Exception):
        assert type(got) is type(want) and str(got) == str(want)
    else:
        assert got.quality_metrics["execution"]["execution_mode"] == "full_frame"
        assert "mixed_route" not in got.quality_metrics
        assert_outputs_identical(got.outputs, want.outputs)
        assert warning_signature(got) == warning_signature(want)
