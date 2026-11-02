"""Classify HTTPException details that read an except-bound name: text (the exception's
str/repr/args/message or the bare name interpolated), classname (only type(NAME).__name__),
attr (another attribute of NAME). Usage: python3 httpexc_kinds.py <api dir>."""
import ast, sys, pathlib, collections
rows = []
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    if "migrations" in p.parts:
        continue
    t = ast.parse(p.read_text())
    par = {c: a for a in ast.walk(t) for c in ast.iter_child_nodes(a)}
    for h in ast.walk(t):
        if not (isinstance(h, ast.ExceptHandler) and h.name):
            continue
        for st in h.body:
            for n in ast.walk(st):
                if not isinstance(n, ast.Call):
                    continue
                f = n.func
                nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if nm != "HTTPException":
                    continue
                d = [k.value for k in n.keywords if k.arg == "detail"] or n.args[1:2]
                if not d:
                    continue
                uses = [y for y in ast.walk(d[0]) if isinstance(y, ast.Name) and y.id == h.name]
                if not uses:
                    continue
                kinds = set()
                for u in uses:
                    a = par.get(u)
                    if isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and a.func.id == "type":
                        kinds.add("classname")
                    elif isinstance(a, ast.Attribute) and a.attr not in ("args", "message"):
                        kinds.add("attr:" + a.attr)
                    else:
                        kinds.add("text")
                rows.append((str(p).split("api/", 1)[-1], n.lineno, "text" if "text" in kinds else "+".join(sorted(kinds))))
seen = set(); c = collections.Counter(); files = collections.defaultdict(set)
for f, l, k in rows:
    if (f, l) in seen: continue
    seen.add((f, l)); c[k.split(":")[0] if k.startswith("attr") else k] += 1; files[k.split(":")[0]].add(f)
print(len(seen), "sites;", dict(c), {k: len(v) for k, v in files.items()})
for f, l, k in sorted(set(rows)): print(f"  {k:24s} api/{f}:{l}")
