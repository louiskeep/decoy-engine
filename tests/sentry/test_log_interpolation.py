"""Engine log calls must not interpolate raw exceptions, data values or expressions.

Logs reach server logs that operators read. A logged exception from a parser, validator or
callback can echo its input, and a logged value or expression can carry personal data. Log the
location and counts instead: component, column or field name, operation, counts, and
`type(exc).__name__` (see dev-rules `observability-and-resilience.md`).

This is a ratchet. Every existing site is listed in `ALLOWLIST` with its reason, keyed by
(module, enclosing function, variable) so ordinary edits do not move it. A new site fails the
test; an allowlisted site that disappears must be removed from the list.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "decoy_engine"

# Variable names that hold exceptions, data values or user expressions by convention.
RISKY_NAMES = frozenset(
    {
        "exc",
        "e",
        "err",
        "error",
        "ex",
        "value",
        "v",
        "val",
        "values",
        "cell",
        "row",
        "expression",
        "expr",
        "formula",
        "text",
        "data",
        "sample",
        "raw",
        "record",
        "item",
        "line",
        "content",
    }
)
_LEVELS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
_LOGGER_NAMES = frozenset({"logger", "_log", "log", "_logger", "LOGGER", "LOG"})

# (module path relative to decoy_engine, enclosing function, variable) -> reason.
ALLOWLIST: dict[tuple[str, str, str], str] = {
    # System errors whose text names a path or an OS condition, not data.
    ("disguises/loader.py", "load_disguises", "exc"): "OSError reading a packaged file",
    ("internal/logging.py", "_configure_logger", "exc"): "OSError opening the log file",
    ("internal/memory.py", "monitor_memory_usage", "e"): "psutil failure, no data",
    ("quarantine.py", "publish_staged_jsonl", "exc"): "OSError removing a staging link",
    # Engine-authored error text.
    ("storm/model_pack/loader.py", "load_with_fallback", "exc"): "ModelPackLoadError text",
    ("internal/faker_setup.py", "make_faker", "exc"): "Faker locale error; the locale is config",
    # Deferred to the post-Phase F Observability program (decoy-platform ROADMAP): these can
    # echo file content or column values and should log the exception type only.
    ("internal/faker_setup.py", "_load_txt", "exc"): "Observability follow-up",
    ("internal/faker_setup.py", "_load_json", "exc"): "Observability follow-up",
    ("storm/profiler.py", "_run_storm_inner", "exc"): "Observability follow-up",
    ("transforms/base.py", "_log_stats", "exc"): "Observability follow-up",
}


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in _LEVELS:
        return False
    target = func.value
    name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
    return name in _LOGGER_NAMES


def _direct_risky(value: ast.expr) -> str | None:
    """The risky name when `value` interpolates it directly, or through str()/repr()."""
    if isinstance(value, ast.Name) and value.id in RISKY_NAMES:
        return value.id
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"str", "repr"}
        and value.args
    ):
        return _direct_risky(value.args[0])
    return None


def _risky_names_in_call(node: ast.Call) -> list[str]:
    found: list[str] = []
    for index, arg in enumerate(node.args):
        if index == 0:
            # The message: f-string placeholders that interpolate a risky name.
            for part in ast.walk(arg):
                if isinstance(part, ast.FormattedValue):
                    name = _direct_risky(part.value)
                    if name:
                        found.append(name)
        else:
            # %-style arguments passed straight through.
            name = _direct_risky(arg)
            if name:
                found.append(name)
    return found


def _findings(source: str, module: str) -> set[tuple[str, str, str]]:
    tree = ast.parse(source)
    found: set[tuple[str, str, str]] = set()

    def visit(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = function
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            if isinstance(child, ast.Call) and _is_logger_call(child):
                for variable in _risky_names_in_call(child):
                    found.add((module, name, variable))
            visit(child, name)

    visit(tree, "<module>")
    return found


def _engine_findings() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        found |= _findings(path.read_text(encoding="utf-8"), module)
    return found


def test_no_new_log_call_interpolates_an_exception_value_or_expression() -> None:
    new = sorted(_engine_findings() - ALLOWLIST.keys())
    assert not new, (
        "log calls interpolate a raw exception, data value or expression; log the location, "
        "counts and type(exc).__name__ instead (dev-rules observability-and-resilience.md): "
        f"{new}"
    )


def test_every_allowlisted_site_still_exists() -> None:
    stale = sorted(ALLOWLIST.keys() - _engine_findings())
    assert not stale, f"allowlisted log sites no longer exist; remove them: {stale}"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def f():\n    log.warning(f'bad {exc}')\n", {("m.py", "f", "exc")}),
        ("def f():\n    logger.error('x %s', value)\n", {("m.py", "f", "value")}),
        ("def f():\n    self.logger.info(f'x {str(e)}')\n", {("m.py", "f", "e")}),
        ("def f():\n    _log.warning('x %s', type(exc).__name__)\n", set()),
        ("def f():\n    _log.warning('x %s', len(values))\n", set()),
        ("def f():\n    print(f'{exc}')\n", set()),
    ],
    ids=["fstring", "percent", "str_call", "type_name", "len", "not_a_logger"],
)
def test_the_detector_flags_direct_interpolation_only(
    source: str, expected: set[tuple[str, str, str]]
) -> None:
    assert _findings(source, "m.py") == expected
