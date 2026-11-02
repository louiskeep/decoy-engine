"""HTTPException(detail=...) sites whose detail expression reads an except-bound name.
Usage: python3 httpexc.py <api dir>. Stdlib ast only; imports nothing from the target."""
import ast, sys, pathlib, collections
by = collections.defaultdict(list)
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    if "migrations" in p.parts:
        continue
    t = ast.parse(p.read_text())
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
                if d and any(isinstance(y, ast.Name) and y.id == h.name for y in ast.walk(d[0])):
                    by[str(p)].append(n.lineno)
tot = sum(len(set(v)) for v in by.values())
print(tot, "HTTPException details reading an except-bound name in", len(by), "files")
for k, v in sorted(by.items(), key=lambda x: (-len(set(x[1])), x[0])):
    print(f" {len(set(v)):2d} {k} {sorted(set(v))}")
