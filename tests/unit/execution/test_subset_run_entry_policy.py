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
    assert _calls(entries["run_pipeline"], "split_subset_config")
    assert _calls(entries["validate_plan"], "split_subset_config")


def test_no_public_entry_is_unclassified() -> None:
    known = APPLIES | set(DELEGATES) | REFUSES | SPLITS | CONFIG_ONLY | NOT_APPLICABLE
    for name, fn in sorted(_public_callables().items()):
        params = list(inspect.signature(fn).parameters)
        if not params or params[0] not in ("config", "config_dump"):
            continue
        assert name in known, (
            f"public entry {name} takes a config; classify it in this file "
            "(APPLIES, DELEGATES, REFUSES, SPLITS, CONFIG_ONLY or NOT_APPLICABLE)"
        )
