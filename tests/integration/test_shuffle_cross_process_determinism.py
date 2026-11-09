"""QA row 24d: the same config, seed and mask secret give byte-identical output in fresh processes.

QA saw a `visits` table masked with plain `strategy: shuffle` differ between two runs while the
hash columns matched. Each run here is its own interpreter with a different PYTHONHASHSEED, so
set ordering and per-process RNG state cannot hide behind one warm process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

CHILD = Path(__file__).with_name("_shuffle_det_child.py")
SRC = Path(__file__).resolve().parents[2] / "src"


def _config(out: Path, diagnosis: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 42},
        "sources": {
            t: {"type": "file", "path": str(out / f"{t}.src.csv"), "format": "csv"}
            for t in ("patients", "visits")
        },
        "targets": {
            t: {"type": "file", "path": str(out / f"{t}.csv"), "format": "csv"}
            for t in ("patients", "visits")
        },
        "tables": [
            {
                "name": "patients",
                "columns": [
                    {"name": "patient_id", "strategy": "hash", "namespace": "patient"},
                    {"name": "full_name", "strategy": "redact"},
                ],
            },
            {
                "name": "visits",
                "columns": [
                    {"name": "patient_id", "strategy": "hash", "namespace": "patient"},
                    {"name": "diagnosis", **diagnosis},
                ],
            },
        ],
        "relationships": [],
    }


def _write_sources(out: Path) -> None:
    """The QA shape: 10 patients, 20 visits keyed by patient_id, diagnosis is the shuffle column."""
    patients = ["patient_id,full_name"] + [f"{i},Person {i}" for i in range(1, 11)]
    diagnoses = ["flu", "cold", "asthma", "diabetes", "none"]
    visits = ["patient_id,diagnosis"] + [f"{1 + i % 10},{diagnoses[i % 5]}" for i in range(20)]
    (out / "patients.src.csv").write_text("\n".join(patients) + "\n", encoding="utf-8")
    (out / "visits.src.csv").write_text("\n".join(visits) + "\n", encoding="utf-8")


def _run_child(cfg_path: Path, hash_seed: str) -> dict[str, str]:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(SRC)}
    proc = subprocess.run(  # noqa: S603 -- fixed argv, own interpreter
        [sys.executable, str(CHILD), str(cfg_path)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "diagnosis",
    [
        pytest.param({"strategy": "shuffle", "namespace": "diagnosis"}, id="default-mode"),
        pytest.param({"strategy": "shuffle"}, id="no-namespace"),
        pytest.param(
            {"strategy": "shuffle", "namespace": "diagnosis", "deterministic": True},
            id="deterministic",
        ),
    ],
)
def test_shuffle_output_identical_across_processes(
    tmp_path: Path, diagnosis: dict[str, Any]
) -> None:
    _write_sources(tmp_path)
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(_config(tmp_path, diagnosis)), encoding="utf-8")
    runs = [_run_child(cfg_path, seed) for seed in ("1", "977", "31337")]
    assert runs[0] == runs[1] == runs[2]
    assert sorted(runs[0]) == ["patients", "visits"]
