"""Config validation is value-free by construction: routes and validator messages.

Two rules, both AST scans over ``src/decoy_engine``:

1. Route. A Pydantic ``ValidationError`` for a public config model carries the submitted values
   in ``errors()``, ``json()`` and ``str()``. Only ``parse_pipeline_config`` may validate a
   reachable public model, and only ``safe_output/_validation.py`` may read a Pydantic
   ``ValidationError``. ``decoy_engine.errors.ValidationError`` is a different class and is fine.
2. Validator messages. A ``raise ValueError(<message>)`` in a module that defines a reachable
   public model must not interpolate a data value. The converter trusts validator text only for
   ``ConfigRuleError``, so each message is classified (the risky-name policy is
   ``test_log_interpolation``'s) as fixed, name-only, value-bearing or unclassified. A call result
   or opaque local is unclassified unless it comes straight from a producer function whose
   returns are classified and folded into the digest. Value-bearing, unclassified and plain
   ``ValueError`` sites must be zero. Each ``ConfigRuleError`` message is pinned by
   ``(path, qualified function, normalized AST digest)`` in
   ``baselines/validation-name-messages.json``, so editing one needs a reviewed baseline change.
   ``PydanticCustomError`` templates are never shown (the converter prints a registered reason's
   summary instead), so they are not scanned.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import inspect
import json
import typing
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from pydantic import BaseModel

import decoy_engine
from tests.sentry.test_log_interpolation import RISKY_NAMES, _risky_in

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "decoy_engine"
BASELINE = ROOT / "tests" / "sentry" / "baselines" / "validation-name-messages.json"

_PYDANTIC_MODULES = frozenset({"pydantic", "pydantic_core", "pydantic_core._pydantic_core"})
_ROUTE_FILES = frozenset({"config/_parse.py"})  # the only validate site for a public model
_HANDLER_FILES = frozenset({"safe_output/_validation.py"})  # the only reader of the error

# (path under decoy_engine, qualified function) -> reason. Internal re-validation of a dict the
# public route already accepted: it cannot carry an unchecked submitted value.
ROUTE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("execution/_transforms_table.py", "_transform_ops"): (
        "re-validates a table entry of a config dict that parse_pipeline_config already accepted"
    ),
    ("profile/_fixed_width_reader.py", "_resolve_layout"): (
        "keeps only exc.error_count() and raises outside the handler; the layout is schema shape"
    ),
    ("profiling/scan_set.py", "_table_core"): (
        "builds a write-side core from the engine's own registry ReasonRef, never a submitted value"
    ),
    ("profiling/scan_set.py", "scan_set_to_dict"): (
        "builds a write-side core from engine-computed relationship counts, never a submitted value"
    ),
}

# Roots of an interpolated expression that name structure (the model, a table, a column, a
# source, a config key), not a data value.
STRUCTURAL_ROOTS = frozenset(
    {
        "self",
        "table",
        "col",
        "column",
        "src",
        "seed",
        "cycle",
        "mask_table_names",
        "ref_table",
        "ref_column",
        "offenders",
        "width",
        # A schema-int offset the layout validator itself computes.
        "prev_end",
        # A source's key in `sources:` (a name, like `table`).
        "source_name",
        # Column names common to the mask columns and a drop_column op.
        "conflicts",
        # The fixed wired-trigger set; the submitted trigger is never interpolated.
        "_WIRED_TRIGGERS",
        # The leaf-type name a `_check` validator closes over (a module-level constant).
        "kind",
    }
)
_BENIGN_CALLS = frozenset(
    {"str", "repr", "sorted", "len", "format", "join", "list", "tuple", "set"}
)


def reachable_models() -> frozenset[type[BaseModel]]:
    """Every model reachable from ``decoy_engine.__all__`` through model-field types."""
    seen: set[type[BaseModel]] = set()

    def walk(tp: object) -> None:
        for arg in typing.get_args(tp):
            walk(arg)
        if inspect.isclass(tp) and issubclass(tp, BaseModel) and tp not in seen:
            seen.add(tp)
            for field in tp.model_fields.values():
                walk(field.annotation)

    for name in decoy_engine.__all__:
        obj = getattr(decoy_engine, name, None)
        if inspect.isclass(obj):
            walk(obj)
    return frozenset(seen)


def _defining_files(models: frozenset[type[BaseModel]]) -> list[Path]:
    files = {Path(inspect.getfile(inspect.getmodule(m))) for m in models}  # type: ignore[arg-type]
    return sorted(files)


# -- rule 1: routes ------------------------------------------------------------------------


def _pydantic_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    classes: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _PYDANTIC_MODULES:
            classes |= {a.asname or a.name for a in node.names if a.name == "ValidationError"}
        elif isinstance(node, ast.Import):
            modules |= {(a.asname or a.name) for a in node.names if a.name in _PYDANTIC_MODULES}
    return classes, modules


def _names_pydantic_error(expr: ast.expr | None, classes: set[str], modules: set[str]) -> bool:
    if isinstance(expr, ast.Tuple):
        return any(_names_pydantic_error(e, classes, modules) for e in expr.elts)
    if isinstance(expr, ast.Name):
        return expr.id in classes
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == "ValidationError"
        and isinstance(expr.value, ast.Name)
        and expr.value.id in modules
    )


def _last_name(expr: ast.expr) -> str:
    return expr.attr if isinstance(expr, ast.Attribute) else getattr(expr, "id", "")


def _reads_bound(node: ast.AST, bound: str) -> bool:
    """Whether ``node`` renders the error object ``bound`` as text or structure."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == bound and func.attr in {"errors", "json", "__str__"}:
                return True
        if isinstance(func, ast.Name) and func.id in {"str", "repr", "format"}:
            return any(isinstance(a, ast.Name) and a.id == bound for a in node.args)
    if isinstance(node, ast.FormattedValue):
        return isinstance(node.value, ast.Name) and node.value.id == bound
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return any(isinstance(n, ast.Name) and n.id == bound for n in ast.walk(node.right))
    return False


