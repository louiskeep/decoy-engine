import ast, sys, pathlib, collections
root = pathlib.Path(sys.argv[1])
types = sys.argv[2].split(",")
hits = collections.defaultdict(list)
for p in sorted(root.rglob("*.py")):
    try: t = ast.parse(p.read_text())
    except Exception: continue
    for n in ast.walk(t):
        if isinstance(n, ast.Call):
            f = n.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in types:
                # field value kinds
                kinds=[]
                for a in list(n.args)+[k.value for k in n.keywords]:
                    if isinstance(a, ast.JoinedStr): kinds.append("fstr")
                    elif isinstance(a, ast.Call) and getattr(a.func,'id',None)=='str': kinds.append("str()")
                hits[name].append((str(p.relative_to(root.parent.parent)), n.lineno, ",".join(kinds)))
for k in types:
    v = hits[k]; files = sorted({h[0] for h in v})
    print(f"## {k}: {len(v)} calls in {len(files)} files; fstr/str() args at {sum(1 for h in v if h[2])}")
    for h in v: print("   ", f"{h[0]}:{h[1]}", h[2])
