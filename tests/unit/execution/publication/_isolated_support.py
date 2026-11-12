"""Shared helpers for the F22 isolated-route tests (plan 5.7, 7.2; slice c2).

A job is one small Parquet table masked by `redact`, so its output depends only on the table
name and the row count (a different row count is a different result). A fake child replaces
the real worker module for one test (`install_fake_worker`, as `test_isolated_run.py` does):
it stages `t.parquet` by plain path writes, may plant something beside it, and reports a
`completed` envelope, standing in for an untrusted producer (class 6).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig

ENGINE_VERSION = "f22-c2-isolated"
PLANT_ENV = "DECOY_TEST_F22_PLANT"
MARKER_ENV = "DECOY_TEST_F22_MARKER"


def job(tmp: Path, rows: int, *, table: str = "t") -> dict[str, Any]:
    """A validated one-table `redact` config whose source holds `rows` rows."""
    src = tmp / f"{table}-{rows}.parquet"
    pq.write_table(pa.table({"a": [f"value-{i}" for i in range(rows)]}), src)
    raw = {
        "version": 2,
        "global_settings": {"seed": 3},
        "sources": {table: {"type": "file", "format": "parquet", "path": str(src)}},
        "tables": [{"name": table, "columns": [{"name": "a", "strategy": "redact"}]}],
        "targets": {table: {"type": "file", "format": "parquet", "path": str(tmp / "o.parquet")}},
    }
    return PipelineConfig.model_validate(raw).model_dump()


def sources(cfg: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in cfg["sources"].items()}


# The fake child: stage `t.parquet` by a plain path write (the umask decides its mode), plant
# what `PLANT_ENV` names, leave a marker that it ran (non-vacuity), report `completed`.
FAKE_WORKER_SRC = """
import json, os, sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

payload_path = Path(sys.argv[1])
payload = json.loads(payload_path.read_text("utf-8"))
stage = Path(payload["staging_output_dir"])
stage.mkdir(parents=True, exist_ok=True)
pq.write_table(pa.table({"a": ["x", "y"]}), stage / "t.parquet")
plant = os.environ.get("DECOY_TEST_F22_PLANT", "")
if plant == "descriptor":
    (stage / "_decoy_publication.json").write_text('{"schema": "decoy-publication/v1"}')
elif plant == "extra_file":
    (stage / "notes.txt").write_text("extra")
elif plant == "nested_dir":
    (stage / "nested").mkdir()
    pq.write_table(pa.table({"a": ["z"]}), stage / "nested" / "t.parquet")
elif plant == "symlink":
    os.rename(stage / "t.parquet", stage.parent / "elsewhere.parquet")
    os.symlink(stage.parent / "elsewhere.parquet", stage / "t.parquet")
elif plant == "missing_table":
    os.unlink(stage / "t.parquet")
elif plant == "bad_name":
    pass
marker = os.environ.get("DECOY_TEST_F22_MARKER")
if marker:
    Path(marker).write_text(plant or "none")
tables = ["../t"] if plant == "bad_name" else ["t"]
envelope = {"outcome": "completed", "peak_rss_mb": 1.0, "staging_output_dir": str(stage),
            "staged_tables": tables, "quality_metrics": {}, "table_kinds": {}}
result = payload_path.parent / "result.json"
result.write_text(json.dumps(envelope))
os.chmod(result, 0o600)  # this fake is the untrusted producer; its envelope stays readable
"""


def install_fake_worker(tmp: Path, monkeypatch: pytest.MonkeyPatch, *, plant: str = "") -> Path:
    """Point the isolated route at the fake child; returns the marker path it writes."""
    fake_dir = tmp / "_fake_workers"
    fake_dir.mkdir(exist_ok=True)
    (fake_dir / "f22_fake_worker.py").write_text(FAKE_WORKER_SRC, encoding="utf-8")
    existing = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_dir) + (os.pathsep + existing if existing else ""))
    monkeypatch.setenv(PLANT_ENV, plant)
    marker = tmp / "fake-worker-ran"
    monkeypatch.setenv(MARKER_ENV, str(marker))
    monkeypatch.setattr("decoy_engine.execution._isolated_run._WORKER_MODULE", "f22_fake_worker")
    return marker


def entries(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.iterdir())


def umask_isolated_case(spec_path: str) -> None:
    """In a `spawn` child under `spec["umask"]`: publish `spec["rows"]` rows through the
    isolated route into `<base>/out/leaf`, with the real child or the plain-writing fake."""
    import json

    from decoy_engine.execution import _isolated_run, run_pipeline_isolated
    from tests.fileaccess.publication._dirmode_support import _probe, _write

    spec = json.loads(Path(spec_path).read_text("utf-8"))
    base = Path(spec["base"])
    cfg = json.loads(Path(spec["cfg"]).read_text("utf-8"))
    tables = sources(cfg)  # read before the umask narrows anything
    os.umask(spec["umask"])
    found: dict[str, Any] = {"probe": _probe(base)}
    if spec.get("fake_dir"):
        existing = os.environ.get("PYTHONPATH", "")
        os.environ["PYTHONPATH"] = spec["fake_dir"] + (os.pathsep + existing if existing else "")
        os.environ[PLANT_ENV] = ""
        _isolated_run._WORKER_MODULE = "f22_fake_worker"
    try:
        result = run_pipeline_isolated(
            cfg, tables, engine_version=ENGINE_VERSION, output_dir=base / "out" / "leaf"
        )
        receipt = result.publication_receipt
        found.update(
            outcome=result.outcome,
            error=result.error,
            receipt=None if receipt is None else receipt.outcome,
            os_error=None if receipt is None else receipt.os_error,
        )
    except Exception as exc:
        found.update(outcome="raised", exc=type(exc).__name__)
    _write(spec["result"], found)
