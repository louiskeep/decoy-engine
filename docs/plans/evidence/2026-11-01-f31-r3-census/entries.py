import inspect, importlib, decoy_engine as de
from decoy_engine import fileaccess
PATHY={"path","paths","source","sources","base_dir","staging_dir","output_dir","temp_dir","vault_path","root","file","filename","dest","target","directory","dir","location","locator","ref"}
CONF={"config","cfg","spec","s","yaml_text","plan_config","job_config","pipeline"}
ents={}
for n in de.__all__:
    o=getattr(de,n)
    if inspect.isfunction(o): ents[f"{o.__module__}.{o.__qualname__}"]=o
    elif inspect.isclass(o) and not issubclass(o,BaseException):
        for m,f in vars(o).items():
            if (not m.startswith("_") or m=="__init__") and inspect.isfunction(f) and f.__module__.startswith("decoy_engine"):
                ents[f"{o.__module__}.{o.__qualname__}.{m}"]=f
for q in fileaccess.ENTRY_POINTS:
    mod,_,fn=q.rpartition("."); ents[q]=getattr(importlib.import_module(mod),fn)
hold=[]
for q,f in sorted(ents.items()):
    f=inspect.unwrap(f)
    try: ps=set(inspect.signature(f).parameters)
    except (TypeError,ValueError): continue
    hit=sorted(ps & (PATHY|CONF))
    if hit: hold.append((q,hit))
print("public entries:",len(ents),"| entry points (fileaccess):",len(fileaccess.ENTRY_POINTS),"| taking a path/config-named parameter:",len(hold))
import collections; print(collections.Counter(q.split(".")[1] for q,_ in hold).most_common(12))
ep=set(fileaccess.ENTRY_POINTS)
rest=[(q,h) for q,h in hold if q not in ep]
print("outside ENTRY_POINTS:",len(rest),"in",len({q.rsplit('.',2)[0] if q.count('.')>2 else q for q,_ in rest}),"approx modules")
mods=collections.Counter()
for q,h in rest:
    f=inspect.unwrap(ents[q]); mods[inspect.getsourcefile(f).split("decoy_engine/",1)[-1]]+=1
print(len(mods),"files"); [print(" ",v,k) for k,v in sorted(mods.items())]
