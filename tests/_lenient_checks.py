"""The compile-time generate checks (GEN-STRICT E2) a runtime-backstop test may lift.

Some fixtures feed the generators configs that the plan check now refuses, to pin what a run
does with them if one gets past the plan. Every place that lifts the checks (the
``lenient_generate_checks`` fixture, the FR0 refreeze script, fresh child processes) takes the
targets from here, so a new strict check is added once.

The relationship parent-key rule stays on while the rest is lifted: a frozen fixture whose
generated parent key repeats would pin output the plan can no longer produce.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest import mock

LENIENT_CHECK_TARGETS = (
    "decoy_engine.plan._compile.check_generate_columns",
    "decoy_engine.plan.checks.check_faker_default_locale",
)


def _keep_parent_rule(config: dict[str, Any], **_: Any) -> None:
    from decoy_engine.plan.checks._relationship_keys import check_generated_parent_keys

    check_generated_parent_keys(config)


def lifted_check(target: str) -> Callable[..., None]:
    """What replaces ``target`` while the checks are lifted."""
    if target.endswith(".check_generate_columns"):
        return _keep_parent_rule
    return lambda *_a, **_k: None


@contextmanager
def lenient_generate_checks_lifted() -> Iterator[None]:
    """Lift every target for the block, for code that has no pytest monkeypatch."""
    with ExitStack() as stack:
        for target in LENIENT_CHECK_TARGETS:
            stack.enter_context(mock.patch(target, lifted_check(target)))
        yield
