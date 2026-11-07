"""Configs and sources for the Avro/ORC dark tests, shared with the script that captured the
expected values from main `b5b1ad16` (so both sides build byte-identical inputs)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

ROWS = 40
PLACEHOLDER = "<SRC>"


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": [str(1000 + i) for i in range(ROWS)],
            "name": [f"name{i:03d}" for i in range(ROWS)],
            "city": [["oslo", "rome", "lima"][i % 3] for i in range(ROWS)],
        }
    )


def write_sources(root: Path) -> None:
    df = frame()
    df.to_csv(root / "in.csv", index=False)
    df.to_parquet(root / "in.parquet", index=False)
    with open(root / "in.fw", "w", encoding="utf-8", newline="") as fh:
        for row in df.itertuples(index=False):
            fh.write(f"{row.id:<6}{row.name:<10}{row.city:<6}\n")


_LAYOUT = {
    "columns": [
        {"name": "id", "start": 0, "width": 6},
        {"name": "name", "start": 6, "width": 10},
        {"name": "city", "start": 16, "width": 6},
    ]
}


def configs(root: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for fmt, name in (("csv", "in.csv"), ("parquet", "in.parquet"), ("fixed_width", "in.fw")):
        source: dict[str, Any] = {"type": "file", "format": fmt, "path": str(root / name)}
        target: dict[str, Any] = {"type": "file", "format": fmt, "path": str(root / f"out.{fmt}")}
        if fmt == "fixed_width":
            source["layout"] = _LAYOUT
            target["layout"] = _LAYOUT
        out[fmt] = {
            "version": 2,
            "global_settings": {"seed": 7},
            "sources": {"people": source},
            "tables": [
                {
                    "name": "people",
                    "columns": [
                        {"name": "name", "strategy": "redact"},
                        {"name": "city", "strategy": "passthrough"},
                    ],
                }
            ],
            "targets": {"people": target},
        }
    return out


def scrub(text: str, root: Path) -> str:
    return text.replace(str(root), PLACEHOLDER)


def capture(root: Path) -> dict[str, Any]:
    """Everything the dark tests compare, from whichever engine is importable."""
    from decoy_engine.config import PipelineConfig
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._serialize import plan_to_yaml
    from decoy_engine.profile import profile_hash, profile_source
    from decoy_engine.run_result import RunResult

    write_sources(root)
    result: dict[str, Any] = {"configs": {}, "profiles": {}, "plans": {}}
    for fmt, raw in configs(root).items():
        cfg = PipelineConfig.model_validate(raw).model_dump()
        result["configs"][fmt] = scrub(json.dumps(cfg, sort_keys=True, default=str), root)
        profile = profile_source(cfg, seed=7)
        result["profiles"][fmt] = profile_hash(profile)
        plan = compile_plan(cfg, profile, decoy_engine_version="0.0.0")
        result["plans"][fmt] = scrub(plan_to_yaml(plan), root)
    result["config_schema"] = PipelineConfig.model_json_schema()
    result["run_result_schema"] = RunResult.model_json_schema()
    return result
