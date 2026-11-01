import ast, sys, pathlib, collections
root=pathlib.Path(sys.argv[1]); M={"debug","info","warning","warn","error","exception","critical","log"}
tot=0; args=0; by=collections.Counter(); bya=collections.Counter()
for p in sorted(root.rglob("*.py")):
    t=ast.parse(p.read_text())
    for n in ast.walk(t):
        if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr in M:
            v=n.func.value; nm=v.id if isinstance(v,ast.Name) else v.attr if isinstance(v,ast.Attribute) else ""
            if nm not in {"logger","_logger","log","_log","LOGGER","_LOG","LOG","logging"}: continue
            tot+=1; by[str(p)]+=1
            first=n.args[0] if n.args else None
            if len(n.args)>(2 if n.func.attr=="log" else 1) or isinstance(first,ast.JoinedStr) or any(k.arg!="stacklevel" for k in n.keywords):
                args+=1; bya[str(p)]+=1
print(tot,"calls in",len(by),"files;",args,"with arguments in",len(bya),"files")
for k,v in sorted(by.items()): print(f"  {v:2d} {bya[k]:2d} {k}")
