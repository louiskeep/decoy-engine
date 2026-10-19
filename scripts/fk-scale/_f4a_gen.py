"""Representative generation plan, output digest and backend probe for the F4a capacity bench.

Plan: docs/plans/2026-10-16-phase-f-program.md section F4 prerequisite (a). Shared by
`bench_worker_gen.py` (pure generation) and `bench_worker_mixed.py` (generate then mask) so both
tiers exercise the same eligible column families.

Why the backend probe is a separate small run: the F0 witness keeps every generated value to prove
the column holds what the backend returned, so it cannot ride a streamed 10M run without defeating
the memory measurement. Backend choice is decided by the plan (column kind, and the pooled-faker
row threshold on the TABLE row count), not by how many rows follow, so the probe runs the SAME
table definitions at a row count above that threshold and the record states this explicitly.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

PROBE_ROWS = 100_000  # above the pooled-faker threshold (50_000), small enough to hold resident
SEED = 20261018
ANCHOR = "dob"
ANCHOR_DATES = [f"{1960 + 5 * i}-{1 + i % 12:02d}-{1 + 2 * i % 28:02d}" for i in range(12)]


def _null_masked_columns() -> list[dict[str, Any]]:
    return [
        {
            "name": "grade",
            "type": "categorical",
            "categories": ["x", "y"],
            "weights": [3, 1],
            "null_probability": 0.1,
        }
    ]


def anchored_columns(*, null_mask: bool = True) -> list[dict[str, Any]]:
    """Families usable on any generated table (they need no sibling table).

    `null_mask=False` drops the null-masked column: the mixed route declines a generated parent
    that holds nulls (`generation-shape-unsupported`, F2 decline-lifting scope) and would fall
    to the resident full-frame continuation, which is not a bounded run."""
    return [
        {"name": "status", "type": "categorical", "categories": ["a", "b", "c"]},
        *(_null_masked_columns() if null_mask else []),
        {"name": "city", "type": "faker", "faker_type": "city"},
        # A categorical ISO-date anchor, not a `date_of_birth` faker: that faker is not
        # pool-eligible and runs the live per-row provider (about 14k rows/s measured), which would
        # turn a generation-capacity bench into a Faker-speed bench.
        {"name": ANCHOR, "type": "categorical", "categories": ANCHOR_DATES},
        {
            "name": "event_date",
            "type": "windowed_date",
            "anchor": ANCHOR,
            "min_days": 1,
            "max_days": 365,
        },
    ]


def rich_parent_columns() -> list[dict[str, Any]]:
    """Extra generated columns for the mixed job's parent (its key column is added by the caller)."""
    return anchored_columns(null_mask=False)


def mixed_parent_config(parent_rows: int) -> dict[str, Any]:
    """The mixed job's rich generated parent as a standalone generate config, for the probe."""
    from _mixed_layout import PARENT, parent_columns

    return {
        "version": 1,
        "global_settings": {"seed": SEED},
        "sources": {},
        "targets": {},
        "tables": [
            {
                "name": PARENT,
                "row_count": parent_rows,
                "generate_columns": parent_columns(True),
            }
        ],
    }


def representative_config(rows: int, parent_rows: int) -> dict[str, Any]:
    """A two-table plan covering sequence, categorical, null_mask, pooled faker, reference and
    windowed_date. `parent_rows` stays fixed across tiers so the reference key pool does not
    scale with the tier."""
    return {
        "version": 1,
        "global_settings": {"seed": SEED},
        "sources": {},
        "targets": {},
        "tables": [
            {
                "name": "orders",
                "row_count": rows,
                "generate_columns": [
                    {"name": "oid", "type": "sequence", "start": 1, "prefix": "O"},
                    {
                        "name": "pid",
                        "type": "reference",
                        "reference_table": "people",
                        "reference_column": "id",
                        "distribution": "random",
                    },
                    *anchored_columns(),
                ],
            },
            {
                "name": "people",
                "row_count": parent_rows,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 1, "prefix": "P", "pad_length": 8},
                    {"name": "city", "type": "faker", "faker_type": "city"},
                ],
            },
        ],
    }


