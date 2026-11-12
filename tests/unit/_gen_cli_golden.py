"""Golden digests for GEN-CLI E1: config hash and per-table bytes for fixed configs.

`capture()` uses only APIs that exist on engine `86a28222`, so the checked-in golden
(`tests/fixtures/gen_cli/golden.json`) was captured on that base with:

    $PY -m tests.unit._gen_cli_golden > tests/fixtures/gen_cli/golden.json

and the tests recompute it on the head and compare. The snapshot file is the checked-in
`tests/fixtures/gen_cli/snap.json`, copied beside the working directory so the config text
(`snapshot_file: snap.json`) is identical on both builds.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gen_cli"


def stat_config() -> dict[str, Any]:
    """One statistical column over the checked-in snapshot; relative `snapshot_file`."""
    return {
        "version": 2,
        "global_settings": {"seed": 42, "fidelity_warn_threshold": 0.99},
        "tables": [
            {
                "name": "synthetic",
                "row_count": 300,
                "generate_columns": [
                    {"name": "amount", "type": "statistical", "snapshot_file": "snap.json"}
                ],
            }
        ],
    }


def graph_config() -> dict[str, Any]:
    """AT-28 graph: `c` references p2 then p1, `d` references c then p1; declared p1, p2, c, d."""

    def seq(name: str) -> dict[str, Any]:
        return {"name": name, "type": "sequence", "start": 1}

    def ref(name: str, table: str) -> dict[str, Any]:
        return {
            "name": name,
            "type": "reference",
            "reference_table": table,
            "reference_column": "id",
        }

    return {
        "version": 2,
        "global_settings": {"seed": 7},
        "tables": [
            {"name": "p1", "row_count": 5, "generate_columns": [seq("id")]},
            {"name": "p2", "row_count": 4, "generate_columns": [seq("id")]},
            {
                "name": "c",
                "row_count": 12,
                "generate_columns": [seq("id"), ref("p2_id", "p2"), ref("p1_id", "p1")],
            },
            {
                "name": "d",
                "row_count": 20,
                "generate_columns": [seq("id"), ref("c_id", "c"), ref("p1_id", "p1")],
            },
        ],
    }


def table_digest(table: Any) -> str:
    rows = json.dumps(table.to_pylist(), sort_keys=True, default=str)
    return hashlib.sha256(rows.encode()).hexdigest()


def digests(outputs: dict[str, Any]) -> dict[str, str]:
    return {name: table_digest(outputs[name]) for name in sorted(outputs)}


def capture() -> dict[str, Any]:
    from decoy_engine.generation.synthesize import generate_tables
    from decoy_engine.plan import compile_plan
    from decoy_engine.plan._compile import _hash_config
    from tests.unit._dps_helpers import empty_profile

    result: dict[str, Any] = {
        "engine_base": "86a28222",
        "command": "python -m tests.unit._gen_cli_golden",
    }
    previous = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        shutil.copy(FIXTURES / "snap.json", Path(tmp) / "snap.json")
        os.chdir(tmp)
        try:
            for label, config in (("statistical", stat_config()), ("graph", graph_config())):
                plan = compile_plan(config, empty_profile(), decoy_engine_version="test")
                result[label] = {
                    "config": config,
                    "config_hash": _hash_config(config),
                    "tables": digests(generate_tables(plan)),
                }
        finally:
            os.chdir(previous)
    return result


def make_snapshot() -> dict[str, Any]:
    """The source of `snap.json`: a seeded normal column, fitted with the engine's snapshot code."""
    import numpy as np
    import pandas as pd

    from decoy_engine.quality.snapshot import compute_distribution_snapshot

    rng = np.random.default_rng(11)
    frame = pd.DataFrame({"amount": rng.normal(100.0, 25.0, size=2000).round(2)})
    return compute_distribution_snapshot(frame)


if __name__ == "__main__":
    out = make_snapshot() if sys.argv[1:] == ["snapshot"] else capture()
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
