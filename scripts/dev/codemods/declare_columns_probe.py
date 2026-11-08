"""Pytest plugin that records which fixture columns a run route would call undeclared.

    DECOY_DECLARE_PROBE=out.jsonl PYTHONPATH=scripts/dev/codemods \\
        python -m pytest -p declare_columns_probe PATHS

It swaps the module-level `check_columns_declared` that `prepare_declared_sources` and the
capability methods resolve for one that records every finding and never raises, so a test
that would be refused still runs to its own assertions. With pytest-xdist each worker writes
`OUT.<worker>`; `config_explicit.py declare-columns --record OUT` reads OUT and every `OUT.*`. The rule itself stays in
`declaration_findings`. Output holds fixture column names only: a scratch file, never committed.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
from typing import Any

import pytest

from decoy_engine.profile import (
    check_columns_declared,
    declaration_findings,
    declared_columns,
    prepare_declared_sources,
)

_OUT = os.environ.get("DECOY_DECLARE_PROBE")
_GLOBALS = inspect.unwrap(prepare_declared_sources).__globals__
_ORIGINAL = check_columns_declared
_ROWS: dict[tuple[Any, ...], dict[str, Any]] = {}
_CURRENT: list[str] = []


def _test_frame() -> str:
    """The innermost frame whose file sits under a `tests` directory, as `path:line`."""
    frame = sys._getframe(2)
    while frame is not None:
        parts = frame.f_code.co_filename.replace("\\", "/").split("/")
        if "tests" in parts:
            rel = os.path.relpath(frame.f_code.co_filename)
            return f"{rel}:{frame.f_lineno}"
        frame = frame.f_back  # type: ignore[assignment]
    return "?"


def _record(config: Any, inputs: Any) -> None:
    node = _CURRENT[-1] if _CURRENT else "?"
    for finding in declaration_findings(config, inputs):
        declared = tuple(declared_columns(config, finding.table))
        key = (node, finding.table, finding.kind, frozenset(declared), _test_frame())
        row = _ROWS.setdefault(
            key,
            {
                "node": node,
                "table": finding.table,
                "kind": finding.kind,
                "undeclared": [],
                "declared": list(declared),
                "frame": key[4],
            },
        )
        row["undeclared"] += [n for n in finding.undeclared if n not in row["undeclared"]]


def _recording(config: Any, inputs: Any) -> None:
    _record(config, inputs)


def pytest_sessionstart(session: pytest.Session) -> None:
    if _OUT:
        _GLOBALS["check_columns_declared"] = _recording


def pytest_runtest_setup(item: pytest.Item) -> None:
    _CURRENT.append(item.nodeid)


def pytest_runtest_teardown(item: pytest.Item) -> None:
    if _CURRENT:
        _CURRENT.pop()


def _output_path(session: pytest.Session) -> str | None:
    """Where this process writes. Under xdist each worker owns `OUT.<worker>` (the codemod
    merges them) and the controller, which records nothing, must not truncate the base file."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        return f"{_OUT}.{worker}"
    if getattr(session.config.option, "dist", "no") != "no":
        return None
    return _OUT


def pytest_sessionfinish(session: pytest.Session) -> None:
    if not _OUT:
        return
    _GLOBALS["check_columns_declared"] = _ORIGINAL
    target = _output_path(session)
    if target is None:
        return
    with open(target, "w", encoding="utf-8") as handle:
        for row in _ROWS.values():
            handle.write(json.dumps(row) + "\n")
