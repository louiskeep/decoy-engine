"""The compile-time generate checks (GEN-STRICT E2) a runtime-backstop test may lift.

Some fixtures feed the generators configs that the plan check now refuses, to pin what a run
does with them if one gets past the plan. Every place that lifts the checks (the
``lenient_generate_checks`` fixture, the FR0 refreeze script, fresh child processes) takes the
targets from here, so a new strict check is added once.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from unittest import mock

LENIENT_CHECK_TARGETS = (
    "decoy_engine.plan._compile.check_generate_columns",
    "decoy_engine.plan.checks.check_faker_default_locale",
)


@contextmanager
def lenient_generate_checks_lifted() -> Iterator[None]:
    """Lift every target for the block, for code that has no pytest monkeypatch."""
    with ExitStack() as stack:
        for target in LENIENT_CHECK_TARGETS:
            stack.enter_context(mock.patch(target, return_value=None))
        yield
