"""Required strategy settings: one mapping, one check, the same code the executor guard raises."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest

from decoy_engine import reasons
from decoy_engine.execution import _strategies
from decoy_engine.plan import PlanCompileError, compile_plan, run_config_only_checks
from decoy_engine.plan.checks import (
    REQUIREMENTS_BY_STRATEGY,
    StrategyRequirement,
    check_required_strategy_settings,
)
from decoy_engine.profile import Profile

SRC = Path(__file__).resolve().parents[4] / "src" / "decoy_engine"
GUARD_NAME = re.compile(r"reasons\.runtime\.([A-Z_]+(?:_REQUIRES_NAMESPACE|_UNSET))\b")
# A runtime guard with no descriptor needs a reason here. None today: every guard that matches
# the pattern is reachable from a config column and has a mapping entry.
EXCLUDED_GUARDS: dict[str, str] = {}

# A deterministic column without a namespace is refused earlier by the namespace completeness
# check, which keeps precedence over the descriptor.
EARLIER_CHECK = {"shuffle": {"deterministic_namespace_missing", "namespace_missing"}}

SATISFIED: dict[str, dict[str, Any]] = {
    "hash": {"namespace": "ns"},
    "fpe": {"namespace": "ns"},
    "date_shift": {"namespace": "ns"},
    "bucket_perturb": {"namespace": "ns"},
    "categorical": {"namespace": "ns", "provider_config": {"categories": ["a", "b"]}},
    "tokenize": {"namespace": "ns"},
    "shuffle": {"namespace": "ns", "deterministic": True},
    "code_set": {"namespace": "ns", "provider_config": {"mode": "gen"}},
    "nested": {"provider_config": {"strategy": "redact", "target": "$.a"}},
}


def _config(strategy: str, **column: Any) -> dict[str, Any]:
    entry = {"name": "name", "strategy": strategy, **column}
    return {"tables": [{"name": "customers", "columns": [entry]}]}


def _without(strategy: str, path: tuple[str, ...]) -> dict[str, Any]:
    """The satisfied column of ``strategy`` with the value at ``path`` removed."""
    column = copy.deepcopy(SATISFIED[strategy])
    node = column
    for key in path[:-1]:
        node = node[key]
    del node[path[-1]]
    return _config(strategy, **column)


def _both_entry_points(config: dict[str, Any], profile: Profile) -> list[PlanCompileError]:
    errors = []
    for call in (
        lambda: run_config_only_checks(config),
        lambda: compile_plan(config, profile, decoy_engine_version="0.1.0"),
    ):
        with pytest.raises(PlanCompileError) as exc:
            call()
        errors.append(exc.value)
    return errors


def test_required_setting_refused_at_validation(simple_profile: Profile) -> None:
    """AT-E2b-3: explicit fpe without a namespace fails both compile entry points."""
    for error in _both_entry_points(_config("fpe", provider_config={}), simple_profile):
        assert error.code == "fpe_requires_namespace"
        assert error.path == "tables.customers.columns.name.namespace"


def test_every_mapping_row_is_refused_at_both_entry_points_and_projected(
    simple_profile: Profile,
) -> None:
    catalog = {row.name: row for row in _strategies.strategy_catalog()}
    assert set(REQUIREMENTS_BY_STRATEGY) <= set(catalog)
    assert set(SATISFIED) == set(REQUIREMENTS_BY_STRATEGY)
    for strategy, requirements in REQUIREMENTS_BY_STRATEGY.items():
        assert catalog[strategy].requirements == requirements
        for req in requirements:
            config = _without(strategy, req.path)
            with pytest.raises(PlanCompileError) as direct:
                check_required_strategy_settings(config)
            assert direct.value.code == req.code
            assert direct.value.path.endswith("." + ".".join(req.path))
            for error in _both_entry_points(config, simple_profile):
                assert error.code in EARLIER_CHECK.get(strategy, {req.code})


def test_satisfied_columns_pass() -> None:
    for strategy, column in SATISFIED.items():
        check_required_strategy_settings(_config(strategy, **column))


@pytest.mark.parametrize("value", [None, "", {}, []])
def test_missing_means_absent_none_empty_string_or_empty_collection(value: Any) -> None:
    config = _config("hash", namespace=value)
    with pytest.raises(PlanCompileError) as exc:
        check_required_strategy_settings(config)
    assert exc.value.code == "hash_requires_namespace"


def test_nested_names_the_setting_that_is_missing() -> None:
    config = _config("nested", provider_config={"strategy": "redact", "target": ""})
    with pytest.raises(PlanCompileError) as exc:
        check_required_strategy_settings(config)
    assert exc.value.code == "nested_target_unset"
    config = _config("nested", provider_config={"target": "$.a"})
    with pytest.raises(PlanCompileError) as exc:
        check_required_strategy_settings(config)
    assert exc.value.code == "nested_strategy_unset"


@pytest.mark.parametrize(
    ("strategy", "column"),
    [
        ("shuffle", {}),
        ("shuffle", {"deterministic": False}),
        ("shuffle", {"deterministic": 1}),
        ("shuffle", {"deterministic": "true"}),
        ("code_set", {"provider_config": {"mode": "mask"}}),
        ("code_set", {}),
        ("code_set", {"provider_config": {"mode": 1}}),
    ],
)
def test_conditional_requirement_needs_the_exact_type_and_value(
    strategy: str, column: dict[str, Any]
) -> None:
    check_required_strategy_settings(_config(strategy, **column))


@pytest.mark.parametrize(
    ("strategy", "column", "code"),
    [
        ("shuffle", {"deterministic": True}, "shuffle_requires_namespace"),
        ("code_set", {"provider_config": {"mode": "gen"}}, "code_set_gen_requires_namespace"),
    ],
)
def test_conditional_requirement_applies_when_the_condition_holds(
    strategy: str, column: dict[str, Any], code: str
) -> None:
    with pytest.raises(PlanCompileError) as exc:
        check_required_strategy_settings(_config(strategy, **column))
    assert exc.value.code == code


def test_unrelated_strategies_and_odd_shapes_are_skipped() -> None:
    check_required_strategy_settings({})
    check_required_strategy_settings({"tables": "x"})
    check_required_strategy_settings({"tables": [1, {"columns": [None, {"strategy": ["hash"]}]}]})
    check_required_strategy_settings(_config("passthrough"))


def test_first_missing_setting_wins_in_config_order() -> None:
    config = {
        "tables": [
            {
                "name": "t",
                "columns": [
                    {"name": "a", "strategy": "hash"},
                    {"name": "b", "strategy": "fpe"},
                ],
            }
        ]
    }
    with pytest.raises(PlanCompileError) as exc:
        check_required_strategy_settings(config)
    assert exc.value.code == "hash_requires_namespace"


def test_descriptors_hold_fixed_registered_values_only() -> None:
    for strategy, requirements in REQUIREMENTS_BY_STRATEGY.items():
        for req in requirements:
            assert isinstance(req, StrategyRequirement)
            assert not re.search(r"[{}%]", req.message)
            plan_spec = reasons.find("plan", req.code)
            runtime_spec = reasons.find("runtime", req.code)
            assert plan_spec is not None and plan_spec.owner == "PlanCompileError", strategy
            assert runtime_spec is not None, strategy
    with pytest.raises(ValueError):
        StrategyRequirement(("a",), "x", "m", when_path=("b",))
    with pytest.raises(ValueError):
        StrategyRequirement((), "x", "m")


def test_the_refusal_does_not_echo_config_values() -> None:
    config = _config("hash", namespace="", provider_config={"note": "SECRET-VALUE"})
    with pytest.raises(PlanCompileError) as exc:
        check_required_strategy_settings(config)
    assert "SECRET-VALUE" not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__context__ is None


def test_catalog_refuses_an_unknown_mapping_key(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = dict(REQUIREMENTS_BY_STRATEGY)
    rows["no_such_strategy"] = rows["hash"]
    monkeypatch.setattr(_strategies, "REQUIREMENTS_BY_STRATEGY", rows)
    with pytest.raises(TypeError, match="unknown strategies"):
        _strategies.strategy_catalog()


def test_catalog_refuses_a_namespace_requirement_the_handler_does_not_declare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _strategies.SCALAR_HANDLERS["redact"]
    monkeypatch.setattr(
        _strategies, "requirements_for", lambda name: REQUIREMENTS_BY_STRATEGY["hash"]
    )
    with pytest.raises(TypeError, match="does not declare"):
        _strategies._catalog_row(handler)


def test_every_config_resolvable_runtime_guard_has_a_mapping_entry_or_a_reason() -> None:
    mapped = {req.code for reqs in REQUIREMENTS_BY_STRATEGY.values() for req in reqs}
    found: set[str] = set()
    for path in SRC.rglob("*.py"):
        if "reasons" in path.relative_to(SRC).parts:
            continue
        for symbol in GUARD_NAME.findall(path.read_text(encoding="utf-8")):
            found.add(getattr(reasons.runtime, symbol))
    assert found, "the scan found no runtime guards"
    assert found - mapped - set(EXCLUDED_GUARDS) == set()
    assert all(EXCLUDED_GUARDS.values())
    assert mapped - found == set()
