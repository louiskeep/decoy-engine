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
from pathlib import Path, PurePosixPath

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
        # Geographic quasi-identifiers taken straight from rows (HIPAA Safe Harbor zip3/zip5).
        # Exact names, not a `zip*` wildcard, so unrelated identifiers stay clean.
        "z3",
        "z5",
        "zip3",
        "zip5",
        "zipcode",
        "prefix",
    }
)
# Names that hold child-process output. Only the error sinks below treat them as risky, because
# `stderr`/`stdout` are routine, harmless names in code that does not build a user-facing error.
SINK_RISKY = RISKY_NAMES | {"stderr", "stdout"}
# Functions whose return value IS the user-visible error text. Their bodies are held to the
# same standard as a sink: a builder that returns the exception message leaks through every
# caller, so the check sits at the definition rather than at each call.
ERROR_TEXT_BUILDERS = frozenset(
    {"_error_text", "exception_error_text", "commit_error_text", "abnormal_exit_text"}
)
_LEVELS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
_EXCEPTION_MARKER = "<exception traceback>"
_COUNT_METHODS = frozenset({"count", "nunique", "sum", "size", "__len__"})

# (module path relative to decoy_engine, qualified function name, variable) -> (count, reason).
ALLOWLIST: dict[tuple[str, str, str], tuple[int, str]] = {
    # System errors whose text names a path or an OS condition, not data.
    ("disguises/loader.py", "_load_from", "exc"): (1, "OSError reading a packaged file"),
    ("internal/logging.py", "_configure_logger", "exc"): (1, "OSError opening the log file"),
    ("internal/memory.py", "MemoryMonitor.monitor_memory_usage", "e"): (1, "psutil failure"),
    # Package or environment configuration text, no customer data.
    ("profiling/model_pack/loader.py", "ModelPackLoader.load_with_fallback", "exc"): (
        2,
        "model pack and environment config text",
    ),
    ("internal/faker_setup.py", "make_faker", "exc"): (1, "Faker locale error; locale is config"),
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
        # A sliced f-string (`f"{exc}"[:500]`) exposes the same text as the f-string itself.
        return _risky_in(base, risky) if not isinstance(base, ast.Name) else []
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
        elif isinstance(func, ast.Name) and func.id == "scrub_error_text":
            # Returns the message with only the Arrow cell pattern removed: still the message.
            children = list(value.args)
        elif isinstance(func, ast.Attribute):
            if func.attr in _COUNT_METHODS:
                return []  # `row.count()`, `values.nunique()` log a number, not the data
            # A method on a risky value (`row.items()`, `str(exc).strip()`) still exposes it.
            root = _root_name(func.value)
            if root is not None:
                return [root] if root in risky else []
            return _risky_in(func.value, risky)
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


def _own_nodes(scope: ast.AST) -> list[ast.AST]:
    """Nodes that belong to `scope` itself: nested defs and classes appear but are not entered."""
    own: list[ast.AST] = []
    pending = list(ast.iter_child_nodes(scope))
    while pending:
        node = pending.pop()
        own.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))
    return own


def _tainted_names(own: list[ast.AST], risky: frozenset[str]) -> frozenset[str]:
    """Names assigned from a risky expression, in source order, one pass.

    Bounded on purpose: a single scope, plain `name = expr` / `name += expr` assignments, no
    loops to a fixpoint, no cross-function flow. It is enough to follow
    `stderr_tail = stderr.strip()...` into the `error=` that uses `stderr_tail`, which an
    expression-only check cannot see because `stderr_tail` is not a risky name.
    """
    tainted: set[str] = set()
    assigns = sorted(
        (n for n in own if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign))),
        key=lambda n: (n.lineno, n.col_offset),
    )
    for node in assigns:
        if node.value is None:
            continue
        if _risky_in(node.value, risky | tainted):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            tainted |= {t.id for t in targets if isinstance(t, ast.Name)}
    return frozenset(tainted)


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _is_envelope_module(module: str) -> bool:
    """The isolated-run modules, whose `{"error": ...}` dict is the worker's result envelope.

    Other `"error"` dict keys (a warning detail, an API payload) are a different sink with
    different consumers; widening this to every module is a separate decision.
    """
    return PurePosixPath(module).name.startswith("_isolated_")


