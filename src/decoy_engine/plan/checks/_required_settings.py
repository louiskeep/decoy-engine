"""Required strategy settings, declared once and checked before any executor runs.

Each descriptor names one setting a strategy cannot run without. ``check_required_strategy_settings``
walks the config and refuses a column that leaves one out, so ``decoy validate`` and ``decoy run``
(``run_config_only_checks`` and ``compile_plan``) fail with the same code the executor's own guard
raises, instead of failing late. The executor guards stay as defense in depth.

The mapping lives in this lower layer so the strategy catalog can project the same descriptors
without the plan layer importing ``execution``. Descriptors hold a registered code and a fixed
message only: no submitted value and no callable predicate, so a refusal cannot echo config data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from decoy_engine import reasons
from decoy_engine.plan._errors import PlanCompileError

__all__ = [
    "REQUIREMENTS_BY_STRATEGY",
    "StrategyRequirement",
    "check_required_strategy_settings",
    "requirements_for",
]


@dataclass(frozen=True)
class StrategyRequirement:
    """One setting a strategy needs, as a path from a column entry.

    ``when_path`` and ``when_equals`` make the requirement conditional: it applies only when the
    value at ``when_path`` has exactly the type and value of ``when_equals``, so ``1`` never
    activates a boolean condition.
    """

    path: tuple[str, ...]
    code: str
    message: str
    when_path: tuple[str, ...] | None = None
    when_equals: bool | str | None = None

    def __post_init__(self) -> None:
        if not self.path or not self.message:
            raise ValueError("a strategy requirement needs a path and a message")
        if (self.when_path is None) != (self.when_equals is None):
            raise ValueError("when_path and when_equals are set together")


def _namespace(strategy: str, what: str | None = None, **when: Any) -> StrategyRequirement:
    code = getattr(reasons.plan, f"{strategy.upper()}_REQUIRES_NAMESPACE")
    return StrategyRequirement(
        ("namespace",), code, f"{what or strategy} needs a namespace; set `namespace`.", **when
    )


_PROVIDER = ("provider_config",)

REQUIREMENTS_BY_STRATEGY: Mapping[str, tuple[StrategyRequirement, ...]] = MappingProxyType(
    {
        "hash": (_namespace("hash"),),
        "fpe": (_namespace("fpe"),),
        "date_shift": (_namespace("date_shift"),),
        "bucket_perturb": (_namespace("bucket_perturb"),),
        "categorical": (_namespace("categorical"),),
        "tokenize": (_namespace("tokenize"),),
        "shuffle": (_namespace("shuffle", when_path=("deterministic",), when_equals=True),),
        "code_set": (
            _namespace(
                "code_set_gen",
                "code_set in gen mode",
                when_path=(*_PROVIDER, "mode"),
                when_equals="gen",
            ),
        ),
        "nested": (
            StrategyRequirement(
                (*_PROVIDER, "strategy"),
                reasons.plan.NESTED_STRATEGY_UNSET,
                "nested needs a child strategy; set `provider_config.strategy`.",
            ),
            StrategyRequirement(
                (*_PROVIDER, "target"),
                reasons.plan.NESTED_TARGET_UNSET,
                "nested needs a target path; set `provider_config.target`.",
            ),
        ),
    }
)


def requirements_for(strategy: str) -> tuple[StrategyRequirement, ...]:
    """The requirements of ``strategy``; empty when it declares none."""
    return REQUIREMENTS_BY_STRATEGY.get(strategy, ())


def _lookup(entry: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = entry
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return node


def _missing(value: Any) -> bool:
    if value is None or value == "":
        return True
    return isinstance(value, (list, tuple, set, frozenset, Mapping)) and len(value) == 0


def _applies(req: StrategyRequirement, entry: Mapping[str, Any]) -> bool:
    if req.when_path is None:
        return True
    actual = _lookup(entry, req.when_path)
    return type(actual) is type(req.when_equals) and actual == req.when_equals


def check_required_strategy_settings(config: dict[str, Any]) -> None:
    """Refuse the first column that leaves out a setting its strategy requires.

    Config-only, so it runs in both compile entry points. Tables and columns are walked in config
    order and each strategy's descriptors in declaration order.

    Raises:
        PlanCompileError: the code of the missing setting's descriptor.
    """
    tables = config.get("tables")
    for table in tables if isinstance(tables, list) else []:
        if not isinstance(table, dict):
            continue
        for col in table.get("columns") or []:
            strategy = col.get("strategy") if isinstance(col, dict) else None
            if not isinstance(strategy, str):
                continue
            for req in requirements_for(strategy):
                if _applies(req, col) and _missing(_lookup(col, req.path)):
                    raise PlanCompileError(
                        code=req.code,
                        path=(
                            f"tables.{table.get('name', '?')}.columns.{col.get('name', '?')}."
                            + ".".join(req.path)
                        ),
                        message=(
                            f"column {col.get('name', '?')!r} in table "
                            f"{table.get('name', '?')!r}: {req.message}"
                        ),
                    )
