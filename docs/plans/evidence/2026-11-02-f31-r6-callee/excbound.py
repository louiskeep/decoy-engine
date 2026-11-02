"""exc-copy census, two passes. Stdlib ast; imports nothing from the target.
Usage: python3 -I excbound.py <platform root>

Pass 1 (revision 5, unchanged): inside `except ... as NAME`, a return, an assignment or a call
statement whose value reads NAME in an R7 text shape: str/repr(NAME), str(f(NAME)), NAME.args,
NAME.message, or NAME interpolated by an f-string, % or .format. Reads inside HTTPException
(part (b)), raise, logger and audit calls are excluded.

Pass 2 (revision 6): the flows pass 1 cannot see.
  attr    the same positions, but NAME read through a text attribute other than args/message
          (TEXT_ATTRS: reason, detail, msg, error, summary, description, stderr, stdout), bare
          or interpolated.
  callee  NAME (or NAME.<attr>) passed to a function resolved inside api/ (a def in the same
          module, or one bound by `from api.x import f [as g]` anywhere in the module); the
          parameter is then treated as NAME inside the callee, recursively to depth 3, with the
          pass-1 and attr shapes. The row is the callee's copy site, with the chain.
  store   the exception object itself stored where it outlives the handler: assigned to an
          attribute or subscript, or passed to .append/.add/.put/.set/.setdefault.
  unres   NAME passed bare to a call that resolves to no def in api/ (a method on an object, a
          third-party call), other than logger, audit, HTTPException, str/repr (pass 1 shapes),
          isinstance-like checks, and an exception constructed in a raise (a re-wrap: its text
          is a row where it is caught and copied).
          Reported so it is classified, not silently dropped.
Named limits: dynamic dispatch and methods on objects are `unres` rows, not followed; a value
stored in a container (a registry dict, a queue) is not traced to its reader, so the READ side
is what pass 2 reports: any text-attribute read of a caught exception, whatever produced it.
"""
import ast, sys, pathlib, collections

LOGGERS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
SKIP_CALLS = {"HTTPException", "write_audit", "write_audit_best_effort"}
P1_ATTRS = ("args", "message")
TEXT_ATTRS = ("reason", "detail", "msg", "error", "summary", "description", "stderr", "stdout")
STORE_METHODS = {"append", "add", "put", "put_nowait", "set", "setdefault", "extend"}
NOT_A_FLOW = {"isinstance", "type", "id", "getattr", "hasattr", "bool", "len", "str", "repr"}
MAX_DEPTH = 3


def _is_name(e, name):
    return isinstance(e, ast.Name) and e.id == name


def _reads_text(e, name, attrs=P1_ATTRS):
    if isinstance(e, ast.Attribute) and _is_name(e.value, name) and e.attr in attrs:
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


def _call_name(f):
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _excluded(node, top, par):
    """True if node sits inside HTTPException, a logger or an audit call below `top`."""
    n = node
    while n is not top and n in par:
        n = par[n]
        if isinstance(n, ast.Call):
            nm = _call_name(n.func)
            if nm in SKIP_CALLS or (isinstance(n.func, ast.Attribute) and nm in LOGGERS):
                return True
    return False


def _in_raise(node, top, par):
    n = node
    while n is not top and n in par:
        n = par[n]
        if isinstance(n, ast.Raise):
            return True
    return isinstance(top, ast.Raise)


def text_uses(expr, name, par, attrs):
    return any(_reads_text(y, name, attrs) and not _excluded(y, expr, par) for y in ast.walk(expr))


def copy_sites(body, name, par, attrs):
    """(lineno, kind) for return/assign/call statements in `body` copying NAME's text."""
    out = []
    for st in body:
        for n in ast.walk(st):
            if isinstance(n, ast.Return) and n.value is not None:
                kind, val = "return", n.value
            elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                kind, val = "assign", n.value
            elif isinstance(n, ast.Expr) and isinstance(n.value, (ast.Call, ast.Yield, ast.Await)):
                kind, val = "call", n.value
            else:
                continue
            if text_uses(val, name, par, attrs):
                out.append((n.lineno, kind))
    return out


