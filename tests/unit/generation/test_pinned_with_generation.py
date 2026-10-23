"""GEN-API 1c-E2: `compile_pinned_plan_with_generation` returns the pinned plan and the
`GenerationPlan` from ONE compile, and `compile_pinned_plan` stays byte-for-byte unchanged."""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

import decoy_engine.plan as plan_module
from decoy_engine.generation import _pinned_compile
from decoy_engine.generation._window_admission import GenerationAdmissionError
from decoy_engine.generation.pinned import (
    PinnedCompilation,
    PinnedModeError,
    PinnedPlan,
    compile_pinned_plan,
    compile_pinned_plan_with_generation,
    execute_pinned,
    fingerprint,
)
from decoy_engine.generation.window import classify_window_from_generation_plan
from decoy_engine.plan._generation import snapshot_content_guard
from decoy_engine.quality.snapshot import compute_distribution_snapshot

REF = datetime(2026, 10, 22, 9, 30, 0, tzinfo=timezone.utc)
REF_TEXT = "2026-10-22T09:30:00Z"

# Fixed identity so the characterization values below do not move with library upgrades.
FIXED_IDENTITY = (
    ("engine", "0.0.0-test"),
    ("numpy", "1.0.0"),
    ("pandas", "2.0.0"),
    ("pyarrow", "3.0.0"),
    ("python", "3.12.0"),
    ("tz", "UTC"),
)

# Captured from the pre-refactor `compile_pinned_plan` (origin/main c0e2fb51) on `_main_config()`
# under FIXED_IDENTITY, before `compile_pinned_plan_with_generation` existed.
PRE_REFACTOR_FINGERPRINT = "a704276678f6d621957472739410f2a341b8e0cd33ea4dc74272ba4748a5e369"
PRE_REFACTOR_TO_DICT = json.loads(
    '{"reference_date": "2026-10-22T09:30:00Z", "runtime_identity": {"engine": "0.0.0-test", '
    '"numpy": "1.0.0", "pandas": "2.0.0", "pyarrow": "3.0.0", "python": "3.12.0", "tz": "UTC"}, '
    '"schema_version": "pinned-plan/v1", "statistical": [], "tables": [{"columns": ['
    '{"config_json": "{\\"name\\": \\"id\\", \\"start\\": 100, \\"step\\": 3, \\"type\\": '
    '\\"sequence\\"}", "name": "id"}, {"config_json": "{\\"categories\\": [\\"e\\", \\"w\\"], '
    '\\"name\\": \\"region\\", \\"type\\": \\"categorical\\"}", "name": "region"}], '
    '"depends_on": [], "name": "customers", "row_count": 12}, {"columns": [{"config_json": '
    '"{\\"name\\": \\"k\\", \\"start\\": 1, \\"type\\": \\"sequence\\"}", "name": "k"}], '
    '"depends_on": [], "name": "empty", "row_count": 0}, {"columns": [{"config_json": '
    '"{\\"name\\": \\"zed\\", \\"start\\": 1, \\"type\\": \\"sequence\\"}", "name": "zed"}, '
    '{"config_json": "{\\"categories\\": [\\"x\\", \\"y\\"], \\"name\\": \\"alpha\\", '
    '\\"type\\": \\"categorical\\"}", "name": "alpha"}, {"config_json": "{\\"name\\": '
    '\\"cust\\", \\"reference_column\\": \\"id\\", \\"reference_table\\": \\"customers\\", '
    '\\"type\\": \\"reference\\"}", "name": "cust"}], "depends_on": ["customers"], '
    '"name": "orders", "row_count": 7}]}'
)


def _main_config(seed: int = 5, reference_date: Any = None) -> dict[str, Any]:
    """Unequal row counts, a non-alphabetical column order, a reference dependency, a zero-row table.

    Declared order (orders, customers, empty) differs from the pinned name order.
    """
    settings: dict[str, Any] = {"seed": seed}
    if reference_date is not None:
        settings["reference_date"] = reference_date
    return {
        "global_settings": settings,
        "tables": [
            {
                "name": "orders",
                "row_count": 7,
                "generate_columns": [
                    {"name": "zed", "type": "sequence", "start": 1},
                    {"name": "alpha", "type": "categorical", "categories": ["x", "y"]},
                    {
                        "name": "cust",
                        "type": "reference",
                        "reference_table": "customers",
                        "reference_column": "id",
                    },
                ],
            },
            {
                "name": "customers",
                "row_count": 12,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 100, "step": 3},
                    {"name": "region", "type": "categorical", "categories": ["e", "w"]},
                ],
            },
            {
                "name": "empty",
                "row_count": 0,
                "generate_columns": [{"name": "k", "type": "sequence", "start": 1}],
            },
        ],
    }