def route_findings(
    source: str, rel: str, model_names: frozenset[str]
) -> dict[tuple[str, str, str], int]:
    """Raw-error reads and public-model validation outside the choke points."""
    tree = ast.parse(source)
    classes, modules = _pydantic_aliases(tree)
    found: dict[tuple[str, str, str], int] = {}

    def add(qual: str, what: str) -> None:
        key = (rel, qual, what)
        found[key] = found.get(key, 0) + 1

    def visit(node: ast.AST, qual: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = qual
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if qual == "<module>" else f"{qual}.{child.name}"
            if (
                isinstance(child, ast.ExceptHandler)
                and child.name
                and rel not in _HANDLER_FILES
                and _names_pydantic_error(child.type, classes, modules)
            ):
                for inner in ast.walk(child):
                    if _reads_bound(inner, child.name):
                        add(name, "pydantic-error-read")
            if isinstance(child, ast.Call) and rel not in _ROUTE_FILES:
                func = child.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr.startswith("model_validate")
                    and _last_name(func.value) in model_names
                ):
                    add(name, "public-model-validate")
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr.startswith("validate_")
                    and isinstance(func.value, ast.Call)
                    and _last_name(func.value.func) == "TypeAdapter"
                    and any(
                        isinstance(n, ast.Name) and n.id in model_names
                        for n in ast.walk(func.value)
                    )
                ):
                    add(name, "public-model-validate")
            visit(child, name)

    visit(tree, "<module>")
    return found


def engine_route_findings() -> dict[tuple[str, str, str], int]:
    names = frozenset(m.__name__ for m in reachable_models())
    found: dict[tuple[str, str, str], int] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        found.update(route_findings(path.read_text(encoding="utf-8"), rel, names))
    return found


# -- rule 2: validator messages --------------------------------------------------------------


def _roots(expr: ast.expr) -> set[str]:
    """Variable roots an interpolated expression reads; ``<call>`` marks an opaque call result."""
    if isinstance(expr, ast.Constant):
        return set()
    if isinstance(expr, (ast.Name, ast.Attribute, ast.Subscript)):
        base: ast.expr = expr
        while isinstance(base, (ast.Attribute, ast.Subscript)):
            base = base.value
        return {base.id} if isinstance(base, ast.Name) else _roots(base)
    if isinstance(expr, ast.Call):
        func = expr.func
        callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if callee not in _BENIGN_CALLS:
            return {"<call>"}
        out: set[str] = set()
        if isinstance(func, ast.Attribute):
            out |= _roots(func.value)
        for arg in [*expr.args, *(k.value for k in expr.keywords)]:
            out |= _roots(arg)
        return out
    out = set()
    for child in ast.iter_child_nodes(expr):
        if isinstance(child, ast.expr):
            out |= _roots(child)
    return out


