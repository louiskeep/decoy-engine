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
_EXCEPTION_MARKER = "<exception traceback>"

# (module path relative to decoy_engine, qualified function name, variable) -> (count, reason).
ALLOWLIST: dict[tuple[str, str, str], tuple[int, str]] = {
    # System errors whose text names a path or an OS condition, not data.
    ("disguises/loader.py", "load_disguises", "exc"): (1, "OSError reading a packaged file"),
    ("internal/logging.py", "_configure_logger", "exc"): (1, "OSError opening the log file"),
    ("internal/memory.py", "MemoryMonitor.monitor_memory_usage", "e"): (1, "psutil failure"),
    ("quarantine.py", "publish_staged_jsonl", "exc"): (1, "OSError removing a staging link"),
    # Package or environment configuration text, no customer data.
    ("storm/model_pack/loader.py", "ModelPackLoader.load_with_fallback", "exc"): (
        2,
        "model pack and environment config text",
    ),
    ("internal/faker_setup.py", "make_faker", "exc"): (1, "Faker locale error; locale is config"),
    # Deferred to the post-Phase F Observability program (decoy-platform ROADMAP): these can
    # echo file content or column values and should log the exception type only.
    ("internal/faker_setup.py", "_load_txt", "exc"): (1, "Observability follow-up"),
    ("internal/faker_setup.py", "_load_json", "exc"): (1, "Observability follow-up"),
    ("storm/profiler.py", "_run_storm_inner", "exc"): (1, "Observability follow-up"),
    ("transforms/base.py", "BaseMaskingStrategy._log_stats", "exc"): (1, "Observability follow-up"),
}


def _logger_aliases(tree: ast.AST) -> frozenset[str]:
    """Names bound to `getLogger(...)` in the module, whatever they are called."""
    getters = {"getLogger"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "logging":
            getters |= {a.asname or a.name for a in node.names if a.name == "getLogger"}
    aliases: set[str] = set()
    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(value, ast.Call):
            func = value.func
            called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if called in getters:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        aliases.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        aliases.add(target.attr)
    return frozenset(aliases)


def _is_logger_receiver(node: ast.expr, aliases: frozenset[str] = frozenset()) -> bool:
    if isinstance(node, ast.Call):
        func = node.func
        return isinstance(func, ast.Attribute) and func.attr == "getLogger"
    name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
    return name in aliases or name == "logging" or name.lower().lstrip("_") in {"log", "logger"}


def _is_logger_call(node: ast.Call, aliases: frozenset[str] = frozenset()) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in _LEVELS
        and _is_logger_receiver(func.value, aliases)
    )


def _root_name(value: ast.expr) -> str | None:
    """The variable an expression reads through attributes and subscripts; None if a call
    sits on the path (`type(exc).__name__` reads the type, not the exception)."""
    while isinstance(value, (ast.Attribute, ast.Subscript)):
        if isinstance(value, ast.Attribute) and value.attr == "__class__":
            return None  # exc.__class__.__name__ is the type, like type(exc).__name__
        value = value.value
    return value.id if isinstance(value, ast.Name) else None