def _write_snapshot(path: Path) -> str:
    rng = np.random.default_rng(7)
    df = pd.DataFrame({"score": rng.normal(100.0, 25.0, size=500).round(2)})
    path.write_text(json.dumps(compute_distribution_snapshot(df)), encoding="utf-8")
    return str(path)


def _stat_config(snapshot: str) -> dict[str, Any]:
    return {
        "global_settings": {"seed": 5},
        "tables": [
            {
                "name": "stat",
                "row_count": 20,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 1},
                    {
                        "name": "score",
                        "type": "statistical",
                        "snapshot_file": snapshot,
                        "null_probability": 0.1,
                    },
                ],
            }
        ],
    }


@pytest.fixture
def snap(tmp_path: Path) -> str:
    return _write_snapshot(tmp_path / "snap.json")


class _CompileSpy:
    """Wraps the real `compile_plan`, recording every call, the plan it returned and the guard."""

    def __init__(self) -> None:
        self.real = plan_module.compile_plan
        self.calls = 0
        self.plans: list[Any] = []
        self.guards: list[Any] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self.guards.append(snapshot_content_guard.get())
        plan = self.real(*args, **kwargs)
        self.plans.append(plan)
        return plan


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _CompileSpy:
    s = _CompileSpy()
    monkeypatch.setattr(plan_module, "compile_plan", s)
    return s


def _resolved(gen: Any) -> dict[str, Any]:
    return json.loads(gen.config_json)


def _pinned_cols(pinned: PinnedPlan, table: str) -> list[dict[str, Any]]:
    t = next(t for t in pinned.tables if t.name == table)
    return [json.loads(c.config_json) for c in t.columns]


# 1. one compile + object identity ----------------------------------------------------------


def test_one_compile_and_same_generation_object(spy: _CompileSpy) -> None:
    result = compile_pinned_plan_with_generation(_main_config(), REF)
    assert isinstance(result, PinnedCompilation)
    assert spy.calls == 1
    assert result.generation is spy.plans[0].generation
    assert isinstance(result.pinned, PinnedPlan)


def test_wrapper_also_compiles_once(spy: _CompileSpy) -> None:
    compile_pinned_plan(_main_config(), REF)
    assert spy.calls == 1


def test_bundle_is_frozen() -> None:
    result = compile_pinned_plan_with_generation(_main_config(), REF)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.pinned = result.pinned  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.generation = result.generation  # type: ignore[misc]


# 2. pinned vs generation projection agreement ----------------------------------------------


def _assert_projection_agrees(result: PinnedCompilation) -> None:
    resolved = _resolved(result.generation)
    by_name = {t["name"]: t for t in resolved["tables"]}
    assert {t.name for t in result.pinned.tables} == set(by_name)
    for pt in result.pinned.tables:
        gt = by_name[pt.name]
        assert pt.row_count == int(gt.get("row_count") or 0)
        assert [c.name for c in pt.columns] == [c["name"] for c in gt["generate_columns"]]
        assert list(pt.depends_on) == sorted(
            {c["reference_table"] for c in gt["generate_columns"] if c["type"] == "reference"}
        )
        for pc, gc in zip(pt.columns, gt["generate_columns"], strict=True):
            pinned_cfg = json.loads(pc.config_json)
            if gc["type"] == "statistical":
                assert pinned_cfg.pop("snapshot_file").startswith("sha256:")
                gc = {k: v for k, v in gc.items() if k != "snapshot_file"}
            assert pinned_cfg == gc


def test_projection_agrees_with_generation() -> None:
    result = compile_pinned_plan_with_generation(_main_config(), REF)
    _assert_projection_agrees(result)
    # Non-alphabetical declared order is kept; tables are sorted by name.
    assert [
        c.name for c in next(t for t in result.pinned.tables if t.name == "orders").columns
    ] == [
        "zed",
        "alpha",
        "cust",
    ]
    assert [t.name for t in result.pinned.tables] == ["customers", "empty", "orders"]
    assert {t.name: t.row_count for t in result.pinned.tables} == {
        "customers": 12,
        "empty": 0,
        "orders": 7,
    }
    assert next(t for t in result.pinned.tables if t.name == "orders").depends_on == ("customers",)


