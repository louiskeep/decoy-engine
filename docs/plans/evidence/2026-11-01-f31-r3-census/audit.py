import ast,sys,pathlib,collections
by=collections.defaultdict(list)
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    if "migrations" in p.parts: continue
    t=ast.parse(p.read_text())
    for n in ast.walk(t):
        if isinstance(n,ast.Call):
            f=n.func; nm=f.id if isinstance(f,ast.Name) else f.attr if isinstance(f,ast.Attribute) else None
            if nm in sys.argv[2].split(","):
                d=[k.value for k in n.keywords if k.arg=="detail"]
                by[str(p)].append((n.lineno, type(d[0]).__name__ if d else "-"))
tot=sum(map(len,by.values())); c=collections.Counter(k for v in by.values() for _,k in v)
print(tot,"calls in",len(by),"files; detail kinds",dict(c))
for k,v in by.items(): print(" ",len(v),k,[x for x in v if x[1]=="JoinedStr"][:8])