def _interpolated(msg: ast.expr) -> list[ast.expr]:
    if isinstance(msg, ast.Constant):
        return []
    if isinstance(msg, ast.JoinedStr):
        return [p.value for p in msg.values if isinstance(p, ast.FormattedValue)]
    if isinstance(msg, ast.BinOp):
        return [*_interpolated(msg.left), *_interpolated(msg.right)] or [msg]
    if (
        isinstance(msg, ast.Call)
        and isinstance(msg.func, ast.Attribute)
        and msg.func.attr == "format"
    ):
        return [*_interpolated(msg.func.value), *msg.args, *(k.value for k in msg.keywords)]
    return [msg]


def classify(msg: ast.expr | None, parts: list[ast.expr] | None = None) -> str:
    """``fixed``, ``name-only``, ``value-bearing`` or ``unclassified`` for a message expression.

    ``parts`` replaces the interpolated parts when producer returns were folded in.
    """
    if msg is None:
        return "fixed"
    parts = _interpolated(msg) if parts is None else parts
    if not parts:
        return "fixed"
    if any(_risky_in(p, RISKY_NAMES) for p in parts):
        return "value-bearing"
    roots = set().union(*(_roots(p) for p in parts))
    return "name-only" if roots <= STRUCTURAL_ROOTS else "unclassified"


def _digest(msg: ast.expr | None, folded: Sequence[ast.expr] = ()) -> str:
    """Hash of the message expression and of every producer return folded into it."""
    dump = "" if msg is None else ast.dump(msg, annotate_fields=False)
    dump += "".join("|" + ast.dump(r, annotate_fields=False) for r in folded)
    return hashlib.sha256(dump.encode()).hexdigest()[:10]


Producers = Mapping[str, Sequence[ast.expr]]


@functools.cache
def engine_producers() -> Producers:
    """Return expressions of each uniquely named module-level function in the engine.

    A message built from a producer's result is classified from the producer's own returns, so a
    change to a returned string moves the digest of every message that uses it. A name defined
    twice is left out, which keeps its callers unclassified.
    """
    found: dict[str, list[ast.expr]] = {}
    ambiguous: set[str] = set()
    for path in sorted(SRC.rglob("*.py")):
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name in found:
                ambiguous.add(node.name)
            found[node.name] = [
                n.value
                for n in ast.walk(node)
                if isinstance(n, ast.Return)
                and n.value is not None
                and not (isinstance(n.value, ast.Constant) and n.value.value is None)
            ]
    return {k: v for k, v in found.items() if k not in ambiguous}


def _fold(
    parts: list[ast.expr], binds: Mapping[str, str], producers: Producers
) -> tuple[list[ast.expr], list[ast.expr]]:
    """Replace a producer call, or a local bound to one, by the producer's returns."""
    out: list[ast.expr] = []
    folded: list[ast.expr] = []
    for part in parts:
        callee = None
        if isinstance(part, ast.Name) and part.id in binds:
            callee = binds[part.id]
        elif isinstance(part, ast.Call) and _call_name(part) in producers:
            callee = _call_name(part)
        if callee is None:
            out.append(part)
            continue
        for ret in producers[callee]:
            folded.append(ret)
            out.extend(_interpolated(ret))
    return out, folded


def _call_name(call: ast.Call) -> str:
    return call.func.id if isinstance(call.func, ast.Name) else ""


def _local_binds(func: ast.AST, producers: Producers) -> dict[str, str]:
    """Locals assigned directly from a producer call: ``problem = csv_dialect_problem(...)``."""
    binds: dict[str, str] = {}
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and _call_name(node.value) in producers
        ):
            binds[node.targets[0].id] = _call_name(node.value)
    return binds


def _rule_error_names(tree: ast.AST) -> set[str]:
    """Local names that refer to ``ConfigRuleError``: import aliases and plain re-bindings."""
    names = {"ConfigRuleError"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname for a in node.names if a.name == "ConfigRuleError" and a.asname}
    grew = True
    while grew:
        grew = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and _last_name(node.value) in names
                and node.targets[0].id not in names
            ):
                names.add(node.targets[0].id)
                grew = True
    return names


