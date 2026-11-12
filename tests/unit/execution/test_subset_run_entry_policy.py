"""Sentry: every public entry that takes a pipeline config sits in exactly one subset kind.

A new `run_*`, `compile_*`, `estimate_*` or `probe_*` entry (or `validate_plan`) whose first
parameter is `config` must be classified here, or a `subset:` block is silently ignored again.
Named limit: other prefixes are not walked, and a deliberately evasive name is not proved.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Callable

import decoy_engine
from decoy_engine import execution, plan
from decoy_engine.execution import native

PREFIXES = ("run_", "compile_", "estimate_", "probe_")

# Applies the block itself.
APPLIES = {"run_pipeline"}
# Hands the config to an entry that applies it, before any read or spawn.
DELEGATES = {
    "run_pipeline_isolated": "the child or in-process run_pipeline applies",
    "run_job_with_governor": "every rung runs through run_pipeline_isolated",
}
# Calls `refuse_unapplied_subset` in its own body.
REFUSES = {
    "run_mask_pipeline_chunked",
    "run_mask_chunked",
    "run_native_or_oracle_chunked",
    "compile_plan",
    "probe_peak_bytes",
}
# Splits the block off and checks the rest.
SPLITS = {"validate_plan"}
# Reads the config only; neither a source nor a plan is touched.
CONFIG_ONLY = {"run_config_only_checks"}
# Answers "not applicable" for a subset config before any read.
NOT_APPLICABLE = {"estimate_job_capacity", "estimate_job"}


def _public_callables() -> dict[str, Callable[..., object]]:
    found: dict[str, Callable[..., object]] = {}
    for module in (decoy_engine, execution, native, plan):
        for name in getattr(module, "__all__", ()):
            obj = getattr(module, name, None)
            if callable(obj) and (name.startswith(PREFIXES) or name == "validate_plan"):
                found[name] = obj
    return found


def _calls(fn: Callable[..., object], callee: str) -> bool:
    source = textwrap.dedent(inspect.getsource(inspect.unwrap(fn)))
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == callee
        for n in ast.walk(ast.parse(source))
    )


def test_every_refusing_entry_calls_the_guard() -> None:
    entries = _public_callables()
    for name in sorted(REFUSES):
        assert name in entries, f"{name} is no longer exported; update REFUSES"
        assert _calls(entries[name], "refuse_unapplied_subset"), name


def test_splitting_and_applying_entries_call_the_split() -> None:
    entries = _public_callables()
    from decoy_engine.execution.pipeline import _subset_stage

    assert _calls(entries["run_pipeline"], "resolve_subset")
    assert _calls(_subset_stage.resolve_subset, "split_subset_config")
    assert _calls(entries["validate_plan"], "split_subset_config")


def unclassified(entries: dict[str, Callable[..., object]]) -> list[str]:
    """Entries whose first parameter is a config and that sit in no subset kind."""
    known = APPLIES | set(DELEGATES) | REFUSES | SPLITS | CONFIG_ONLY | NOT_APPLICABLE
    missing = []
    for name, fn in sorted(entries.items()):
        params = list(inspect.signature(fn).parameters)
        if params and params[0] in ("config", "config_dump") and name not in known:
            missing.append(name)
    return missing


def test_no_public_entry_is_unclassified() -> None:
    assert unclassified(_public_callables()) == [], (
        "a public entry takes a config; classify it in this file "
        "(APPLIES, DELEGATES, REFUSES, SPLITS, CONFIG_ONLY or NOT_APPLICABLE)"
    )


def test_the_check_fails_on_a_planted_unclassified_entry() -> None:
    def run_x(config: dict[str, object]) -> None: ...

    def estimate_y(config_dump: dict[str, object]) -> None: ...

    def run_other(rows: int) -> None: ...

    planted = {**_public_callables(), "run_x": run_x, "estimate_y": estimate_y}
    planted["run_other"] = run_other
    assert unclassified(planted) == ["estimate_y", "run_x"]
