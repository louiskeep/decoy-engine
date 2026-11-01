import ast,sys,pathlib,collections
http=collections.Counter(); aud=collections.Counter()
for p in sorted(pathlib.Path("api").rglob("*.py")):
    if "migrations" in p.parts: continue
    t=ast.parse(p.read_text())
    for n in ast.walk(t):
        if not isinstance(n,ast.Call): continue
        f=n.func; nm=f.id if isinstance(f,ast.Name) else f.attr if isinstance(f,ast.Attribute) else None
        if nm=="HTTPException":
            d=[k.value for k in n.keywords if k.arg=="detail"] or n.args[1:2]
            if d and not isinstance(d[0],ast.Constant): http[str(p)]+=1
        if nm in("write_audit","write_audit_best_effort"):
            d=[k.value for k in n.keywords if k.arg=="detail"]
            if d and not isinstance(d[0],ast.Constant): aud[str(p)]+=1
print("http non-constant:",sum(http.values()),"in",len(http),"| audit non-constant:",sum(aud.values()),"in",len(aud))
u=sorted(set(http)|set(aud)); print("union files",len(u))
for i in range(0,len(u),10):
    g=u[i:i+10]; print(f"group {i//10+1}: {len(g)} files, {sum(http[x]+aud[x] for x in g)} sites:", ", ".join(f"{x.removeprefix('api/')}({http[x]}/{aud[x]})" for x in g))
