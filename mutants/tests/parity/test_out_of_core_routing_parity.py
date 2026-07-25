"""SC2 part 2 parity: `run_pipeline` out-of-core AUTO-ROUTING vs the oracle.

`tests/parity/test_out_of_core_fk_parity.py` pins that `run_fk_out_of_core`
matches the pandas oracle for every gate-admitted plan. This file pins the
LAYER ABOVE it: that `run_pipeline`'s SC2 routing actually DISPATCHES an
eligible large FK job to that route (not merely gate-admits it), that the
dispatched output is byte-identical to the full-frame path a caller would
otherwise have taken, and that an ineligible-large FK job is REJECTED before
the read/mask step instead of silently OOM-ing full-frame.

TB-5: byte-estimate routing is the DEFAULT now, so these tests drive it
directly -- a tight `out_of_core_budget_bytes` (standing in for a cgroup-limited
container) makes the byte estimate exceed budget so a tiny fixture routes the
same bounded path a multi-million-row job would, without materializing the data.
`use_probe_routing=False` keeps the assertions off the probe subprocess (the
probe is covered in `test_probe.py`); the byte-estimate bounded dispatch and
its full-frame parity are what this file pins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline import run_pipeline

_N = 40


def _src(tmp_path: Path, table: pa.Table, name: str) -> str:
    p = tmp_path / f"{name}.parquet"
    pq.write_table(table, p)
    return str(p)


def _supported_fk_config(tmp_path: Path, *, orphans: bool) -> dict[str, Any]:
    """Pure-mask FK chain (parent -> child -> grandchild) whose every strategy
    is out-of-core-supported (hash keys, redact/truncate payloads). With
    `orphans=True` some child keys reference no parent, exercising the
    PRESERVE-orphan path through the routed dispatch."""
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(_N)], type=pa.string()),
            "note": pa.array([f"secret-{i}" for i in range(_N)], type=pa.string()),
        }
    )
    child_fk = [(f"orphan{i}" if (orphans and i % 7 == 0) else f"p{i}") for i in range(_N)]
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
            "pid": pa.array(child_fk, type=pa.string()),
            "code": pa.array([f"CODE{i:03d}" for i in range(_N)], type=pa.string()),
        }
    )
    grandchild = pa.table(
        {
            "gid": pa.array([f"g{i}" for i in range(_N)], type=pa.string()),
            "cfk": pa.array([f"c{i}" for i in range(_N)], type=pa.string()),
        }
    )
    tables = {"parent": parent, "child": child, "grandchild": grandchild}
    return {
        "version": 1,
        "global_settings": {"job_name": "sc2-parity", "seed": 11},
        "sources": {
            name: {"type": "file", "path": _src(tmp_path, tbl, name), "format": "parquet"}
            for name, tbl in tables.items()
        },
        "targets": {
            name: {
                "type": "file",
                "path": str(tmp_path / f"{name}.out.parquet"),
                "format": "parquet",
            }
            for name in tables
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    {"name": "id", "strategy": "hash", "namespace": "pns"},
                    {"name": "note", "strategy": "redact"},
                ],
            },
            {
                "name": "child",
                "columns": [
                    {"name": "cid", "strategy": "hash", "namespace": "cns"},
                    {"name": "pid", "strategy": "hash", "namespace": "pns"},
                    {"name": "code", "strategy": "truncate", "provider_config": {"length": 4}},
                ],
            },
            {
                "name": "grandchild",
                "columns": [{"name": "cfk", "strategy": "hash", "namespace": "cns"}],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "pns",
            },
            {
                "parent": {"table": "child", "columns": ["cid"]},
                "children": [{"table": "grandchild", "columns": ["cfk"]}],
                "orphan_policy": "preserve",
                "namespace": "cns",
            },
        ],
    }


def _sources(config: dict[str, Any]) -> dict[str, pa.Table]:
    return {name: pq.read_table(spec["path"]) for name, spec in config["sources"].items()}


def _pydict(outputs: dict[str, pa.Table]) -> dict[str, dict[str, list[Any]]]:
    return {t: tbl.to_pydict() for t, tbl in outputs.items()}


_TIGHT_BUDGET = 64 * 1024 * 1024  # resolve_budget's floor; below the ~200 MB intercept


@pytest.mark.parametrize("orphans", [False, True])
def test_auto_route_out_of_core_matches_full_frame_oracle(tmp_path: Path, orphans: bool) -> None:
    config = _supported_fk_config(tmp_path, orphans=orphans)
    sources = _sources(config)

    oracle = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
    # Default byte-estimate routing under a tight budget: the estimate exceeds
    # the 64 MB budget, and the job is out-of-core-eligible (hash/redact/truncate),
    # so it routes the bounded out_of_core path by BYTES, not row count.
    routed = run_pipeline(
        config,
        sources,
        engine_version="0.1.0",
        out_of_core_budget_bytes=_TIGHT_BUDGET,
        use_probe_routing=False,
    )

    # The job was really dispatched to out-of-core (not just admitted), keyed
    # off computed bytes vs. budget.
    assert routed.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    assert (
        routed.quality_metrics["execution"]["route_reason"] == "byte_estimate_bounded_out_of_core"
    )
    # ...and its output is value-identical to the full-frame path.
    assert set(routed.outputs) == set(oracle.outputs) == {"parent", "child", "grandchild"}
    assert _pydict(routed.outputs) == _pydict(oracle.outputs)


def test_forced_out_of_core_matches_full_frame_oracle(tmp_path: Path) -> None:
    config = _supported_fk_config(tmp_path, orphans=True)
    sources = _sources(config)
    oracle = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="full_frame")
    forced = run_pipeline(config, sources, engine_version="0.1.0", execution_mode="out_of_core")
    assert forced.quality_metrics["execution"]["execution_mode"] == "out_of_core"
    assert _pydict(forced.outputs) == _pydict(oracle.outputs)


def test_ineligible_byte_over_budget_fk_job_rejected_before_read_not_executed(
    tmp_path: Path,
) -> None:
    """The irreducible reject class, now BYTE-based (TB-5 contract migration):
    an FK job that no bounded route can take, whose computed bytes exceed the
    budget, must be rejected BEFORE read -- never executed to a silent
    full-frame OOM. `fidelity_report=True` disqualifies the sequential route
    (it needs every masked output resident at once) and is likewise not carried
    by the streaming out-of-core route, so no bounded route applies; when the
    byte estimate also does not confirm full_frame fits the budget, that is a
    reject, not a run. The out-of-core compat gate still ADMITS the FK
    structure (hash edges) -- proving the reject does not depend on the gate
    declining, only on no bounded route being able to run the job."""
    config = _supported_fk_config(tmp_path, orphans=False)
    sources = _sources(config)

    # Tight budget: the byte estimate exceeds it, and no bounded route applies
    # (fidelity_report bars sequential; out_of_core can't carry it) -> reject.
    with pytest.raises(ExecutionError) as exc:
        run_pipeline(
            config,
            sources,
            engine_version="0.1.0",
            fidelity_report=True,
            out_of_core_budget_bytes=_TIGHT_BUDGET,
            use_probe_routing=False,
        )
    assert exc.value.code == "fk_full_frame_oom_risk_rejected"

    # Under a generous budget the SAME job runs full-frame (no regression):
    # proves the reject is a byte-size gate, not a hard ban on the recipe.
    ran = run_pipeline(
        config,
        sources,
        engine_version="0.1.0",
        fidelity_report=True,
        out_of_core_budget_bytes=4 * 1024 * 1024 * 1024,
    )
    assert ran.quality_metrics["execution"]["execution_mode"] == "full_frame"
