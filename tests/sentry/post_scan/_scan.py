"""AST scanners for the four post-scan choke points (no pytest import, so a script can run them).

Each scanner returns site keys `path::scope::label::ordinal`, the same shape the other ratchet
sentries use, so a baseline can only shrink. A key names code, never data.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "decoy_engine"
BASELINES = ROOT / "tests" / "sentry" / "baselines"

Label = Callable[[ast.AST], str | None]
BROAD = {"Exception", "BaseException"}
SEVERITY_NAMES = {"severity", "sev"}
OUTCOMES = {"fail", "error"}


class _Scoped(ast.NodeVisitor):
    def __init__(self, label: Label) -> None:
        self.label, self.stack, self.found = label, [], []

    def _scope(self, node: ast.AST) -> None:
        self.stack.append(getattr(node, "name", "?"))
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scope  # noqa: N815

    def generic_visit(self, node: ast.AST) -> None:
        found = self.label(node)
        if found is not None:
            self.found.append((".".join(self.stack) or "<module>", found))
        super().generic_visit(node)


def sites(source: str, rel: str, label: Label) -> list[str]:
    visitor = _Scoped(label)
    visitor.visit(ast.parse(source))
    seen: dict[tuple[str, str], int] = {}
    keys = []
    for scope, name in visitor.found:
        n = seen.get((scope, name), 0)
        seen[(scope, name)] = n + 1
        keys.append(f"{rel}::{scope}::{name}::{n}")
    return keys


def files(*parts: str) -> Iterator[Path]:
    """Python files under `src/decoy_engine/<part>` for each part (a file or a folder)."""
    for part in parts:
        path = SRC / part
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from sorted(path.rglob("*.py"))


def scan(label: Label, *parts: str, skip: str = "") -> list[str]:
    out = []
    for path in files(*parts):
        rel = path.relative_to(SRC.parent).as_posix()
        if not skip or path.name != skip:
            out += sites(path.read_text(encoding="utf-8"), rel, label)
    return out


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return set().union(*(_strings(e) for e in node.elts)) if node.elts else set()
    return set()


# ----- one blocking decision ------------------------------------------------------------------


def _severity_operand(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in SEVERITY_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr == "severity"
    if isinstance(node, ast.Subscript):
        return "severity" in _strings(node.slice)
    return isinstance(node, ast.Call) and any("severity" in _strings(a) for a in node.args)


def blocking_label(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call) and _name(node.func) == "PostScanBlockedError":
        return "construct"
    if isinstance(node, ast.ClassDef) and node.name == "PostScanBlockedError":
        return "define"
    if isinstance(node, ast.Compare):
        operands = [node.left, *node.comparators]
        if any(_severity_operand(o) for o in operands) and any(
            _strings(o) & OUTCOMES for o in operands
        ):
            return "severity-compare"
    return None


def blocking_sites() -> list[str]:
    return scan(blocking_label, ".", skip="_blocked.py")


# ----- one pass-through rule ------------------------------------------------------------------


def _broad(handler: ast.ExceptHandler) -> bool:
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(k is None or _name(k) in BROAD for k in kinds)


def _opens_with_pass_through(handler: ast.ExceptHandler) -> bool:
    first = handler.body[0] if handler.body else None
    if not isinstance(first, ast.If) or not handler.name:
        return False
    test = first.test
    ok_test = (
        isinstance(test, ast.Call)
        and _name(test.func) == "passes_through"
        and [_name(a) for a in test.args] == [handler.name]
    )
    ok_body = (
        len(first.body) == 1 and isinstance(first.body[0], ast.Raise) and first.body[0].exc is None
    )
    return ok_test and ok_body


def _catches_decoy_error(handler: ast.ExceptHandler) -> bool:
    from decoy_engine import errors

    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    for kind in kinds:
        found = getattr(errors, _name(kind) or "", None) if kind is not None else None
        if isinstance(found, type) and issubclass(found, errors.DecoyError):
            return True
    return False


def pass_through_label(node: ast.AST) -> str | None:
    if not isinstance(node, ast.ExceptHandler):
        return None
    if _catches_decoy_error(node):
        return "decoy-handler"
    if _broad(node) and not _opens_with_pass_through(node):
        return "unguarded"
    return None


def pass_through_sites() -> list[str]:
    return scan(pass_through_label, "verification/postmask", "execution/pipeline/_post_scan.py")


# ----- one key core ---------------------------------------------------------------------------


def key_core_label(node: ast.AST) -> str | None:
    if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "duckdb" for a in node.names):
        return "duckdb-import"
    if isinstance(node, ast.ImportFrom):
        names = {a.name for a in node.names}
        if (node.module or "").split(".")[0] == "duckdb" or "connect_duckdb" in names:
            return "duckdb-import"
    if isinstance(node, ast.Call) and _name(node.func) == "_comparable":
        return "comparable-call"
    if isinstance(node, ast.Compare) and any(
        isinstance(o, ast.Attribute) and o.attr in {"type", "types", "schema"}
        for o in [node.left, *node.comparators]
    ):
        return "type-compare"
    return None


def key_core_sites() -> list[str]:
    return scan(key_core_label, "verification", "execution/pipeline/_post_scan.py")


# ----- one reason-identity serializer ---------------------------------------------------------


def serializer_label(node: ast.AST) -> str | None:
    wanted = {"namespace", "code"}
    if isinstance(node, ast.Dict):
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        return "dict-literal" if wanted <= keys else None
    if isinstance(node, ast.Call):
        func = _name(node.func)
        if func == "dict" and wanted <= {k.arg for k in node.keywords}:
            return "dict-call"
        if func == "asdict" and node.args and _name(node.args[0]) in {"ref"}:
            return "asdict-ref"
    return None


def serializer_sites() -> list[str]:
    return scan(serializer_label, "phase.py", "verification/postmask")


# ----- one applicability predicate ------------------------------------------------------------

APPLIES_HOME = "decoy_engine/config/_post_scan.py::post_scan_gate_applies::"
GATE = "execution/pipeline/_post_scan.py"


def _reads(node: ast.AST, word: str) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and _name(n.func) == f"post_scan_{word}_requested":
            return True
        if _name(n) == word or (isinstance(n, ast.Constant) and n.value == word):
            return True
    return False


def applies_label(node: ast.AST) -> str | None:
    """An `and`/`or` that weighs declared relationships against `post_scan.integrity`."""
    if isinstance(node, ast.BoolOp):
        ops = node.values
        if any(_reads(o, "relationships") for o in ops) and any(
            _reads(o, "integrity") for o in ops
        ):
            return "applies-decision"
    return None


def required_label(node: ast.AST) -> str | None:
    """`required` set from anything but a `post_scan_gate_applies(...)` call."""
    targets: list[ast.expr] = []
    value: ast.expr | None = None
    if isinstance(node, ast.Assign):
        targets, value = node.targets, node.value
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets, value = [node.target], node.value
    if value is None or not any(_name(t) == "required" for t in targets):
        return None
    ok = isinstance(value, ast.Call) and _name(value.func) == "post_scan_gate_applies"
    return None if ok and not isinstance(node, ast.AugAssign) else "required-assign"


def applies_sites() -> list[str]:
    decisions = [k for k in scan(applies_label, ".") if not k.startswith(APPLIES_HOME)]
    return decisions + scan(required_label, GATE)


# ----- no commit without the gate -------------------------------------------------------------

COMMIT_HOME = ("_publication_observer.py", "observed_commit")
# `publish.commit()` is the session wrapper, whose own `commit` goes through `observed_commit`.
# Only these files may call it, matched by exact path and receiver name, so a helper named
# `publish` elsewhere is still a commit site.
PUBLISH_SESSION_FILES = frozenset(
    {
        "decoy_engine/execution/_pipeline_full_frame.py",
        "decoy_engine/execution/_pipeline_mixed_route.py",
        "decoy_engine/generation/_streamed.py",
    }
)


def _receiver(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Attribute):
        inner = func.value
        return inner.attr if isinstance(inner, ast.Attribute) else _name(inner)
    return None


def commit_label(node: ast.AST) -> str | None:
    """A sink commit outside `observed_commit`: `x.commit()`, `atomic_commit(...)`, or a
    `getattr(x, "commit")` handle. `publish.commit()` is the named session wrapper, whose own
    `commit` goes through `observed_commit`."""
    if not isinstance(node, ast.Call):
        return None
    name = _name(node.func)
    if name == "atomic_commit":
        return "atomic-commit"
    if name == "getattr" and len(node.args) > 1 and "commit" in _strings(node.args[1]):
        return "getattr-commit"
    if name == "commit" and isinstance(node.func, ast.Attribute):
        return "publish-commit" if _receiver(node) == "publish" else "commit-call"
    return None


def session_commit(key: str) -> bool:
    """True for a `publish.commit()` site in one of the session wrapper's own files."""
    path, _, label, _ = key.split("::")
    return label == "publish-commit" and path in PUBLISH_SESSION_FILES


