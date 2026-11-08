"""Digests of three configs without entity_key: compiled plan, outputs and metrics.

Runs against any engine tree on ``sys.path``, so the same function captured the pinned values
from engine main ``b5b1ad16`` and re-checks them here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.plan import compile_plan
from decoy_engine.plan._serialize import plan_to_yaml
from decoy_engine.profile import profile_source
from tests._keyed_secret import THROWAWAY_KEY_PROVIDER

_DATES = [f"20{10 + i % 10}-{1 + i % 12:02d}-{1 + i % 28:02d}" for i in range(30)]
_SOURCE = pa.table(
    {
        "pid": [None if i % 7 == 0 else f"p{i // 3}" for i in range(30)],
        "ssn": [f"{100000000 + i * 7919}" for i in range(30)],
        "note": [f"note {i}" for i in range(30)],
        "d": _DATES,
        "d2": list(reversed(_DATES)),
    }
)

CASES: dict[str, list[dict[str, Any]]] = {
    "plain": [
        {
            "name": "d",
            "strategy": "date_shift",
            "namespace": "dates",
            "provider_config": {"min_days": -40, "max_days": 40, "date_format": "%Y-%m-%d"},
        },
    ],
    "group_by": [
        {"name": "pid", "strategy": "hash", "namespace": "pids"},
        {
            "name": "d",
            "strategy": "date_shift",
            "namespace": "dates",
            "provider_config": {"group_by": "pid", "date_format": "%Y-%m-%d"},
        },
        {
            "name": "d2",
            "strategy": "date_shift",
            "namespace": "dates",
            "provider_config": {"group_by": "pid", "min_days": -9, "max_days": 9},
        },
    ],
    "mixed": [
        {"name": "pid", "strategy": "hash", "namespace": "pids"},
        {
            "name": "ssn",
            "strategy": "fpe",
            "namespace": "ssns",
            "provider_config": {"charset": "digits"},
        },
        {"name": "note", "strategy": "redact"},
        {
            "name": "d",
            "strategy": "date_shift",
            "namespace": "dates",
            "provider_config": {"preserve_values": ["2010-01-01"], "date_format": "%Y-%m-%d"},
        },
    ],
}


def _digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def capture(tmp: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for name, columns in CASES.items():
        src_path = tmp / f"{name}.parquet"
        pq.write_table(_SOURCE, src_path)
        cfg = PipelineConfig.model_validate(
            {
                "version": 2,
                "global_settings": {"seed": 42},
                "sources": {"t": {"type": "file", "format": "parquet", "path": str(src_path)}},
                "tables": [{"name": "t", "columns": columns}],
                "targets": {
                    "t": {"type": "file", "format": "parquet", "path": str(tmp / "o.parquet")}
                },
            }
        ).model_dump()
        plan = compile_plan(cfg, profile_source(cfg, seed=42), decoy_engine_version="pin")
        plan_text = plan_to_yaml(plan).replace(str(tmp), "<tmp>")
        result = run_pipeline(
            cfg,
            sources={"t": _SOURCE},
            engine_version="pin",
            key_provider=THROWAWAY_KEY_PROVIDER,
        )
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, result.outputs["t"].schema) as writer:
            writer.write_table(result.outputs["t"])
        metrics = {k: v for k, v in result.quality_metrics.items() if k != "execution"}
        out[name] = {
            "plan": _digest(plan_text),
            "checks_passed": _digest(json.dumps(list(plan.plan_compile.checks_passed))),
            "output": _digest(sink.getvalue().to_pybytes()),
            "metrics": _digest(json.dumps(metrics, sort_keys=True, default=str)),
        }
    return out


if __name__ == "__main__":
    import sys
    import tempfile

    with tempfile.TemporaryDirectory(dir=sys.argv[1] if len(sys.argv) > 1 else None) as d:
        print(json.dumps(capture(Path(d)), indent=2, sort_keys=True))
