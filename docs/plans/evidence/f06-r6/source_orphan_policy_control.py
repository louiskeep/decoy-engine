import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[4]))
from pathlib import Path
import tempfile, pyarrow as pa, pyarrow.parquet as pq
from tests.acceptance.test_f06_parent_quarantine import _config
from tests.acceptance._f06_support import run
# control: no quarantine, child genuinely references missing parent "9"
for pol in ("remap","preserve"):
  for mode in ("full_frame","sequential"):
    d=Path(tempfile.mkdtemp()); cfg, src = _config(d,"parse")
    cfg["tables"][0]["columns"][1] = {"name":"amt","strategy":"passthrough"}; cfg.pop("protected_quarantine")
    cfg["relationships"][0]["orphan_policy"]=pol
    src["child"]=pa.table({"cid":["a","b"],"pid":["9","2"]}); pq.write_table(src["child"], cfg["sources"]["child"]["path"])
    r=run(cfg,src,execution_mode=mode); p=r.outputs["parent"].column("id").to_pylist(); c=r.outputs["child"].column("pid").to_pylist()
    print("source-orphan control", pol, mode, "orphans published:", [x for x in c if x not in p])