def message_findings(
    source: str, rel: str, producers: Producers | None = None, value_errors: bool = True
) -> set[str]:
    """Baseline keys for every ``ConfigRuleError`` construction (and, if asked, ``ValueError`` raise).

    A ``ConfigRuleError`` is found at the call, however it is imported or bound, because the
    converter trusts its text wherever it is raised from. A plain ``ValueError`` only matters in a
    module that defines a public model, so the caller turns ``value_errors`` off elsewhere.
    """
    tree = ast.parse(source)
    producers = engine_producers() if producers is None else producers
    rule_names = _rule_error_names(tree)
    found: set[str] = set()

    def record(call: ast.Call, callee: str, name: str, scope: Mapping[str, str]) -> None:
        msg = call.args[0] if call.args else None
        parts, folded = _fold(_interpolated(msg), scope, producers) if msg else ([], [])
        kind = classify(msg, parts)
        if kind in {"fixed", "name-only"}:
            # A plain ValueError renders as the generic row, so a certified message
            # must be a ConfigRuleError.
            kind = "permitted" if callee == "ConfigRuleError" else "uncertified"
        found.add(f"{kind}::{rel}::{name}::{_digest(msg, folded)}")

    def visit(node: ast.AST, qual: str, binds: Mapping[str, str]) -> None:
        for child in ast.iter_child_nodes(node):
            name = qual
            scope = binds
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = child.name if qual == "<module>" else f"{qual}.{child.name}"
                if not isinstance(child, ast.ClassDef):
                    scope = _local_binds(child, producers)
            if (
                value_errors
                and isinstance(child, ast.Raise)
                and isinstance(child.exc, ast.Call)
                and _last_name(child.exc.func) == "ValueError"
            ):
                record(child.exc, "ValueError", name, scope)
            if isinstance(child, ast.Call) and _last_name(child.func) in rule_names:
                record(child, "ConfigRuleError", name, scope)
            visit(child, name, scope)

    visit(tree, "<module>", {})
    return found


def scan_messages(
    root: Path, defining: frozenset[str], producers: Producers | None = None
) -> set[str]:
    """Scan every module under ``root``; ``ValueError`` only where a public model is defined."""
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        found |= message_findings(text, rel, producers, value_errors=rel in defining)
    return found


def engine_message_findings() -> set[str]:
    defining = frozenset(p.relative_to(SRC).as_posix() for p in _defining_files(reachable_models()))
    return scan_messages(SRC, defining)


# -- tests -------------------------------------------------------------------------------------


def test_public_models_are_found_and_cover_the_defining_modules() -> None:
    files = {p.relative_to(SRC).as_posix() for p in _defining_files(reachable_models())}
    assert {"config/_pipeline.py", "config/_sources.py", "sdk.py"} <= files


def test_no_raw_validation_error_route() -> None:
    found = engine_route_findings()
    allowed = {k for k in found if (k[0], k[1]) in ROUTE_ALLOWLIST}
    assert set(found) - allowed == set(), "route validates a public model or reads the raw error"
    stale = set(ROUTE_ALLOWLIST) - {(k[0], k[1]) for k in found}
    assert stale == set(), "ROUTE_ALLOWLIST entry no longer needed; remove it"


def test_no_value_bearing_unclassified_or_uncertified_message() -> None:
    open_kinds = ("value-bearing::", "unclassified::", "uncertified::")
    entries = set(json.loads(BASELINE.read_text(encoding="utf-8"))["entries"])
    assert {e for e in entries if e.startswith(open_kinds)} == set()
    assert {f for f in engine_message_findings() if f.startswith(open_kinds)} == set()


def test_validator_messages_match_the_baseline() -> None:
    entries = set(json.loads(BASELINE.read_text(encoding="utf-8"))["entries"])
    found = engine_message_findings()
    assert found - entries == set(), "new, changed or value-bearing validator message"
    assert entries - found == set(), "baseline entry no longer present; remove it (shrink-only)"


_NAMES = frozenset({"PipelineConfig", "TableConfig"})


