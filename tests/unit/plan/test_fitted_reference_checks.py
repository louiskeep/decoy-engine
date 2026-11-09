"""AT-6b: a DP-declared pipeline refuses `distribution: fitted` on a reference column.

The exact source degree histogram must not reach DP output. The one decision point is
`check_dp_generate_contract`; compile, the config-only checks and the load of a serialized Plan
all go through it, before verification.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any

import pytest
import yaml

from decoy_engine import run_config_only_checks
from decoy_engine.plan import PlanCompileError, _checks_dp, _compile, _serialize, compile_plan
from decoy_engine.plan._checks_dp import check_dp_generate_contract
from decoy_engine.plan._serialize import plan_from_yaml, plan_to_yaml
from decoy_engine.profile import Profile

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
