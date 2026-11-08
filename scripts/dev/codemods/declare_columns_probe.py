"""Pytest plugin that records which fixture columns a run route would call undeclared.

    DECOY_DECLARE_PROBE=out.jsonl PYTHONPATH=scripts/dev/codemods \\
        python -m pytest -p declare_columns_probe PATHS

It swaps the module-level `check_columns_declared` that `prepare_declared_sources` and the
capability methods resolve for one that records every finding and never raises, so a test
that would be refused still runs to its own assertions. With pytest-xdist each worker writes
`OUT.<run>.<worker>` and OUT holds a manifest naming the run; `config_explicit.py
declare-columns --record OUT` reads only that run's files. The rule itself stays in
`declaration_findings`. Output holds fixture column names only: a scratch file, never committed.
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import uuid
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
_RUN_ENV = "DECOY_DECLARE_PROBE_RUN"


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


def _output_path() -> str:
    """Where this process writes. Under xdist each worker owns `OUT.<run>.<worker>`, where
    `<run>` is minted once by the controller before workers start; the controller records
    nothing and writes only a `{"run": <run>}` manifest to OUT, so files of earlier runs under
    the same OUT are never read."""
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        return f"{_OUT}.{os.environ[_RUN_ENV]}.{worker}"
    return _OUT


def pytest_configure(config: pytest.Config) -> None:
    if _OUT and not os.environ.get("PYTEST_XDIST_WORKER"):
        os.environ[_RUN_ENV] = uuid.uuid4().hex[:12]


def pytest_sessionfinish(session: pytest.Session) -> None:
    if not _OUT:
        return
    _GLOBALS["check_columns_declared"] = _ORIGINAL
    target = _output_path()
    controller = not os.environ.get("PYTEST_XDIST_WORKER")
    manifest = controller and getattr(session.config.option, "dist", "no") != "no"
    with open(target, "w", encoding="utf-8") as handle:
        if manifest:
            handle.write(json.dumps({"run": os.environ[_RUN_ENV]}) + "\n")
            return
        for row in _ROWS.values():
            handle.write(json.dumps(row) + "\n")