class Module:
    def __init__(self, root, p):
        self.rel = str(p.relative_to(root))
        self.mod = self.rel[:-3].replace("/", ".").removesuffix(".__init__")
        self.tree = ast.parse(p.read_text())
        self.par = {c: a for a in ast.walk(self.tree) for c in ast.iter_child_nodes(a)}
        self.defs = {n.name: n for n in self.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.imports = {}
        for n in ast.walk(self.tree):
            if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("api") and n.level == 0:
                for a in n.names:
                    self.imports[a.asname or a.name] = (n.module, a.name)


def load(root):
    mods = {}
    for p in sorted((root / "api").rglob("*.py")):
        if "migrations" in p.parts:
            continue
        m = Module(root, p)
        mods[m.mod] = m
    return mods


def resolve(mods, m, func):
    if isinstance(func, ast.Name):
        if func.id in m.defs:
            return m, m.defs[func.id]
        if func.id in m.imports:
            mod, nm = m.imports[func.id]
            t = mods.get(mod)
            if t and nm in t.defs:
                return t, t.defs[nm]
    return None


def param_for(fn, call, idx, kw):
    params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if kw is not None:
        return kw if kw in params + [a.arg for a in fn.args.kwonlyargs] else None
    return params[idx] if idx < len(params) else None


def flows(call, name):
    """(index or None, keyword or None) for each argument that is NAME or NAME.<attr>."""
    def hit(e):
        return _is_name(e, name) or (isinstance(e, ast.Attribute) and _is_name(e.value, name))
    for i, a in enumerate(call.args):
        if hit(a):
            yield i, None
    for k in call.keywords:
        if k.arg and hit(k.value):
            yield None, k.arg


def follow(mods, m, body, name, chain, depth, rows, seen):
    """Pass-2 attr rows in `body`, then follow calls that pass NAME."""
    for line, kind in copy_sites(body, name, m.par, P1_ATTRS + TEXT_ATTRS):
        if depth == 0 and (line, kind) in set(copy_sites(body, name, m.par, P1_ATTRS)):
            continue  # a pass-1 row
        rows.add(("callee" if depth else "attr", f"{m.rel}:{line}", kind, chain))
    for st in body:
        for n in ast.walk(st):
            if not isinstance(n, ast.Call):
                continue
            nm = _call_name(n.func)
            if nm in SKIP_CALLS or nm in NOT_A_FLOW or (isinstance(n.func, ast.Attribute) and nm in LOGGERS):
                continue
            if _excluded(n, st, m.par):
                continue  # inside HTTPException (part (b)), a logger or an audit call
            args = list(flows(n, name))
            if not args:
                continue
            bare = any(_is_name(a, name) for a in n.args) or any(
                k.arg and _is_name(k.value, name) for k in n.keywords)
            if isinstance(n.func, ast.Attribute) and nm in STORE_METHODS and bare:
                rows.add(("store", f"{m.rel}:{n.lineno}", "call", chain))
                continue
            r = resolve(mods, m, n.func)
            if r is None:
                if _in_raise(n, st, m.par):
                    continue  # a re-wrap: its text is a row where it is caught and copied
                if bare and depth == 0:
                    rows.add(("unres", f"{m.rel}:{n.lineno}", nm or "?", chain))
                continue
            tm, fn = r
            if depth + 1 > MAX_DEPTH:
                continue
            for idx, kw in args:
                p = param_for(fn, n, idx, kw)
                key = (tm.rel, fn.name, p)
                if p is None or key in seen:
                    continue
                seen.add(key)
                follow(mods, tm, fn.body, p, f"{chain} -> {tm.rel}:{fn.lineno} {fn.name}({p})",
                       depth + 1, rows, seen)
    for st in body:
        for n in ast.walk(st):
            if isinstance(n, ast.Assign) and _is_name(n.value, name) and any(
                    isinstance(t, (ast.Attribute, ast.Subscript)) for t in n.targets):
                rows.add(("store", f"{m.rel}:{n.lineno}", "assign", chain))


def main():
    root = pathlib.Path(sys.argv[1])
    mods = load(root)
    p1, p2 = set(), set()
    for m in mods.values():
        for h in ast.walk(m.tree):
            if not (isinstance(h, ast.ExceptHandler) and h.name):
                continue
            for line, kind in copy_sites(h.body, h.name, m.par, P1_ATTRS):
                p1.add((m.rel, line, kind))
            follow(mods, m, h.body, h.name, f"{m.rel}:{h.lineno}", 0, p2, set())
    print("pass 1:", len(p1), "sites in", len({r[0] for r in p1}), "files;",
          dict(sorted(collections.Counter(r[2] for r in p1).items())))
    for f, l, k in sorted(p1):
        print(f"  {k:7s} {f}:{l}")
    p2 = {r for r in p2 if (r[0] != "callee") or r[1] not in {f"{f}:{l}" for f, l, _ in p1}}
    print("pass 2:", len(p2), "rows;", dict(sorted(collections.Counter(r[0] for r in p2).items())))
    for kind, site, how, chain in sorted(p2):
        print(f"  {kind:6s} {site} [{how}] via {chain}")


if __name__ == "__main__":
    main()
