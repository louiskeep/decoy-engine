import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[4]))
from pathlib import Path
import tempfile
import pyarrow as pa, pyarrow.parquet as pq
from tests.acceptance._f06_support import run, column, INT64, PROTECTED_QUARANTINE
from tests.acceptance.test_f06_route_parity import _runtime
def w(d, name, t):
    p=str(d/f"{name}.parquet"); pq.write_table(t,p); return {"type":"file","format":"parquet","path":p},{"type":"file","format":"parquet","path":p+".out"}
amt = lambda: column("amt", logical_type="integer", parse_format=INT64, error_policy="quarantine")
def composite(d, pol):
    par = pa.table({"a":["1","2"],"b":["x","y"],"amt":["x","5"]}); chi = pa.table({"cid":["c1","c2"],"pa":["1","2"],"pb":["x","y"]})
    ps,pd_=w(d,"parent",par); cs,cd=w(d,"child",chi)
    return {"version":1,"global_settings":{"seed":7},"sources":{"parent":ps,"child":cs},
      "tables":[{"name":"parent","columns":[column("a","hash",namespace="ns"),column("b","hash",namespace="ns"),amt()]},
                {"name":"child","columns":[column("cid"),column("pa","hash",namespace="ns"),column("pb","hash",namespace="ns")]}],
      "targets":{"parent":pd_,"child":cd},
      "relationships":[{"parent":{"table":"parent","columns":["a","b"]},"children":[{"table":"child","columns":["pa","pb"]}],"orphan_policy":pol,"namespace":"ns"}],
      "namespaces":{},"protected_quarantine":PROTECTED_QUARANTINE}, {"parent":par,"child":chi}, ("parent",("a","b")), ("child",("pa","pb"))
def selfref(d, pol):
    t = pa.table({"id":["1","2","3"],"mgr":[None,"1","2"],"amt":["x","5","6"]})
    s,o=w(d,"emp",t)
    return {"version":1,"global_settings":{"seed":7},"sources":{"emp":s},
      "tables":[{"name":"emp","columns":[column("id","hash",namespace="ns"),column("mgr","hash",namespace="ns"),amt()]}],
      "targets":{"emp":o},
      "relationships":[{"parent":{"table":"emp","columns":["id"]},"children":[{"table":"emp","columns":["mgr"]}],"orphan_policy":pol,"namespace":"ns"}],
      "namespaces":{},"protected_quarantine":PROTECTED_QUARANTINE}, {"emp":t}, ("emp",("id",)), ("emp",("mgr",))
for shape in (composite, selfref):
  for pol in ("fail","remap","preserve","warn"):
    for kw in (dict(execution_mode="full_frame"), dict(execution_mode="sequential"), dict()):
      d=Path(tempfile.mkdtemp()); cfg, src, P, C = shape(d, pol)
      try:
        r=run(cfg, src, quarantine_runtime=_runtime(d/"q"), **kw)
        pk=set(zip(*[r.outputs[P[0]].column(c).to_pylist() for c in P[1]]))
        ck=[k for k in zip(*[r.outputs[C[0]].column(c).to_pylist() for c in C[1]]) if None not in k]
        print(shape.__name__, pol, kw, "PUBLISHED", r.quality_metrics["execution"].get("execution_mode"), "orphans", sum(k not in pk for k in ck))
      except Exception as e:
        print(shape.__name__, pol, kw, "ERR", getattr(e,"code",type(e).__name__))
