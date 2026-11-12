import sys, json
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[4]))
from pathlib import Path
import tempfile
from tests.acceptance.test_f06_parent_quarantine import _config, ROUTE_KWARGS
from tests.acceptance._f06_support import run
from tests.acceptance.test_f06_route_parity import _runtime
for route, kw in ROUTE_KWARGS.items():
  for stage in ("parse","format"):
    d = Path(tempfile.mkdtemp())
    cfg, src = _config(d, stage)
    # control: null policy
    for c in cfg["tables"][0]["columns"]:
        if c.get("error_policy"): c["error_policy"]="null"
    cfg.pop("protected_quarantine")
    try:
        r = run(cfg, src, **kw)
        qm = r.quality_metrics
        print(route, stage, "exec", qm.get("execution",{}).get("execution_mode"), "auto_chunk", (qm.get("auto_chunk") or {}).get("mode"), "uni", "unified_slice_activation" in qm, "mixed", "mixed_route" in qm, "refusals", qm.get("execution",{}).get("declared_route_refusals"), sorted(qm))
    except Exception as e:
        print(route, stage, "ERR", type(e).__name__, getattr(e,"code",None), e)