def test_statistical_digest_substitution(snap: str) -> None:
    result = compile_pinned_plan_with_generation(_stat_config(snap), REF)
    _assert_projection_agrees(result)
    gen = result.generation
    assert len(gen.snapshots) == 1
    digest = gen.snapshots[0].sha256
    generation_col = _resolved(gen)["tables"][0]["generate_columns"][1]
    assert generation_col["snapshot_file"] == snap  # generation keeps the path
    assert _pinned_cols(result.pinned, "stat")[1]["snapshot_file"] == f"sha256:{digest}"
    assert [(s.table, s.column, s.snapshot_digest) for s in result.pinned.statistical] == [
        ("stat", "score", digest)
    ]


def test_resolved_config_is_what_the_pinned_plan_is_built_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default the compiler adds to the resolved config appears in the pinned plan too."""
    real = plan_module.compile_plan

    def filled(*args: Any, **kwargs: Any) -> Any:
        plan = real(*args, **kwargs)
        resolved = json.loads(plan.generation.config_json)
        resolved["tables"][1]["generate_columns"][0]["prefix"] = "C-"
        gen = dataclasses.replace(plan.generation, config_json=json.dumps(resolved, sort_keys=True))
        return dataclasses.replace(plan, generation=gen)

    monkeypatch.setattr(plan_module, "compile_plan", filled)
    result = compile_pinned_plan_with_generation(_main_config(), REF)
    assert _pinned_cols(result.pinned, "customers")[0]["prefix"] == "C-"
    _assert_projection_agrees(result)


# 3. execution agreement --------------------------------------------------------------------


@pytest.mark.parametrize("which", ["main", "stat"])
def test_execute_pinned_matches_generation(which: str, snap: str) -> None:
    cfg = _main_config() if which == "main" else _stat_config(snap)
    result = compile_pinned_plan_with_generation(cfg, REF)
    out = execute_pinned(result.pinned, 17)
    admitted = {t["name"]: t for t in _resolved(result.generation)["tables"]}
    assert set(out) == set(admitted)
    for name, table in out.items():
        assert table.column_names == [c["name"] for c in admitted[name]["generate_columns"]]
        assert table.num_rows == int(admitted[name].get("row_count") or 0)
    if which == "main":
        assert out["empty"].num_rows == 0 and out["empty"].column_names == ["k"]


# 4. admission verdicts ---------------------------------------------------------------------


def test_admission_verdicts_per_table() -> None:
    gen = compile_pinned_plan_with_generation(_main_config(), REF).generation
    got = {
        name: classify_window_from_generation_plan(gen, name, window_end=2**63)
        for name in ("orders", "customers", "empty")
    }
    assert got["empty"].klass == "A" and got["empty"].class_b_reasons == ()
    assert got["customers"].klass == "B"
    assert got["customers"].class_b_reasons == (("region", "family_class_b:categorical"),)
    assert got["orders"].klass == "B"
    assert got["orders"].class_b_reasons == (
        ("alpha", "family_class_b:categorical"),
        ("cust", "family_class_b:reference"),
    )


def test_refusable_column_raises_the_coded_admission_error() -> None:
    cfg = {
        "global_settings": {"seed": 5},
        "tables": [
            {
                "name": "b",
                "row_count": 4,
                "generate_columns": [
                    {"name": "s", "type": "sequence", "start": 1, "step": 10**30, "pad_length": 1}
                ],
            }
        ],
    }
    gen = compile_pinned_plan_with_generation(cfg, REF).generation
    with pytest.raises(GenerationAdmissionError) as exc:
        classify_window_from_generation_plan(gen, "b", max_cell_chars=33, window_end=2**63)
    assert exc.value.code == "generation_unbounded_column"
    assert exc.value.column == "s"
    assert exc.value.table == "b"


# 5. wrapper delegation + pre-refactor characterization -------------------------------------


@pytest.fixture
def fixed_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_pinned_compile, "runtime_identity", lambda: FIXED_IDENTITY)


def test_wrapper_delegates_to_the_new_entry(fixed_identity: None) -> None:
    via_wrapper = compile_pinned_plan(_main_config(), REF)
    via_new = compile_pinned_plan_with_generation(_main_config(), REF).pinned
    assert via_wrapper == via_new
    assert fingerprint(via_wrapper) == fingerprint(via_new)


def test_pinned_output_matches_pre_refactor_values(fixed_identity: None) -> None:
    for plan in (
        compile_pinned_plan(_main_config(), REF),
        compile_pinned_plan_with_generation(_main_config(), REF).pinned,
    ):
        assert fingerprint(plan) == PRE_REFACTOR_FINGERPRINT
        assert plan.to_dict() == PRE_REFACTOR_TO_DICT


def test_reference_date_omitted_positional_and_keyword(fixed_identity: None) -> None:
    from_config = _main_config(reference_date=REF_TEXT)
    omitted = compile_pinned_plan(from_config)
    positional = compile_pinned_plan(_main_config(), REF)
    keyword = compile_pinned_plan(_main_config(), reference_date=REF)
    for plan in (omitted, positional, keyword):
        assert fingerprint(plan) == PRE_REFACTOR_FINGERPRINT
    for entry in (
        compile_pinned_plan_with_generation(from_config).pinned,
        compile_pinned_plan_with_generation(_main_config(), REF).pinned,
        compile_pinned_plan_with_generation(_main_config(), reference_date=REF).pinned,
    ):
        assert fingerprint(entry) == PRE_REFACTOR_FINGERPRINT


def test_explicit_reference_date_beats_the_config_field() -> None:
    later = REF + timedelta(days=3)
    cfg = _main_config(reference_date=REF_TEXT)
    for plan in (
        compile_pinned_plan(cfg, later),
        compile_pinned_plan_with_generation(cfg, later).pinned,
    ):
        assert plan.reference_date == "2026-10-25T09:30:00Z"


def test_pinned_plan_is_seed_free(fixed_identity: None) -> None:
    a = compile_pinned_plan_with_generation(_main_config(seed=1), REF).pinned
    b = compile_pinned_plan_with_generation(_main_config(seed=999999), REF).pinned
    assert a == b and a.to_dict() == b.to_dict()
    assert fingerprint(a) == fingerprint(b) == PRE_REFACTOR_FINGERPRINT


# 6. refusal parity, sequence and guard lifecycle -------------------------------------------


def _error(fn: Any) -> tuple[str, str]:
    with pytest.raises(PinnedModeError) as exc:
        fn()
    return exc.value.code, exc.value.message


def _both_raise(config: dict[str, Any], ref: Any = REF) -> tuple[str, str]:
    a = _error(lambda: compile_pinned_plan(config, ref))
    b = _error(lambda: compile_pinned_plan_with_generation(config, ref))
    assert a == b
    return a


def _with_column(col: dict[str, Any]) -> dict[str, Any]:
    cfg = _main_config()
    cfg["tables"][0]["generate_columns"].append(col)
    return cfg


def test_reference_date_missing() -> None:
    assert _both_raise(_main_config(), None) == (
        "pinned_reference_date_invalid",
        "reference_date is required: pass it or set global_settings.reference_date",
    )


def test_reference_date_naive_and_unparseable() -> None:
    assert _both_raise(_main_config(), datetime(2026, 10, 22, 9, 30)) == (
        "pinned_reference_date_invalid",
        "reference_date must be a timezone-aware datetime",
    )
    assert _both_raise(_main_config(), "nope") == (
        "pinned_reference_date_invalid",
        "reference_date is not an ISO 8601 datetime",
    )


def test_unpinnable_types() -> None:
    assert _both_raise(_with_column({"name": "n", "type": "faker", "faker_type": "name"})) == (
        "pinned_faker_unsupported",
        "table 'orders' column 'n': faker columns are not available in pinned mode",
    )
    assert _both_raise(_with_column({"name": "f", "type": "formula", "formula": "1 + 1"})) == (
        "pinned_formula_unsupported",
        "table 'orders' column 'f': formula columns are not available in pinned mode",
    )


def test_no_generate_columns_is_not_generation_only() -> None:
    cfg = _main_config()
    cfg["tables"][2]["generate_columns"] = []
    assert _both_raise(cfg) == (
        "pinned_not_generation_only",
        "table 'empty' has no generate_columns; pinned mode is generation-only",
    )


def test_live_token_in_config() -> None:
    cfg = _with_column({"name": "p", "type": "sequence", "prefix": "${env.X}"})
    assert _both_raise(cfg) == (
        "pinned_live_token",
        "config.tables[0].generate_columns[3].prefix holds an unresolved variable token",
    )


def test_live_token_inside_the_snapshot_guard(tmp_path: Path, snap: str) -> None:
    doc = json.loads(Path(snap).read_text(encoding="utf-8"))
    col = next(iter(doc["columns"]))
    doc["columns"][col]["extra"] = {"deep": ["ok", "x-${env.SECRET}"]}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    code, message = _both_raise(_stat_config(str(bad)))
    assert code == "pinned_live_token"
    assert message.startswith("snapshot") and "SECRET" not in message


def test_compile_with_nothing_to_generate(monkeypatch: pytest.MonkeyPatch) -> None:
    real = plan_module.compile_plan
    monkeypatch.setattr(
        plan_module,
        "compile_plan",
        lambda *a, **k: dataclasses.replace(real(*a, **k), generation=None),
    )
    assert _both_raise(_main_config()) == (
        "pinned_not_generation_only",
        "the job compiled with nothing to generate",
    )


def test_statistical_digest_missing(monkeypatch: pytest.MonkeyPatch, snap: str) -> None:
    real = plan_module.compile_plan

    def no_specs(*args: Any, **kwargs: Any) -> Any:
        plan = real(*args, **kwargs)
        gen = dataclasses.replace(plan.generation, statistical_specs=())
        return dataclasses.replace(plan, generation=gen)

    monkeypatch.setattr(plan_module, "compile_plan", no_specs)
    assert _both_raise(_stat_config(snap)) == (
        "pinned_statistical_digest_missing",
        "table 'stat' column 'score': no pinned snapshot digest",
    )


def test_guard_is_restored_after_success() -> None:
    before = snapshot_content_guard.get()
    compile_pinned_plan_with_generation(_main_config(), REF)
    assert snapshot_content_guard.get() is before


def test_guard_is_installed_while_compiling(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = plan_module.compile_plan

    def watch(*args: Any, **kwargs: Any) -> Any:
        seen.append(snapshot_content_guard.get())
        return real(*args, **kwargs)

    before = snapshot_content_guard.get()
    monkeypatch.setattr(plan_module, "compile_plan", watch)
    compile_pinned_plan_with_generation(_main_config(), REF)
    assert seen and seen[0] is not before


@pytest.mark.parametrize("entry", [compile_pinned_plan, compile_pinned_plan_with_generation])
def test_guard_is_restored_after_a_raising_compile(
    monkeypatch: pytest.MonkeyPatch, entry: Any
) -> None:
    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("compile failed")

    before = snapshot_content_guard.get()
    monkeypatch.setattr(plan_module, "compile_plan", boom)
    with pytest.raises(RuntimeError, match="compile failed"):
        entry(_main_config(), REF)
    assert snapshot_content_guard.get() is before


# 6b. precedence and the post-compile resolved recheck --------------------------------------


def test_live_token_wins_over_missing_reference_date() -> None:
    cfg = _with_column({"name": "p", "type": "sequence", "prefix": "${env.X}"})
    assert _both_raise(cfg, None) == (
        "pinned_live_token",
        "config.tables[0].generate_columns[3].prefix holds an unresolved variable token",
    )


def test_reference_date_error_wins_over_unpinnable_column() -> None:
    cfg = _with_column({"name": "n", "type": "faker", "faker_type": "name"})
    assert _both_raise(cfg, None) == (
        "pinned_reference_date_invalid",
        "reference_date is required: pass it or set global_settings.reference_date",
    )
    assert _both_raise(cfg, datetime(2026, 10, 22, 9, 30)) == (
        "pinned_reference_date_invalid",
        "reference_date must be a timezone-aware datetime",
    )


@pytest.mark.parametrize("entry", [compile_pinned_plan, compile_pinned_plan_with_generation])
def test_unpinnable_input_never_reaches_compile_plan(spy: _CompileSpy, entry: Any) -> None:
    cfg = _with_column({"name": "n", "type": "faker", "faker_type": "name"})
    with pytest.raises(PinnedModeError) as exc:
        entry(cfg, REF)
    assert exc.value.code == "pinned_faker_unsupported"
    assert spy.calls == 0


def test_resolved_config_unpinnable_after_compile(monkeypatch: pytest.MonkeyPatch) -> None:
    real = plan_module.compile_plan

    def resolves_to_faker(*args: Any, **kwargs: Any) -> Any:
        plan = real(*args, **kwargs)
        resolved = json.loads(plan.generation.config_json)
        resolved["tables"][0]["generate_columns"][0] = {
            "name": "zed",
            "type": "faker",
            "faker_type": "name",
        }
        gen = dataclasses.replace(plan.generation, config_json=json.dumps(resolved, sort_keys=True))
        return dataclasses.replace(plan, generation=gen)

    monkeypatch.setattr(plan_module, "compile_plan", resolves_to_faker)
    assert _both_raise(copy.deepcopy(_main_config())) == (
        "pinned_faker_unsupported",
        "table 'orders' column 'zed': faker columns are not available in pinned mode",
    )


# 7. exports --------------------------------------------------------------------------------


def test_exports() -> None:
    import decoy_engine.generation.pinned as pinned

    assert "PinnedCompilation" in pinned.__all__
    assert "compile_pinned_plan_with_generation" in pinned.__all__
    assert pinned.PinnedCompilation is PinnedCompilation
    assert pinned.compile_pinned_plan_with_generation is compile_pinned_plan_with_generation