def _risky_in(value: ast.expr, risky: frozenset[str]) -> list[str]:
    """Every risky name `value` exposes as text, directly or through formatting. All of them
    count, so adding a second risky value to an allowlisted message is still a new finding."""
    if isinstance(value, (ast.Name, ast.Attribute, ast.Subscript)):
        root = _root_name(value)
        if root is not None:
            return [root] if root in risky else []
        base: ast.expr = value
        while isinstance(base, (ast.Attribute, ast.Subscript)):
            base = base.value
        # `str(exc)[:200]` still exposes the text; `type(exc).__name__` stays exempt because
        # `type` is not a formatting call.
        return _risky_in(base, risky) if isinstance(base, ast.Call) else []
    children: list[ast.expr] = []
    if isinstance(value, ast.JoinedStr):
        children = [p.value for p in value.values if isinstance(p, ast.FormattedValue)]
    elif isinstance(value, ast.BinOp):
        children = [value.left, value.right]
    elif isinstance(value, ast.Starred):
        children = [value.value]
    elif isinstance(value, ast.BoolOp):
        children = list(value.values)
    elif isinstance(value, ast.IfExp):
        children = [value.body, value.orelse]
    elif isinstance(value, ast.Dict):
        # Logging renders a dict's keys as well as its values.
        children = [k for k in value.keys if k is not None] + list(value.values)
    elif isinstance(value, ast.DictComp):
        children = [value.key, value.value, *(g.iter for g in value.generators)]
    elif isinstance(value, (ast.GeneratorExp, ast.ListComp, ast.SetComp)):
        # The iterable counts too: conservative on purpose, since a false positive only costs
        # an allowlist entry while `", ".join(str(x) for x in row)` must not pass.
        children = [value.elt, *(g.iter for g in value.generators)]
    elif isinstance(value, (ast.Tuple, ast.List, ast.Set)):
        children = list(value.elts)
    elif isinstance(value, ast.Call):
        func = value.func
        if (isinstance(func, ast.Name) and func.id in {"str", "repr", "format"}) or (
            isinstance(func, ast.Attribute) and func.attr in {"format", "join"}
        ):
            children = [*value.args, *(k.value for k in value.keywords)]
        elif isinstance(func, ast.Attribute):
            # A method on a risky value (`row.items()`, `exc.strip()`) still exposes it.
            root = _root_name(func.value)
            return [root] if root in risky else []
    return [hit for child in children for hit in _risky_in(child, risky)]


def _risky_names_in_call(node: ast.Call, risky: frozenset[str]) -> list[str]:
    found = [hit for arg in node.args for hit in _risky_in(arg, risky)]
    found += [
        hit
        for keyword in node.keywords
        if keyword.arg in {"msg", "extra"}
        for hit in _risky_in(keyword.value, risky)
    ]
    func = node.func
    logs_traceback = isinstance(func, ast.Attribute) and func.attr == "exception"
    for keyword in node.keywords:
        if keyword.arg == "exc_info":
            falsy = isinstance(keyword.value, ast.Constant) and not keyword.value.value
            logs_traceback = not falsy
    if logs_traceback:
        found.append(_EXCEPTION_MARKER)
    return found


def _findings(source: str, module: str) -> dict[tuple[str, str, str], int]:
    tree = ast.parse(source)
    aliases = _logger_aliases(tree)
    found: dict[tuple[str, str, str], int] = {}

    def visit(node: ast.AST, qualname: str, risky: frozenset[str]) -> None:
        for child in ast.iter_child_nodes(node):
            name, scope_risky = qualname, risky
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if qualname == "<module>" else f"{qualname}.{child.name}"
            if isinstance(child, ast.ExceptHandler) and child.name:
                scope_risky = risky | {child.name}
            if isinstance(child, ast.Call) and _is_logger_call(child, aliases):
                for variable in _risky_names_in_call(child, scope_risky):
                    key = (module, name, variable)
                    found[key] = found.get(key, 0) + 1
            visit(child, name, scope_risky)

    visit(tree, "<module>", RISKY_NAMES)
    return found


