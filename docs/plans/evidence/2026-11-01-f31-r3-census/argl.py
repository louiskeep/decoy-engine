import ast,sys,pathlib
T=set(sys.argv[2].split(","))
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    t=ast.parse(p.read_text())
    for n in ast.walk(t):
        if isinstance(n,ast.Call):
            f=n.func; nm=f.id if isinstance(f,ast.Name) else f.attr if isinstance(f,ast.Attribute) else None
            if nm in T:
                for k in n.keywords:
                    if k.arg in ("detail","message","notes","error","hint") and not isinstance(k.value,ast.Constant):
                        print(f"{p}:{k.value.lineno} {nm}.{k.arg} = {ast.unparse(k.value)[:90]}")