def _sink_findings(source: str, module: str) -> dict[tuple[str, str, str], int]:
    """Risky values reaching a user-visible error, not a log call.

    Named sinks only: `emit_step(error_msg=...)`, `IsolatedRunResult(error=...)`, a dict entry
    keyed `"error"` in an isolated-run module (the worker envelope), and the return of an `ERROR_TEXT_BUILDERS` function.
    The variable in each key is `<sink>:<name>` so a sink finding never collides with a log one.
    """
    found: dict[tuple[str, str, str], int] = {}

    def add(qualname: str, sink: str, hits: list[str]) -> None:
        for hit in hits:
            key = (module, qualname, f"{sink}:{hit}")
            found[key] = found.get(key, 0) + 1

    def scan(scope: ast.AST, qualname: str) -> None:
        own = _own_nodes(scope)
        risky = SINK_RISKY | {
            n.name for n in own if isinstance(n, ast.ExceptHandler) and n.name is not None
        }
        risky = risky | _tainted_names(own, risky)
        in_builder = (
            isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
            and scope.name in ERROR_TEXT_BUILDERS
        )
        for node in own:
            if isinstance(node, ast.Call):
                sink = {"emit_step": "error_msg", "IsolatedRunResult": "error"}.get(
                    _call_name(node)
                )
                for keyword in node.keywords:
                    if sink is not None and keyword.arg == sink:
                        add(qualname, f"{_call_name(node)}.{sink}", _risky_in(keyword.value, risky))
            elif isinstance(node, ast.Dict) and _is_envelope_module(module):
                for key_node, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key_node, ast.Constant) and key_node.value == "error":
                        add(qualname, "envelope.error", _risky_in(value, risky))
            elif in_builder and isinstance(node, ast.Return) and node.value is not None:
                add(qualname, "builder", _risky_in(node.value, risky))
        for node in own:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scan(node, node.name if qualname == "<module>" else f"{qualname}.{node.name}")

    scan(ast.parse(source), "<module>")
    return found


def _engine_findings() -> dict[tuple[str, str, str], int]:
    found: dict[tuple[str, str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        text = path.read_text(encoding="utf-8")
        found.update(_findings(text, module))
        found.update(_sink_findings(text, module))
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
        ("def f():\n    log.warning('%s', str(exc).strip())\n", {(_M, "f", "exc"): 1}),
        (
            "def f():\n    log.warning('%s', str(exc).replace('x', ' ')[:200])\n",
            {(_M, "f", "exc"): 1},
        ),
        ("def f():\n    log.info('%s', row.count())\n", {}),
        # Safe forms.
        ("def f():\n    _log.warning('x %s', type(exc).__name__)\n", {}),
        ("def f():\n    _log.warning('x %s', len(values))\n", {}),
        ("def f():\n    print(f'{exc}')\n", {}),
    ],
)
def test_the_detector(source: str, expected: dict[tuple[str, str, str], int]) -> None:
    assert _findings(source, _M) == expected


# --- error sinks ---------------------------------------------------------------------------
# The fixtures below are the pre-OBS-1b engine code, verbatim where it matters: each leaked a
# raw message or child output into the user-visible `IsolatedRunResult.error` and passed the
# log-only sentry, because the exposed variables are not risky-named and the sinks are not log
# calls.
_ISO = "_isolated_run.py"

_PRE_FIX_TAIL_ERROR = """
def _run_isolated(stderr, stdout, proc, signal_name, evidence):
    stderr_tail = stderr.strip().splitlines()[-1][:300] if stderr.strip() else ""
    stdout_tail = stdout.strip().splitlines()[-1][:300] if stdout.strip() else ""
    return IsolatedRunResult(
        outcome="crashed",
        error=(
            f"child terminated abnormally (returncode={proc.returncode}"
            f"{f', signal={signal_name}' if signal_name else ''}); "
            f"stderr tail: {stderr_tail!r}"
            f"{f'; stdout tail: {stdout_tail!r}' if stdout_tail else ''}"
            f"{error_suffix(evidence) if evidence else ''}"
        ),
    )
"""

_PRE_FIX_BUILDER = """
def exception_error_text(exc):
    return f"{type(exc).__name__}: {scrub_error_text(str(exc))}"
"""


def test_sink_flags_the_pre_fix_tail_expression_through_assignments() -> None:
    assert _sink_findings(_PRE_FIX_TAIL_ERROR, _ISO) == {
        (_ISO, "_run_isolated", "IsolatedRunResult.error:stderr_tail"): 1,
        (_ISO, "_run_isolated", "IsolatedRunResult.error:stdout_tail"): 1,
    }


