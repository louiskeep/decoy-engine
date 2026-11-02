"""Count HTTPException calls lexically inside an `except` handler body, split by whether the
detail reads the except-bound name. Sizes Dennis's sink re-frame for part (b): a handler that
withholds every in-except detail not built by a closed constructor would touch all of them.
Usage: python3 -I httpexc_in_except.py <api dir>. Stdlib ast; imports nothing from the target."""
import ast, sys, pathlib

reads = other = files = 0
seen = set()
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    if "migrations" in p.parts:
        continue
    t = ast.parse(p.read_text())
    for h in ast.walk(t):
        if not isinstance(h, ast.ExceptHandler):
            continue
        for st in h.body:
            for n in ast.walk(st):
                if not isinstance(n, ast.Call) or id(n) in seen:
                    continue
                f = n.func
                nm = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if nm != "HTTPException":
                    continue
                seen.add(id(n))
                d = [k.value for k in n.keywords if k.arg == "detail"] or n.args[1:2]
                uses = h.name and d and any(
                    isinstance(y, ast.Name) and y.id == h.name for y in ast.walk(d[0])
                )
                if uses:
                    reads += 1
                else:
                    other += 1
                    files += 0
print(f"in-except HTTPException: {reads + other}; detail reads the bound name: {reads}; does not: {other}")
