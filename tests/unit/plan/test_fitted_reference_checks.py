"""AT-6b: a DP-declared pipeline refuses `distribution: fitted` on a reference column.

The exact source degree histogram must not reach DP output. The one decision point is
`check_dp_generate_contract`; compile, the config-only checks and the load of a serialized Plan
all go through it, before verification.
"""

from __future__ import annotations

import ast
import base64
import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml

from decoy_engine import run_config_only_checks
from decoy_engine.plan import PlanCompileError, _checks_dp, _compile, _serialize, compile_plan
from decoy_engine.plan._checks_dp import check_dp_generate_contract
from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
from decoy_engine.profile import Profile
from decoy_engine.quality.snapshot import compute_distribution_snapshot

CODE = "dp_generate_fitted_fanout_unsupported"
SYMBOL = CODE.upper()


def _profile() -> Profile:
    return Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime.now(timezone.utc),
        decoy_engine_version="test",
    )


def _config(distribution: str, *, dp: bool = True, **extra: Any) -> dict[str, Any]:
    settings: dict[str, Any] = {"seed": 1}
    if dp:
        settings["dp"] = {"epsilon": 5.0, "delta": 1e-6}
    return {
        "global_settings": settings,
        "tables": [
            {
                "name": "par",
                "row_count": 5,
                "generate_columns": [{"name": "k", "type": "sequence"}],
            },
            {
                "name": "child",
                "row_count": 20,
                "generate_columns": [
                    {
                        "name": "r",
                        "type": "reference",
                        "reference_table": "par",
                        "reference_column": "k",
                        "distribution": distribution,
                        **extra,
                    }
                ],
            },
        ],
    }


def test_contract_refuses_fitted_reference_under_dp() -> None:
    with pytest.raises(PlanCompileError) as exc:
        check_dp_generate_contract(_config("fitted"))
    assert exc.value.code == CODE
    assert "random" in str(exc.value)
    assert "tables.child.generate_columns.r" in exc.value.path


@pytest.mark.parametrize("distribution", ["random", "sequential", "weighted"])
def test_contract_passes_other_distributions_under_dp(distribution: str) -> None:
    extra = {"weights": [1, 2, 3]} if distribution == "weighted" else {}
    check_dp_generate_contract(_config(distribution, **extra))


@pytest.mark.parametrize("col_type", ["statistical", "windowed_date", "categorical"])
def test_contract_ignores_fitted_distribution_on_non_reference_columns(col_type: str) -> None:
    cfg = _config("random")
    cfg["tables"][1]["generate_columns"] = [
        {"name": "x", "type": col_type, "distribution": "fitted"}
    ]
    check_dp_generate_contract(cfg)


def test_contract_passes_fitted_reference_without_dp() -> None:
    check_dp_generate_contract(_config("fitted", dp=False))


def test_compile_plan_refuses_before_snapshot_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = _compile.check_dp_generate_contract

    def spy(config: dict[str, Any]) -> None:
        calls.append("contract")
        real(config)

    def no_verify(*_a: Any, **_k: Any) -> None:
        raise AssertionError("snapshot verification ran before the DP contract")

    monkeypatch.setattr(_compile, "check_dp_generate_contract", spy)
    monkeypatch.setattr(_compile, "verify_dp_snapshots", no_verify)
    with pytest.raises(PlanCompileError) as exc:
        compile_plan(_config("fitted"), _profile(), decoy_engine_version="test")
    assert exc.value.code == CODE
    assert calls == ["contract"]


