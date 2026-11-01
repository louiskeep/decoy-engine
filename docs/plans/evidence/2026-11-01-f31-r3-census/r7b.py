import ast,sys,pathlib,collections
inr=0; out=collections.Counter(); outk=collections.Counter()
for p in sorted(pathlib.Path(sys.argv[1]).rglob("*.py")):
    t=ast.parse(p.read_text())
    par={}
    for a in ast.walk(t):
        for c in ast.iter_child_nodes(a): par[c]=a
    for h in ast.walk(t):
        if not (isinstance(h,ast.ExceptHandler) and h.name): continue
        nm=h.name
        for st in h.body:
          for x in ast.walk(st):
            k=None
            if isinstance(x,ast.FormattedValue) and any(isinstance(y,ast.Name) and y.id==nm for y in ast.walk(x.value)): k="fstring"
            elif isinstance(x,ast.Call) and isinstance(x.func,ast.Name) and x.func.id in("str","repr") and x.args and isinstance(x.args[0],ast.Name) and x.args[0].id==nm: k=x.func.id+"()"
            elif isinstance(x,ast.Attribute) and isinstance(x.value,ast.Name) and x.value.id==nm and x.attr in("args","message"): k="."+x.attr
            if not k: continue
            a=x
            while a in par and not isinstance(a,(ast.Raise,ast.stmt)): a=par[a]
            if isinstance(a,ast.Raise): inr+=1
            else: out[str(p).split("decoy_engine/",1)[-1].split("api/",1)[-1]]+=1; outk[k]+=1
print("inside raise:",inr,"| outside raise:",sum(out.values()),"in",len(out),"files",dict(outk))
for f,c in sorted(out.items()): print(f"  {c} {f}")