def test_sink_expression_check_alone_would_miss_the_pre_fix_tail() -> None:
    # Pins WHY assignment tracking exists: the expression names only `stderr_tail`/`stdout_tail`,
    # which are not risky by name, so checking the `error=` expression alone sees nothing.
    expr = ast.parse("f'{stderr_tail!r}'", mode="eval").body
    assert _risky_in(expr, SINK_RISKY) == []


def test_sink_flags_the_pre_fix_error_text_builder() -> None:
    assert _sink_findings(_PRE_FIX_BUILDER, "_isolated_common.py") == {
        ("_isolated_common.py", "exception_error_text", "builder:exc"): 1
    }


def test_sink_flags_a_builder_that_returns_the_message_directly() -> None:
    src = "def _error_text(exc):\n    return f'{type(exc).__name__}: {exc}'[:500]\n"
    assert _sink_findings(src, "_isolated_worker.py") == {
        ("_isolated_worker.py", "_error_text", "builder:exc"): 1
    }


def test_sink_flags_the_envelope_error_entry() -> None:
    src = "def _run():\n    try:\n        pass\n    except BaseException as exc:\n        return {'error': str(exc)}\n"
    assert _sink_findings(src, "_isolated_worker.py") == {
        ("_isolated_worker.py", "_run", "envelope.error:exc"): 1
    }
    # The same dict in a module that is not an isolated-run one is a different sink.
    assert _sink_findings(src, "other.py") == {}


def test_sink_flags_the_scan_emit_step_message() -> None:
    src = (
        "def scan(logger):\n    try:\n        pass\n    except Exception as exc:\n"
        "        emit_step(logger, 's', status='error', error_class=type(exc).__name__,\n"
        "                  error_msg=str(exc))\n"
    )
    assert _sink_findings(src, "profiling/profiler.py") == {
        ("profiling/profiler.py", "scan", "emit_step.error_msg:exc"): 1
    }


def test_sink_flags_the_commit_error_branch() -> None:
    src = (
        "def f():\n    try:\n        pass\n    except CommitError as exc:\n"
        "        return IsolatedRunResult(outcome='crashed', error=str(exc))\n"
    )
    assert _sink_findings(src, _ISO) == {(_ISO, "f", "IsolatedRunResult.error:exc"): 1}


@pytest.mark.parametrize(
    "src",
    [
        # The post-fix shapes: summaries built by the safe helpers, type names, counts.
        "def f(proc, stderr, stdout, sig):\n"
        "    return IsolatedRunResult(error=abnormal_exit_text(proc.returncode, sig, stderr, stdout)"
        " + (error_suffix(ev) if ev else ''))\n",
        "def f(exc):\n    return IsolatedRunResult(error=commit_error_text(exc))\n",
        "def f(exc):\n    return IsolatedRunResult(error=exception_error_text(exc))\n",
        "def f(exc):\n    emit_step(l, 's', error_msg=type(exc).__name__)\n",
        "def f(stderr):\n    n = len(stderr)\n    return IsolatedRunResult(error=f'{n} bytes')\n",
        "def exception_error_text(exc):\n    head = type(exc).__name__\n    return head\n",
    ],
)
def test_sink_accepts_the_safe_shapes(src: str) -> None:
    assert _sink_findings(src, _ISO) == {}


def test_geo_names_are_exact_matches_not_a_wildcard() -> None:
    for name in ("z3", "z5", "zip3", "zip5", "zipcode", "prefix"):
        assert name in RISKY_NAMES
    assert _findings("def f():\n    log.debug('p %r', z3)\n", _M) == {(_M, "f", "z3"): 1}
    # Unrelated identifiers that merely contain the letters are not flagged.
    assert _findings("def f():\n    log.debug('p %s', zip_count)\n", _M) == {}
    assert _findings("def f():\n    log.debug('p %s', prefix_len)\n", _M) == {}


def test_pre_fix_geo_log_line_is_flagged() -> None:
    src = (
        "def _cascade_one_row(z3, restricted):\n"
        "    if z3 in restricted:\n"
        "        _LOG.debug('geo_generalize: zip3 prefix %r is HHS-restricted '\n"
        "                   '(45 CFR 164.514(b)(2)(i)(B)); skipping zip3 level.', z3)\n"
    )
    assert _findings(src, "transforms/geo_generalize.py") == {
        ("transforms/geo_generalize.py", "_cascade_one_row", "z3"): 1
    }