def _routes(src: str, rel: str = "mod.py") -> dict[tuple[str, str, str], int]:
    return route_findings(src, rel, _NAMES)


@pytest.mark.parametrize(
    "src",
    [
        "from pydantic import ValidationError\n"
        "def f(x):\n    try:\n        PipelineConfig.model_validate(x)\n"
        "    except ValidationError as e:\n        raise RuntimeError(str(e))\n",
        "import pydantic as p\n"
        "def f(x):\n    try:\n        pass\n    except p.ValidationError as e:\n"
        "        return e.errors()\n",
        "from pydantic_core import ValidationError as VE\n"
        "def f():\n    try:\n        pass\n    except (OSError, VE) as e:\n"
        "        return f'{e}'\n",
        "def f(x):\n    return TableConfig.model_validate_json(x)\n",
        "def f(x):\n    return TypeAdapter(list[TableConfig]).validate_python(x)\n",
    ],
)
def test_route_scanner_sees_planted_bypass(src: str) -> None:
    assert _routes(src)


def test_route_scanner_ignores_the_engine_error_and_the_choke_points() -> None:
    engine = (
        "from decoy_engine.errors import ValidationError\n"
        "def f():\n    try:\n        pass\n    except ValidationError as e:\n        return str(e)\n"
    )
    assert _routes(engine) == {}
    assert (
        _routes("def f(x):\n    return PipelineConfig.model_validate(x)\n", "config/_parse.py")
        == {}
    )
    err = "from pydantic import ValidationError\ndef f(e):\n    try:\n        pass\n    except ValidationError as e:\n        return e.errors()\n"
    assert _routes(err, "safe_output/_validation.py") == {}


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("'fixed text'", "fixed"),
        ("f'table {self.name!r} requires `x`'", "name-only"),
        ("f'bad value {value}'", "value-bearing"),
        ("f'bad {helper(self)}'", "unclassified"),
        ("f'bad {problem}'", "unclassified"),
        ("f\"cycle {' -> '.join(cycle)}\"", "name-only"),
        ("'bad {}'.format(row)", "value-bearing"),
    ],
)
def test_classifier(expr: str, expected: str) -> None:
    assert classify(ast.parse(expr, mode="eval").body) == expected


def test_message_scanner_sees_planted_and_certified_sites() -> None:
    planted = "def v(self, value):\n    raise ValueError(f'bad value {value}')\n"
    assert any(k.startswith("value-bearing::") for k in message_findings(planted, "m.py"))
    changed = "def v(self):\n    raise ConfigRuleError(f'table {self.name!r} bad')\n"
    certified = message_findings(changed, "m.py")
    assert len(certified) == 1 and next(iter(certified)).startswith("permitted::")
    edited = "def v(self):\n    raise ConfigRuleError(f'table {self.name!r} worse')\n"
    assert message_findings(edited, "m.py") != certified
    plain = message_findings("def v():\n    raise ValueError('fixed')\n", "m.py")
    assert len(plain) == 1 and next(iter(plain)).startswith("uncertified::")


@pytest.mark.parametrize(
    "src",
    [
        # an import alias
        "from decoy_engine.errors import ConfigRuleError as C\n"
        "def v(value):\n    raise C(f'bad {value}')\n",
        # a bound instance
        "def v(value):\n    e = ConfigRuleError(f'bad {value}')\n    raise e\n",
        # a module attribute
        "from decoy_engine import errors\n"
        "def v(value):\n    raise errors.ConfigRuleError(f'bad {value}')\n",
        # a re-bound name
        "D = ConfigRuleError\ndef v(value):\n    raise D(f'bad {value}')\n",
    ],
)
def test_message_scanner_sees_aliased_and_bound_construction(src: str) -> None:
    found = message_findings(src, "m.py", value_errors=False)
    assert any(k.startswith("value-bearing::") for k in found)


def test_message_scan_reaches_modules_that_define_no_public_model(tmp_path: Path) -> None:
    (tmp_path / "helper.py").write_text(
        "from decoy_engine.errors import ConfigRuleError\n"
        "def check(value):\n    raise ConfigRuleError(f'bad {value}')\n",
        encoding="utf-8",
    )
    found = scan_messages(tmp_path, frozenset(), producers={})
    assert any(k.startswith("value-bearing::helper.py::check::") for k in found)
