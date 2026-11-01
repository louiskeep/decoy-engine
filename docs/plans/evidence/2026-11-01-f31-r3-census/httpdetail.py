import ast,sys,pathlib,collections
by=collections.defaultdict(list); kinds=collections.Counter()
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    if "migrations" in p.parts: continue
    t=ast.parse(p.read_text())
    for n in ast.walk(t):
        if isinstance(n,ast.Call):
            f=n.func; nm=f.id if isinstance(f,ast.Name) else f.attr if isinstance(f,ast.Attribute) else None
            if nm!="HTTPException": continue
            d=[k.value for k in n.keywords if k.arg=="detail"] or (n.args[1:2])
            if d and isinstance(d[0],ast.JoinedStr):
                vals=[ast.unparse(v.value) for v in d[0].values if isinstance(v,ast.FormattedValue)]
                exc=any(("exc" in v or v in("e","err","error")) for v in vals)
                kinds["exc" if exc else "other"]+=1
                by[str(p)].append((n.lineno,vals))
tot=sum(map(len,by.values())); print(tot,"f-string details in",len(by),"files",dict(kinds))
for k,v in sorted(by.items(),key=lambda x:-len(x[1])): print(f" {len(v):2d} {k}", [l for l,_ in v][:12])