def commit_sites() -> list[str]:
    keys = scan(commit_label, "execution", "generation")
    home = f"/{COMMIT_HOME[0]}::{COMMIT_HOME[1]}::"
    return [k for k in keys if home not in k and not session_commit(k)]


# ----- one model predicate --------------------------------------------------------------------

MODEL_HOME = "decoy_engine/config/_post_scan.py"


def model_label(node: ast.AST) -> str | None:
    """A read of `ner_findings`, or `detectors` weighed against `"regex"`, outside the predicate."""
    if isinstance(node, ast.Constant) and node.value == "ner_findings":
        return "ner-findings-read"
    if isinstance(node, ast.Compare):
        operands = [node.left, *node.comparators]
        if any("regex" in _strings(o) for o in operands) and any(
            "detectors" in ast.unparse(o) for o in operands
        ):
            return "detectors-compare"
    return None


def model_sites() -> list[str]:
    return [k for k in scan(model_label, ".") if not k.startswith(MODEL_HOME)]


# ----- one route residency decision ----------------------------------------------------------

READABILITY_HOME = "decoy_engine/execution/_transforms_gate.py::post_scan_resident_for::"
CONFIG_HOME = "decoy_engine/config/_post_scan.py"


def readability_label(node: ast.AST) -> str | None:
    """A residency decision made outside the one helper."""
    if isinstance(node, ast.Call) and _name(node.func) == "post_scan_resident_reason":
        return "decision-call"
    return None


def readability_sites() -> list[str]:
    keys = scan(readability_label, ".")
    return [k for k in keys if not k.startswith((READABILITY_HOME, CONFIG_HOME))]