def committed_digest(committed: Path) -> dict[str, Any]:
    """sha256 over the committed Parquet content, per file, in stable order.

    Hashes Arrow IPC bytes of each row group rather than the file bytes, so a Parquet
    writer metadata change cannot register as a data change. Group boundaries come from the
    sink's batch size, which is a deterministic function of the run parameters, so two runs of
    one configuration must agree. The digest is layout-bound: it compares reruns, not layouts.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    per_file: dict[str, str] = {}
    for path in sorted(committed.glob("*.parquet")):
        h = hashlib.sha256()
        pf = pq.ParquetFile(path, pre_buffer=False)
        h.update(str(pf.schema_arrow).encode())
        # One row group at a time: `iter_batches` over a 10M-row file measurably raised the
        # worker's RSS by about 230 MB, which would have been charged to the generation run.
        for i in range(pf.num_row_groups):
            batch = pf.read_row_group(i, use_threads=False)
            sink = pa.BufferOutputStream()
            with pa.ipc.new_stream(sink, batch.schema) as writer:
                writer.write_table(batch)
            h.update(sink.getvalue())
        per_file[path.stem] = h.hexdigest()
    combined = hashlib.sha256(
        "".join(f"{k}:{v}" for k, v in sorted(per_file.items())).encode()
    ).hexdigest()
    return {"combined": combined, "per_table": per_file}


# Backends the witness can label, and whether the label is the compiled Rust companion.
_RUST_LABELS = frozenset({"compiled_index_selection"})


def probe_backends(config: dict[str, Any]) -> dict[str, Any]:
    """Report each column's executed backend: the F0 witness for every witnessed kind.

    windowed_date has no selection backend and its stream needs the sibling anchor batch, which the
    witness's per-stream wrapper cannot supply. It is therefore excluded from the witnessed run and
    reported from a second, unwitnessed run of the full plan: admission-matrix row plus a check
    that the column is populated. It is never labelled as a selection backend.
    """
    import copy
    import json
    from datetime import datetime, timezone

    import pytest
    from tests.parity import _f0_witness as w

    from decoy_engine.generation._admission import ADMISSION_MATRIX, classify_plan
    from decoy_engine.generation._plan_entry import generate_tables
    from decoy_engine.plan import compile_plan
    from decoy_engine.profile import Profile

    profile = Profile(
        schema_version=1,
        tables=(),
        relationships=(),
        profiled_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        decoy_engine_version="f4a-probe",
    )

    def plan_of(cfg: dict[str, Any]) -> Any:
        return compile_plan(cfg, profile, decoy_engine_version="f4a-probe")

    witnessed_cfg = copy.deepcopy(config)
    for table in witnessed_cfg["tables"]:
        table["generate_columns"] = [
            c for c in table["generate_columns"] if c["type"] != "windowed_date"
        ]
    columns: list[dict[str, Any]] = []
    with pytest.MonkeyPatch.context() as mp, w.end_to_end(mp) as witness:
        tables = generate_tables(plan_of(witnessed_cfg))
    for table, tbl in tables.items():
        for name in tbl.column_names:
            key = (table, name)
            report = witness._classify_one(key, tbl.column(name).to_pylist())
            columns.append(
                {
                    "table": table,
                    "column": name,
                    "kind": witness.kinds[key],
                    "selection": sorted(report.selection),
                    "subpaths": sorted(report.subpaths),
                    "evidence": report.evidence,
                    "rust_native": bool(report.selection & _RUST_LABELS),
                }
            )

    full_plan = plan_of(config)
    full = generate_tables(full_plan)
    admitted, why = ADMISSION_MATRIX["windowed_date"]
    for spec in config["tables"]:
        for col in spec["generate_columns"]:
            if col["type"] != "windowed_date":
                continue
            values = full[spec["name"]].column(col["name"])
            columns.append(
                {
                    "table": spec["name"],
                    "column": col["name"],
                    "kind": "windowed_date",
                    "selection": [],
                    "subpaths": [],
                    "evidence": "unwitnessed",
                    "executed": "shared apply_windowed_date (outside the F0 selection taxonomy)",
                    "admitted_to_core": admitted,
                    "admission_note": why,
                    "populated_rows": len(values) - values.null_count,
                    "rust_native": False,
                }
            )
    core_config = json.loads(full_plan.generation.config_json)
    return {
        "probe_rows": {t: v.num_rows for t, v in full.items()},
        "core_eligible": bool(classify_plan(core_config).eligible),
        "columns": columns,
    }


def digest_in_subprocess(committed: Path) -> dict[str, Any] | None:
    """Digest the committed output in its own process, after the measured run has ended.

    Reading a 10M-row Parquet file back raised the generating worker's RSS by hundreds of MB, and
    `wait4` charges a reaped child to its parent, so the read-back cannot live in the measured
    worker or in a child of it. This process is a sibling of the worker under the harness, so its
    memory is not part of the capacity figure.
    """
    import json
    import subprocess
    import sys

    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(Path(__file__).resolve()), "--digest", str(committed)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    out = json.loads(proc.stdout)
    return out if isinstance(out, dict) else None


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) != 3 or sys.argv[1] != "--digest":
        raise SystemExit("usage: _f4a_gen.py --digest <committed-dir>")
    print(json.dumps(committed_digest(Path(sys.argv[2]))))
