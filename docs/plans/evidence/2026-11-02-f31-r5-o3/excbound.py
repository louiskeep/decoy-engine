"""O3 census: except-bound exception text copied into a return value, an assignment or a call statement
(append, setattr, a builder call),
outside HTTPException, raise, logger and audit calls. A read counts as text in the R7
shapes: str/repr(NAME), str(f(NAME)), NAME.args, NAME.message, or NAME interpolated by an
f-string, % or .format. Passing NAME to a callee is not counted here (the callee's own copy is).
Usage: python3 excbound.py <platform root>. Prints one row per (file, line, stmt kind)."""
import ast, sys, pathlib, collections

LOGGERS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
SKIP_CALLS = {"HTTPException", "write_audit", "write_audit_best_effort"}


def _is_name(e, name):
    return isinstance(e, ast.Name) and e.id == name


def _reads_text(e, name):
    """The R7 text shapes: str/repr(NAME), str(f(NAME)), NAME.args, NAME.message,
    NAME interpolated by an f-string, % or .format."""
    if isinstance(e, ast.Attribute) and _is_name(e.value, name) and e.attr in ("args", "message"):
        return True
    if isinstance(e, ast.Call) and isinstance(e.func, ast.Name) and e.func.id in ("str", "repr") and e.args:
        a = e.args[0]
        if _is_name(a, name):
            return True
        if isinstance(a, ast.Call) and any(_is_name(x, name) for x in a.args):
            return True
    if isinstance(e, ast.FormattedValue) and _is_name(e.value, name):
        return True
    if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Mod):
        r = e.right
        items = r.elts if isinstance(r, ast.Tuple) else [r]
        if any(_is_name(x, name) for x in items):
            return True
    if (isinstance(e, ast.Call) and isinstance(e.func, ast.Attribute) and e.func.attr == "format"
            and any(_is_name(x, name) for x in e.args)):
        return True
    return False


def text_uses(expr, name, par):
    for y in ast.walk(expr):
        if not _reads_text(y, name):
            continue
        n, skip = y, False
        while n is not expr and n in par:
            n = par[n]
            if isinstance(n, ast.Call):
                f = n.func
                nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if nm in SKIP_CALLS or (isinstance(f, ast.Attribute) and nm in LOGGERS):
                    skip = True
        if not skip:
            return True
    return False


root = pathlib.Path(sys.argv[1])
rows = []
for p in sorted((root / "api").rglob("*.py")):
    if "migrations" in p.parts:
        continue
    t = ast.parse(p.read_text())
    par = {c: a for a in ast.walk(t) for c in ast.iter_child_nodes(a)}
    for h in ast.walk(t):
        if not (isinstance(h, ast.ExceptHandler) and h.name):
            continue
        for st in h.body:
            for n in ast.walk(st):
                if isinstance(n, ast.Return) and n.value is not None:
                    kind, val = "return", n.value
                elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                    kind, val = "assign", n.value
                elif isinstance(n, ast.Expr) and isinstance(n.value, (ast.Call, ast.Yield, ast.Await)):
                    kind, val = "call", n.value
                else:
                    continue
                if text_uses(val, h.name, par):
                    rows.append((str(p.relative_to(root)), n.lineno, kind))
rows = sorted(set(rows))
print(len(rows), "sites in", len({r[0] for r in rows}), "files;", dict(collections.Counter(r[2] for r in rows)))
for f, l, k in rows:
    print(f"  {k:7s} {f}:{l}")
