"""Outbound GCS and S3 sentries: one client factory per provider (AT-C9, AT-C41), timeout and retry on every call (AT-C10).

AT-C9: a call resolving to ``google.cloud.storage.Client`` anywhere under
``src/decoy_engine`` except ``connectors/_clients.py`` is a finding; so is a
``DYNAMIC`` call target in a module that imports ``google.cloud.storage`` (fail closed).

AT-C10: the GCS name set is derived from the installed library (public methods of
``Client``, ``Bucket`` and ``Blob`` with a ``timeout`` parameter, plus those taking
``**kwargs`` whose source mentions ``timeout``). A call ``<anything>.<name>(...)`` with
``name`` in the set, outside the factory, that does not pass both ``timeout=`` and
``retry=`` as explicit keywords is a finding, whatever the receiver. Today's non-GCS
name collisions live in the shrink-only baseline ``baselines/gcs-call-kwargs.json``.

Named limit: matching is by method name, so a bound method stored in a variable and
called later, or a client built inside an unwrapped third-party library, is not seen.
Calls wrapped in ``functools.partial`` and the ``google.cloud.storage.transfer_manager``
functions are not seen either. Only ``src/decoy_engine`` is scanned. Resolution reuses ``tests/sentry/fa/resolve.py``.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.sentry.fa.resolve import (
    DYNAMIC,
    ProjectIndex,
    Scope,
    build_import_map,
    module_name,
    resolve,
)

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
FACTORY = "decoy_engine/connectors/_clients.py"
BASELINE = HERE / "baselines" / "gcs-call-kwargs.json"
CORPUS = HERE / "fixtures" / "outbound_seeded.py.txt"
CLIENT_TARGETS = frozenset({"google.cloud.storage.Client", "google.cloud.storage.client.Client"})
REQUIRED = frozenset({"timeout", "retry"})
# Canonical spellings of an S3 client or resource build. Calls on a built object print as
# `Name()`, so `boto3.Session(...).client(...)` reads `boto3.Session().client`.
S3_BUILDERS = frozenset(
    {
        "boto3.client",
        "boto3.resource",
        "boto3.Session",
        "boto3.session.Session",
        "boto3.Session().client",
        "boto3.Session().resource",
        "boto3.session.Session().client",
        "boto3.session.Session().resource",
        "botocore.session.get_session().create_client",
        "botocore.session.Session().create_client",
    }
)
# First-inventory size of the collision walk; lower it together with the baseline.
WALK_FLOOR = 150


def gcs_name_set() -> frozenset[str]:
    try:
        from google.cloud.storage import Blob, Bucket, Client
    except ImportError as exc:  # never skip: the regression gate installs the extra
        pytest.fail(f"google-cloud-storage is not importable; install the `cloud` extra ({exc})")
    names: set[str] = set()
    for cls in (Client, Bucket, Blob):
        for name in dir(cls):
            if name.startswith("_") or isinstance(inspect.getattr_static(cls, name), property):
                continue
            member = getattr(cls, name)
            if not callable(member):
                continue
            try:
                params = inspect.signature(member).parameters
            except (TypeError, ValueError):
                continue
            if "timeout" in params:
                names.add(name)
            elif any(p.kind is p.VAR_KEYWORD for p in params.values()):
                try:
                    if "timeout" in inspect.getsource(member):
                        names.add(name)
                except (OSError, TypeError):
                    continue
    return frozenset(names)


@dataclass(frozen=True)
class Finding:
    rule: str
    path: str
    line: int
    qualname: str
    method: str
    ordinal: int

    @property
    def key(self) -> str:
        return f"{self.path}::{self.qualname}::{self.method}::{self.ordinal}"

    def message(self) -> str:
        return f"{self.rule} {self.path}:{self.line} {self.qualname} {self.method}"


def _calls_in(body: list[ast.stmt]) -> list[ast.Call]:
    """Calls in ``body`` outside nested defs and classes, in source order."""
    out: list[ast.Call] = []
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return sorted(out, key=lambda c: (c.lineno, c.col_offset))


def _nested(body: list[ast.stmt]) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    out: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append(node)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _imports_storage(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(a.name.startswith("google.cloud.storage") for a in node.names)
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return module.startswith("google.cloud.storage") or (
            module == "google.cloud" and any(a.name == "storage" for a in node.names)
        )
    return False


def _is_client_target(target: object) -> bool:
    """``Client`` itself or any attribute of it (``Client.from_service_account_json``)."""
    return isinstance(target, str) and any(
        target == t or target.startswith(t + ".") for t in CLIENT_TARGETS
    )


def _chain(node: ast.AST, scope: Scope) -> str | None:
    """Dotted name of a call target through import aliases, with `()` for each call on the way."""
    if isinstance(node, ast.Name):
        return scope.lookup(node.id)
    if isinstance(node, ast.Attribute):
        base = _chain(node.value, scope)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, ast.Call):
        base = _chain(node.func, scope)
        return f"{base}()" if base else None
    return None


class _ModuleScan:
    def __init__(
        self, rel: str, name: str, tree: ast.Module, index: ProjectIndex, names: frozenset[str]
    ):
        self.rel, self.name, self.tree, self.index, self.names = rel, name, tree, index, names
        self.is_package = rel.endswith("__init__.py")
        self.imports_storage = any(_imports_storage(n) for n in ast.walk(tree))
        self.findings: list[Finding] = []
        self.seen = 0

    def run(self) -> list[Finding]:
        scope = Scope(build_import_map(self.tree.body, self.name, self.is_package))
        self._body(self.tree.body, scope, "<module>", "")
        return self.findings

    def _body(self, body: list[ast.stmt], scope: Scope, qual: str, owner: str) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
                if isinstance(target, ast.Name) and not isinstance(stmt.value, ast.Call):
                    found = resolve(stmt.value, scope, self.index)
                    if found is not None and found != DYNAMIC:
                        scope.aliases[target.id] = found
        self._classify(_calls_in(body), scope, qual)
        for node in _nested(body):
            if isinstance(node, ast.ClassDef):
                self._body(node.body, scope, f"{owner}{node.name}", f"{owner}{node.name}.")
            else:
                inner = scope.child(build_import_map(node.body, self.name, self.is_package))
                name = f"{owner}{node.name}"
                self._body(node.body, inner, name, f"{name}.")

    def _classify(self, calls: list[ast.Call], scope: Scope, qual: str) -> None:
        counts: dict[str, int] = defaultdict(int)
        for call in calls:
            target = resolve(call.func, scope, self.index)
            if self.rel != FACTORY and (
                _is_client_target(target) or (target == DYNAMIC and self.imports_storage)
            ):
                self._add("AT-C9", call, qual, "Client", 0)
            if self.rel != FACTORY and _chain(call.func, scope) in S3_BUILDERS:
                self._add("AT-C41", call, qual, "S3", 0)
            if not isinstance(call.func, ast.Attribute) or call.func.attr not in self.names:
                continue
            method = call.func.attr
            ordinal = counts[method]
            counts[method] += 1
            if self.rel == FACTORY:
                continue
            self.seen += 1
            # `timeout=None` or `retry=None` switches the bound off, so it does not count.
            explicit = {
                k.arg
                for k in call.keywords
                if k.arg is not None
                and not (isinstance(k.value, ast.Constant) and k.value.value is None)
            }
            if not explicit >= REQUIRED:
                self._add("AT-C10", call, qual, method, ordinal)

    def _add(self, rule: str, call: ast.Call, qual: str, method: str, ordinal: int) -> None:
        self.findings.append(Finding(rule, self.rel, call.lineno, qual, method, ordinal))


@dataclass(frozen=True)
class Scan:
    findings: list[Finding]
    seen: int


def scan_tree(src: Path = SRC) -> Scan:
    names = gcs_name_set()
    index = ProjectIndex("decoy_engine")
    trees: dict[str, tuple[str, ast.Module]] = {}
    for path in sorted((src / "decoy_engine").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        trees[path.relative_to(src).as_posix()] = (module_name(path, src), tree)
        index.add(module_name(path, src), tree, path.name == "__init__.py")
    findings: list[Finding] = []
    seen = 0
    for rel, (name, tree) in trees.items():
        scanner = _ModuleScan(rel, name, tree, index, names)
        findings.extend(scanner.run())
        seen += scanner.seen
    return Scan(findings, seen)


def scan_text(text: str, rel: str = "<seeded>/outbound_seeded.py") -> list[Finding]:
    index = ProjectIndex("decoy_engine")
    return _ModuleScan(rel, "seeded", ast.parse(text), index, gcs_name_set()).run()


def load_baseline() -> dict[str, dict[str, str]]:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert data["sentry_id"] == "gcs-call-kwargs" and data["created_in"] == "X4d"
    return data["entries"]


def write_baseline(reasons: dict[str, str]) -> int:
    """Write the first inventory; ``reasons`` maps every current AT-C10 key to its reason.

    A key without a reason fails, so every row is reviewed before it lands.
    """
    keys = sorted(f.key for f in scan_tree().findings if f.rule == "AT-C10")
    missing = [k for k in keys if k not in reasons]
    if missing:
        raise SystemExit(f"no reason for {len(missing)} key(s): {missing[:5]}")
    entries = {k: {"reason": reasons[k]} for k in keys}
    body = {"sentry_id": "gcs-call-kwargs", "created_in": "X4d", "entries": entries}
    BASELINE.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return len(entries)


@pytest.fixture(scope="module")
def tree_scan() -> Scan:
    return scan_tree()


# ----- name set ---------------------------------------------------------------------


def test_name_set_is_derived_and_not_vacuous() -> None:
    names = gcs_name_set()
    assert len(names) >= 40, sorted(names)
    assert {"download_as_bytes", "reload", "open", "get_bucket", "list_blobs"} <= names


# ----- AT-C9 / AT-C10 on the real tree ---------------------------------------------


def test_no_storage_client_outside_the_factory(tree_scan: Scan) -> None:
    bad = [f.message() for f in tree_scan.findings if f.rule == "AT-C9"]
    assert not bad, "storage.Client built outside connectors/_clients.py:\n" + "\n".join(bad)


def test_no_s3_client_outside_the_factory(tree_scan: Scan) -> None:
    """AT-C41: the profiler and the connectors build S3 clients only through ``_clients``."""
    bad = [f.message() for f in tree_scan.findings if f.rule == "AT-C41"]
    assert not bad, "S3 client built outside connectors/_clients.py:\n" + "\n".join(bad)


def test_gcs_calls_carry_timeout_and_retry(tree_scan: Scan) -> None:
    allowed = set(load_baseline())
    found = {f.key: f for f in tree_scan.findings if f.rule == "AT-C10"}
    new = sorted(found[k].message() for k in set(found) - allowed)
    stale = sorted(allowed - set(found))
    assert not new, "GCS-named call without explicit timeout= and retry=:\n" + "\n".join(new)
    assert not stale, "baseline rows no longer match a call (delete them):\n" + "\n".join(stale)


def test_walk_sees_the_collisions_and_no_live_gcs_site(tree_scan: Scan) -> None:
    assert tree_scan.seen >= WALK_FLOOR, tree_scan.seen
    live = [
        f.message()
        for f in tree_scan.findings
        if f.path in {"decoy_engine/connectors/gcs.py", "decoy_engine/profile/_cloud_readers.py"}
    ]
    assert not live, live


def test_baseline_rows_are_reviewed() -> None:
    entries = load_baseline()
    for key, entry in entries.items():
        reason = entry["reason"]
        assert len(reason) >= 15 and not reason.lower().startswith(("review", "todo")), key
        assert "connectors/gcs.py" not in key and "_cloud_readers.py" not in key, key
        path, _qual, method, ordinal = key.split("::")
        assert path.startswith("decoy_engine/") and ordinal.isdigit(), key
        assert method in gcs_name_set(), key


MESSAGE = re.compile(r"^AT-C(9|10|41) [\w/.<>-]+:\d+ [\w.<>]+ \w+$")


def test_finding_messages_carry_location_only(tree_scan: Scan) -> None:
    findings = tree_scan.findings + scan_text(CORPUS.read_text(encoding="utf-8"))
    assert findings
    for finding in findings:
        assert MESSAGE.match(finding.message()), finding.message()


# ----- seeded bypasses ---------------------------------------------------------------

SEEDED: dict[str, set[tuple[str, str]]] = {
    "client_module_alias": {("AT-C9", "Client")},
    "client_name_alias": {("AT-C9", "Client")},
    "client_context_manager": {("AT-C9", "Client")},
    "download_plain": {("AT-C10", "download_as_bytes")},
    "copy_blob_call": {("AT-C10", "copy_blob")},
    "Holder.reload_attribute": {("AT-C10", "reload")},
    "helper_return": {("AT-C10", "download_as_bytes")},
    "open_plain": {("AT-C10", "open")},
    "reload_missing_retry": {("AT-C10", "reload")},
    "reload_kwargs_unpack": {("AT-C10", "reload")},
    "reload_both": set(),
    "client_from_json_file": {("AT-C9", "Client")},
    "client_from_info": {("AT-C9", "Client")},
    "client_anonymous": {("AT-C9", "Client")},
    "reload_timeout_none": {("AT-C10", "reload")},
    "reload_retry_none": {("AT-C10", "reload")},
    "s3_client_plain": {("AT-C41", "S3")},
    "s3_resource_plain": {("AT-C41", "S3")},
    "s3_session_client": {("AT-C41", "S3")},
    "s3_session_module_client": {("AT-C41", "S3")},
    "s3_from_import_client": {("AT-C41", "S3")},
    "s3_from_import_session": {("AT-C41", "S3")},
    "s3_alias_client": {("AT-C41", "S3")},
    "s3_botocore_session": {("AT-C41", "S3")},
    "s3_session_in_variable": {("AT-C41", "S3")},
}


@pytest.fixture(scope="module")
def seeded() -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for f in scan_text(CORPUS.read_text(encoding="utf-8")):
        out[f.qualname].add((f.rule, f.method))
    return out


def test_seeded_corpus_is_not_vacuous(seeded: dict[str, set[tuple[str, str]]]) -> None:
    assert seeded
    assert set(seeded) <= set(SEEDED), sorted(set(seeded) - set(SEEDED))


@pytest.mark.parametrize("case", sorted(SEEDED))
def test_seeded_case(case: str, seeded: dict[str, set[tuple[str, str]]]) -> None:
    assert seeded.get(case, set()) == SEEDED[case]


# ----- trusted Decoy errors: gcs_error passes TransientError/PermanentError through --------
# That is only safe while these modules build them from fixed text. A literal, or a
# module-level name bound to one, is the only argument shape allowed.

LITERAL_FILES = (
    "decoy_engine/connectors/_clients.py",
    "decoy_engine/connectors/gcs.py",
    "decoy_engine/connectors/object_store.py",
    "decoy_engine/connectors/s3.py",
    "decoy_engine/profile/_cloud_readers.py",
)
ERROR_CLASSES = frozenset({"TransientError", "PermanentError", "ConnectorError"})


def _is_str_const(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def non_literal_errors(text: str, rel: str) -> list[str]:
    """Error constructions whose text is not fixed.

    One positional string literal (or a module-level name bound to one) plus at most one
    ``kind=`` keyword whose value is a string literal.
    """
    tree = ast.parse(text)
    fixed = {
        t.id
        for n in tree.body
        if isinstance(n, (ast.Assign, ast.AnnAssign))
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
        for t in (n.targets if isinstance(n, ast.Assign) else [n.target])
        if isinstance(t, ast.Name)
    }
    bad = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        f = call.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if name not in ERROR_CLASSES:
            continue
        arg = call.args[0] if len(call.args) == 1 else None
        kind_ok = all(k.arg == "kind" and _is_str_const(k.value) for k in call.keywords) and (
            len(call.keywords) <= 1
        )
        ok = kind_ok and (_is_str_const(arg) or (isinstance(arg, ast.Name) and arg.id in fixed))
        if not ok:
            bad.append(f"{rel}:{call.lineno} {name}")
    return bad


def unchained_raise_findings(text: str, rel: str) -> list[str]:
    """``raise ... from ...`` anywhere, and ``raise <expr>`` inside an ``except`` handler.

    A bare ``raise`` is allowed: it re-raises the in-flight error without adding a cause.
    """
    tree = ast.parse(text)
    in_handler = {
        id(r)
        for h in ast.walk(tree)
        if isinstance(h, ast.ExceptHandler)
        for stmt in h.body
        for r in ast.walk(stmt)
        if isinstance(r, ast.Raise)
    }
    return [
        f"{rel}:{n.lineno} raise"
        for n in ast.walk(tree)
        if isinstance(n, ast.Raise)
        and (n.cause is not None or (n.exc is not None and id(n) in in_handler))
    ]


def test_connector_errors_are_built_from_literals() -> None:
    bad = [
        b
        for rel in LITERAL_FILES
        for b in non_literal_errors((SRC / rel).read_text(encoding="utf-8"), rel)
    ]
    assert not bad, bad


def test_connector_raises_leave_the_except_block() -> None:
    bad = [
        b
        for rel in LITERAL_FILES
        for b in unchained_raise_findings((SRC / rel).read_text(encoding="utf-8"), rel)
    ]
    assert not bad, bad


@pytest.mark.parametrize(
    "snippet",
    [
        'PermanentError(f"bad {exc}")',
        "PermanentError(message)",
        "TransientError(str(exc))",
        'ConnectorError("a", "b")',
        "PermanentError(MSG, kind=k)",
        'PermanentError("m", kind="transient", extra="x")',
        'PermanentError("m", other="x")',
    ],
    ids=["f-string", "variable", "str-exc", "two-args", "kind-variable", "two-kw", "other-kw"],
)
def test_literal_check_flags_seeded_cases(snippet: str) -> None:
    assert len(non_literal_errors("MSG = 'x'\n" + snippet, "<seeded>")) == 1


def test_literal_check_accepts_literals_and_named_constants() -> None:
    text = 'FIXED = "x"\nPermanentError("y")\nTransientError(FIXED)\n'
    text += 'PermanentError(FIXED, kind="transient")\n'
    assert non_literal_errors(text, "<seeded>") == []


RAISES = {
    "from-cause": (
        "try:\n    f()\nexcept Exception as exc:\n    err = e(exc)\nraise e from exc\n",
        1,
    ),
    "raise-in-except": ("try:\n    f()\nexcept Exception as exc:\n    raise e\n", 1),
    "bare-in-except": ("try:\n    f()\nexcept Exception:\n    raise\n", 0),
    "after-block": ("try:\n    f()\nexcept Exception as exc:\n    err = e(exc)\nraise err\n", 0),
}


@pytest.mark.parametrize("case", sorted(RAISES))
def test_raise_check_seeded_cases(case: str) -> None:
    text, count = RAISES[case]
    assert len(unchained_raise_findings(text, "<seeded>")) == count


def test_s3_connector_is_checked_for_literal_errors() -> None:
    assert "decoy_engine/connectors/s3.py" in LITERAL_FILES