def test_config_only_checks_refuse(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = _compile.check_dp_generate_contract

    def spy(config: dict[str, Any]) -> None:
        calls.append("contract")
        real(config)

    monkeypatch.setattr(_compile, "check_dp_generate_contract", spy)
    with pytest.raises(PlanCompileError) as exc:
        run_config_only_checks(_config("fitted"))
    assert exc.value.code == CODE
    assert calls == ["contract"]


def test_compile_and_config_only_pass_with_random() -> None:
    cfg = _config("random")
    run_config_only_checks(cfg)
    assert compile_plan(cfg, _profile(), decoy_engine_version="test").generation is not None


def _serialized_with(distribution: str) -> str:
    plan = compile_plan(_config("random"), _profile(), decoy_engine_version="test")
    data = yaml.safe_load(plan_to_yaml(plan))
    config = json.loads(data["generation"]["config_json"])
    child = next(t for t in config["tables"] if t["name"] == "child")
    child["generate_columns"][0]["distribution"] = distribution
    data["generation"]["config_json"] = json.dumps(config)
    return yaml.safe_dump(copy.deepcopy(data), sort_keys=False)


def test_serialized_plan_with_fitted_under_dp_is_refused_before_snapshot_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = _serialize.check_dp_generate_contract

    def spy(config: dict[str, Any]) -> None:
        calls.append("contract")
        real(config)

    def no_verify(*_a: Any, **_k: Any) -> None:
        raise AssertionError("snapshot verification ran before the DP contract")

    tampered = _serialized_with("fitted")
    monkeypatch.setattr(_serialize, "check_dp_generate_contract", spy)
    monkeypatch.setattr(_serialize, "verify_dp_snapshots", no_verify)
    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(tampered)
    assert exc.value.code == CODE
    assert calls == ["contract"]


def test_serialized_plan_with_random_under_dp_loads() -> None:
    reloaded = plan_from_yaml(_serialized_with("random"))
    assert reloaded.generation is not None


def test_dp_contract_is_the_only_dp_fitted_predicate() -> None:
    import inspect

    sources = {
        "_compile": inspect.getsource(_compile),
        "_serialize": inspect.getsource(_serialize),
    }
    for name, text in sources.items():
        assert SYMBOL not in text, name
    assert SYMBOL in inspect.getsource(_checks_dp)


# The refusal needs only the config, so it must fire before any snapshot is read or decoded.
# A DP config with a fitted fan-out and a statistical column that names a bad snapshot file
# must report the DP code, never a file, digest or JSON error from the snapshot itself.


def _config_with_snapshot(path: str) -> dict[str, Any]:
    cfg = _config("fitted")
    cfg["tables"][0]["generate_columns"].append(
        {"name": "s", "type": "statistical", "snapshot_file": path, "source_column": "x"}
    )
    return cfg


def _snapshot_path(kind: str, tmp_path: Any) -> str:
    if kind == "missing":
        return str(tmp_path / "absent.json")
    bad = tmp_path / f"{kind}.json"
    bad.write_text("{not json" if kind == "malformed" else "{}", encoding="utf-8")
    if kind == "unreadable":
        bad.chmod(0)
    return str(bad)


def _forbid_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_read(*_a: Any, **_k: Any) -> None:
        raise AssertionError("snapshot read ran before the DP contract")

    monkeypatch.setattr(_compile, "read_and_pin_snapshots", no_read)


@pytest.mark.parametrize("kind", ["missing", "unreadable", "malformed"])
def test_compile_plan_refuses_before_any_snapshot_read(
    kind: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_with_snapshot(_snapshot_path(kind, tmp_path))
    _forbid_read(monkeypatch)
    with pytest.raises(PlanCompileError) as exc:
        compile_plan(cfg, _profile(), decoy_engine_version="test")
    assert exc.value.code == CODE


@pytest.mark.parametrize("kind", ["missing", "unreadable", "malformed"])
def test_config_only_checks_refuse_before_any_snapshot_read(
    kind: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_with_snapshot(_snapshot_path(kind, tmp_path))
    _forbid_read(monkeypatch)
    with pytest.raises(PlanCompileError) as exc:
        run_config_only_checks(cfg)
    assert exc.value.code == CODE


@pytest.mark.parametrize(
    "snapshots",
    [
        [{"path": "gone.json"}],
        [{"path": "x.json", "sha256": "0" * 64, "payload_b64": "!!not-base64!!"}],
        [{}],
    ],
    ids=["missing-fields", "bad-payload", "empty-entry"],
)
def test_plan_from_yaml_refuses_before_decoding_snapshots(
    snapshots: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    data = yaml.safe_load(_serialized_with("fitted"))
    data["generation"]["snapshots"] = snapshots

    def no_decode(*_a: Any, **_k: Any) -> None:
        raise AssertionError("snapshot decode ran before the DP contract")

    monkeypatch.setattr(_serialize, "_pinned_snapshot_from_dict", no_decode)
    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(yaml.safe_dump(data, sort_keys=False))
    assert exc.value.code == CODE


# AT-6 / AT-7b (E2a): the fitted snapshot checks at validate, run and plan load.

CANARY = "CANARY-7f3a9"
_SNAP = "visits.snap.json"


def _fitted_snapshot(*, fanout: bool = True) -> dict[str, Any]:
    df = pd.DataFrame({"k": ["a"] * 3 + ["b"] * 1 + ["c"] * 1 + ["d"] * 1 + ["e"] * 2})
    return compute_distribution_snapshot(df, fanout_columns=["k"] if fanout else [])


def _write(tmp_path: Path, snapshot: dict[str, Any]) -> str:
    path = tmp_path / _SNAP
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return str(path)


def _fitted_config(snapshot_file: str | None, **extra: Any) -> dict[str, Any]:
    col: dict[str, Any] = {
        "name": "r",
        "type": "reference",
        "reference_table": "par",
        "reference_column": "k",
        "distribution": "fitted",
        "source_column": "k",
        **extra,
    }
    if snapshot_file is not None:
        col["snapshot_file"] = snapshot_file
    cfg = _config("random", dp=False)
    cfg["tables"][1]["generate_columns"] = [col]
    return cfg


def _refusal(cfg: dict[str, Any]) -> PlanCompileError:
    with pytest.raises(PlanCompileError) as exc:
        compile_plan(cfg, _profile(), decoy_engine_version="test")
    with pytest.raises(PlanCompileError) as exc_cfg:
        run_config_only_checks(cfg)
    assert exc_cfg.value.code == exc.value.code
    return exc.value


def test_valid_fitted_reference_compiles_and_pins_the_snapshot(tmp_path: Path) -> None:
    path = _write(tmp_path, _fitted_snapshot())
    run_config_only_checks(_fitted_config(path))
    plan = compile_plan(_fitted_config(path), _profile(), decoy_engine_version="test")
    assert plan.generation is not None
    assert [s.source_path for s in plan.generation.snapshots] == [path]
    assert plan.generation.statistical_specs == ()


def test_refuses_without_snapshot_file() -> None:
    err = _refusal(_fitted_config(None))
    assert err.code == "reference_fanout_missing"
    assert "snapshot_file" in err.message


def test_refuses_unreadable_snapshot(tmp_path: Path) -> None:
    err = _refusal(_fitted_config(str(tmp_path / "absent.json")))
    assert err.code == "statistical_snapshot_unreadable"


def test_refuses_absent_source_column(tmp_path: Path) -> None:
    path = _write(tmp_path, _fitted_snapshot())
    assert _refusal(_fitted_config(path, source_column="nosuch")).code == "reference_fanout_missing"


@pytest.mark.parametrize("snapshot", [_fitted_snapshot(fanout=False)], ids=["no-fanout-key"])
def test_refuses_snapshot_without_fanout(tmp_path: Path, snapshot: dict[str, Any]) -> None:
    err = _refusal(_fitted_config(_write(tmp_path, snapshot)))
    assert err.code == "reference_fanout_missing"
    assert "--fanout" in err.message


def _with_block(block: Any) -> dict[str, Any]:
    snap = _fitted_snapshot()
    snap["columns"]["k"]["fanout"] = block
    return snap


_GOOD = {"rows": 8, "keys": 5, "degrees": [[1, 3], [2, 1], [3, 1]]}
_BAD_BLOCKS = {
    "keys-zero": {"rows": 0, "keys": 0, "degrees": []},
    "totals-off": {**_GOOD, "rows": 9},
    "zero-degree": {"rows": 5, "keys": 5, "degrees": [[0, 2], [1, 3]]},
    "unsorted": {"rows": 8, "keys": 5, "degrees": [[2, 1], [1, 3], [3, 1]]},
    "non-dict": [1, 2],
    "partial-degrees": {**_GOOD, "degrees": [[1, 3]]},
    "pair-wrong-len": {**_GOOD, "degrees": [[1, 3, 9]]},
    "bool-rows": {**_GOOD, "rows": True},
    "float-keys": {**_GOOD, "keys": 5.0},
    "str-rows": {**_GOOD, "rows": CANARY},
    "bool-degree": {**_GOOD, "degrees": [[True, 3], [2, 1], [3, 1]]},
    "float-parents": {**_GOOD, "degrees": [[1, 3.0], [2, 1], [3, 1]]},
    "numeric-string-degree": {**_GOOD, "degrees": [["1", 3], [2, 1], [3, 1]]},
    "canary-parents": {**_GOOD, "degrees": [[1, CANARY], [2, 1], [3, 1]]},
}


@pytest.mark.parametrize("name", sorted(_BAD_BLOCKS))
def test_refuses_malformed_fanout_block_value_free(tmp_path: Path, name: str) -> None:
    err = _refusal(_fitted_config(_write(tmp_path, _with_block(_BAD_BLOCKS[name]))))
    assert err.code == "reference_fanout_invalid"
    assert CANARY not in str(err) and CANARY not in err.message


@pytest.mark.parametrize(
    "extra", [{"weights": [1, 2]}, {"min_per_parent": 1}, {"max_per_parent": 2}]
)
def test_refuses_conflicting_knobs(tmp_path: Path, extra: dict[str, Any]) -> None:
    path = _write(tmp_path, _fitted_snapshot())
    assert _refusal(_fitted_config(path, **extra)).code == "reference_fanout_conflict"


def test_non_fitted_reference_is_not_checked() -> None:
    run_config_only_checks(_config("random", dp=False))


# AT-7b: plan load.


def _plan_yaml(tmp_path: Path) -> dict[str, Any]:
    path = _write(tmp_path, _fitted_snapshot())
    plan = compile_plan(_fitted_config(path), _profile(), decoy_engine_version="test")
    return yaml.safe_load(plan_to_yaml(plan))


def test_plan_round_trip_loads(tmp_path: Path) -> None:
    data = _plan_yaml(tmp_path)
    assert plan_from_yaml(yaml.safe_dump(data, sort_keys=False)).generation is not None


def test_plan_load_refuses_fitted_snapshot_removed_from_manifest(tmp_path: Path) -> None:
    data = _plan_yaml(tmp_path)
    data["generation"]["snapshots"] = []
    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(yaml.safe_dump(data, sort_keys=False))
    assert exc.value.code == "statistical_snapshot_not_pinned"


def test_plan_load_refuses_broken_embedded_fanout(tmp_path: Path) -> None:
    data = _plan_yaml(tmp_path)
    entry = data["generation"]["snapshots"][0]
    snap = _with_block({**_GOOD, "rows": 9})
    raw = json.dumps(snap).encode()
    entry["payload_b64"] = base64.b64encode(raw).decode("ascii")
    entry["sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(PlanCompileError) as exc:
        plan_from_yaml(yaml.safe_dump(data, sort_keys=False))
    assert exc.value.code == "reference_fanout_invalid"


def test_check_statistical_columns_is_called_only_by_the_wrapper() -> None:
    import decoy_engine.plan as plan_pkg

    callers: list[str] = []
    for src in Path(plan_pkg.__file__).parent.glob("*.py"):
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                    if name == "check_statistical_columns":
                        callers.append(f"{src.name}:{fn.name}")
    assert callers == ["_generation.py:check_generate_snapshot_columns"]


def test_fitted_check_never_reopens_an_unpinned_path(tmp_path: Path) -> None:
    from decoy_engine.plan._generation import check_generate_snapshot_columns

    cfg = _fitted_config(_write(tmp_path, _fitted_snapshot()))
    with pytest.raises(PlanCompileError) as exc:
        check_generate_snapshot_columns(cfg, {}, frozenset(), None)
    assert exc.value.code == "statistical_snapshot_unreadable"