def _engine_findings() -> dict[tuple[str, str, str], int]:
    found: dict[tuple[str, str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        found.update(_findings(path.read_text(encoding="utf-8"), module))
    return found


def test_no_new_log_call_interpolates_an_exception_value_or_expression() -> None:
    found = _engine_findings()
    new = sorted(
        (key, count) for key, count in found.items() if count > ALLOWLIST.get(key, (0, ""))[0]
    )
    assert not new, (
        "log calls interpolate a raw exception, data value or expression; log the location, "
        "counts and type(exc).__name__ instead (dev-rules observability-and-resilience.md): "
        f"{new}"
    )


def test_every_allowlisted_site_still_exists() -> None:
    found = _engine_findings()
    stale = sorted(
        (key, allowed)
        for key, (allowed, _reason) in ALLOWLIST.items()
        if found.get(key, 0) < allowed
    )
    assert not stale, f"allowlisted log sites were removed or reduced; lower the counts: {stale}"


_M = "m.py"
_TB = "<exception traceback>"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        # Message forms.
        ("def f():\n    log.warning(f'bad {exc}')\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    log.error(exc)\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    log.error(str(exc))\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    log.error('x %s' % exc)\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    log.error('x %s %s' % (a, value))\n", {(_M, "f", "value"): 1}),
        ("def f():\n    log.error('{}'.format(exc))\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    log.error('x ' + str(exc))\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    logger.error('x %s', value)\n", {(_M, "f", "value"): 1}),
        ("def f():\n    log.error(f'{exc.args[0]}')\n", {(_M, "f", "exc"): 1}),
        # Receivers.
        ("def f():\n    _LOG.warning(f'{exc}')\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    self.logger.info(f'x {e}')\n", {(_M, "f", "e"): 1}),
        ("def f():\n    logging.warning(f'{exc}')\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    logging.getLogger(__name__).debug(f'{exc}')\n", {(_M, "f", "exc"): 1}),
        # Tracebacks.
        ("def f():\n    log.exception('failed')\n", {(_M, "f", _TB): 1}),
        ("def f():\n    log.error('x', exc_info=True)\n", {(_M, "f", _TB): 1}),
        ("def f():\n    log.error('x', exc_info=False)\n", {}),
        # A name bound by `except ... as` is risky even when it is not a conventional name.
        (
            "def f():\n    try:\n        g()\n    except OSError as boom:\n"
            "        log.error('x %s', boom)\n",
            {(_M, "f", "boom"): 1},
        ),
        # Qualified keys and counts.
        ("class C:\n    def f(self):\n        log.error(exc)\n", {(_M, "C.f", "exc"): 1}),
        (
            "def f():\n    def g():\n        log.error(exc)\n    log.error(exc)\n",
            {(_M, "f.g", "exc"): 1, (_M, "f", "exc"): 1},
        ),
        ("def f():\n    log.error(exc)\n    log.warning(exc)\n", {(_M, "f", "exc"): 2}),
        ("def f():\n    log.error(', '.join(values))\n", {(_M, "f", "values"): 1}),
        ("def f():\n    log.error(', '.join(str(x) for x in row))\n", {(_M, "f", "row"): 1}),
        ("def f():\n    log.error('%(v)s' % {'v': exc})\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    log.error(msg=f'{exc}')\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    log.error(exc or 'x')\n", {(_M, "f", "exc"): 1}),
        ("def f():\n    log.error('%s', value if a else b)\n", {(_M, "f", "value"): 1}),
        ("def f():\n    log.error('%s %s', *values)\n", {(_M, "f", "values"): 1}),
        ("def f():\n    log.exception('x', exc_info=False)\n", {}),
        ("def f():\n    log.error('%s', exc.__class__.__name__)\n", {}),
        (
            "def f():\n    log.error(f'{exc}: {value}')\n",
            {(_M, "f", "exc"): 1, (_M, "f", "value"): 1},
        ),
        ("def f():\n    log.error({value: 'invalid'})\n", {(_M, "f", "value"): 1}),
        (
            "audit = logging.getLogger(__name__)\ndef f():\n    audit.error('%s', exc)\n",
            {(_M, "f", "exc"): 1},
        ),
        # Conservative: the iterable of a comprehension counts even if each element is sanitized.
        (
            "def f():\n    log.info('%s', [sanitize(x) for x in values])\n",
            {(_M, "f", "values"): 1},
        ),
        ("def f():\n    log.error('%s', str(exc)[:200])\n", {(_M, "f", "exc"): 1}),
        (
            "def f():\n    log.error('%s', {k: str(x) for k, x in row.items()})\n",
            {(_M, "f", "row"): 1},
        ),
        (
            "from logging import getLogger as gl\naudit = gl(__name__)\n"
            "def f():\n    audit.error('%s', exc)\n",
            {(_M, "f", "exc"): 1},
        ),
        # Safe forms.
        ("def f():\n    _log.warning('x %s', type(exc).__name__)\n", {}),
        ("def f():\n    _log.warning('x %s', len(values))\n", {}),
        ("def f():\n    print(f'{exc}')\n", {}),
    ],
)
def test_the_detector(source: str, expected: dict[tuple[str, str, str], int]) -> None:
    assert _findings(source, _M) == expected
