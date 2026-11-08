import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[4]))
from pathlib import Path
import tempfile
from tests.acceptance.test_f06_parent_quarantine import _config
from tests.acceptance._f06_support import run
from tests.acceptance.test_f06_route_parity import _runtime
for stage in ("parse","format"):
 for pol in ("fail","remap","preserve","warn"):
  for kw in (dict(execution_mode="full_frame"), dict(execution_mode="sequential"), dict(execution_mode="out_of_core"), dict()):
    d = Path(tempfile.mkdtemp())
    cfg, src = _config(d, stage)
    cfg["relationships"][0]["orphan_policy"]=pol
    try:
        r = run(cfg, src, quarantine_runtime=_runtime(d/"q"), **kw); qm=r.quality_metrics
        p = r.outputs["parent"].column("id").to_pylist(); c = r.outputs["child"].column("pid").to_pylist()
        print(stage, pol, kw, "PUBLISHED exec", qm["execution"].get("execution_mode"), "parent", p, "child", c, "orphans", [x for x in c if x not in p])
    except Exception as e:
        print(stage, pol, kw, "ERR", type(e).__name__, getattr(e,"code",None), str(e)[:160])
