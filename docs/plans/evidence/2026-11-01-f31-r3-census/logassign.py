import ast,sys,pathlib,collections
by=collections.defaultdict(list)
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    if "migrations" in p.parts: continue
    t=ast.parse(p.read_text())
    for n in ast.walk(t):
        tg=[]
        if isinstance(n,ast.Assign): tg=n.targets
        elif isinstance(n,(ast.AugAssign,ast.AnnAssign)): tg=[n.target]
        for x in tg:
            if isinstance(x,ast.Attribute) and x.attr=="log": by[str(p)].append(n.lineno)
for k,v in by.items(): print(len(v),k,v)
print(sum(map(len,by.values())),"in",len(by))
