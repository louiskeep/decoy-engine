import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[4]))
from pathlib import Path
import tempfile
from tests.acceptance.test_f06_parent_quarantine import _config
from tests.acceptance._f06_support import run
from tests.acceptance.test_f06_route_parity import _runtime
for pol in ("warn","preserve","remap"):
    d=Path(tempfile.mkdtemp()); cfg, src = _config(d,"parse"); cfg["relationships"][0]["orphan_policy"]=pol
    r=run(cfg,src,quarantine_runtime=_runtime(d/"q"),execution_mode="sequential")
    print(pol, "warnings:", [str(w)[:120] for w in r.warnings], "dc:", {k:v for k,v in r.quality_metrics["declared_casting"].items() if k!="quarantine"})
