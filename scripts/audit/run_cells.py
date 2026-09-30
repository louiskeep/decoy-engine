#!/usr/bin/env python3
"""Stage 2a runner: executes the first evidence-audit batch, one cell per
fresh subprocess, and appends each cell's JSON record to runs.jsonl.

Per docs/plans/2026-09-30-rust-coverage-evidence-audit.md's devbox-memory
rule, cells run STRICTLY sequentially (no parallel probe runs); each cell
gets its own interpreter via subprocess.run so a leaked allocation in one
cell cannot inflate the next cell's VmHWM reading.

Usage:
    /dev/shm/audit-2026-09-30/venv/bin/python scripts/audit/run_cells.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE = REPO_ROOT / "scripts" / "audit" / "probe.py"
# Fixed path by design: the dedicated stage 2a audit venv, built once per
# docs/plans/2026-09-30-rust-coverage-evidence-audit.md's step 1, not a
# general "insecure temp file" pattern.
VENV_PYTHON = "/dev/shm/audit-2026-09-30/venv/bin/python"  # noqa: S108
RUNS_LOG = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "runs.jsonl"


def _cell(id_, kind, description, ledger_ids, **kw):
    return {"id": id_, "kind": kind, "description": description, "ledger_ids": ledger_ids, **kw}


# ---------------------------------------------------------------------------
# Stage 2a batch: engine-direct entry point only, real data, local Parquet
# unless stated. See the plan's step 3 for the exact batch definition.
# ---------------------------------------------------------------------------

CELLS = [
    # -- one column per strategy, 10k rows, Parquet --
    _cell(
        "R001_hash_10k",
        "single_strategy",
        "single-table hash mask, 10k rows, Parquet",
        ["B001", "B023", "B024", "B026", "B027", "B028", "B035", "B039", "B040"],
        strategy="hash",
        rows=10_000,
    ),
    _cell(
        "R002_categorical_det_true_10k",
        "single_strategy",
        "single-table categorical (deterministic=True) mask, 10k rows",
        ["B028", "B035", "B039"],
        strategy="categorical_det_true",
        rows=10_000,
    ),
    _cell(
        "R003_categorical_det_false_10k",
        "single_strategy",
        "single-table categorical (deterministic=False) mask, 10k rows",
        ["B026", "B028"],
        strategy="categorical_det_false",
        rows=10_000,
    ),
    _cell(
        "R004_bucket_perturb_10k",
        "single_strategy",
        "single-table bucket_perturb mask, 10k rows",
        ["B028", "B035", "B039"],
        strategy="bucket_perturb",
        rows=10_000,
    ),
    _cell(
        "R005_group_key_10k",
        "single_strategy",
        "single-table group_key mask (with passthrough group_by sibling), 10k rows",
        ["B028", "B031", "B035", "B039"],
        strategy="group_key",
        rows=10_000,
    ),
    _cell(
        "R006_date_shift_10k",
        "single_strategy",
        "single-table date_shift mask, 10k rows",
        ["B028", "B029", "B035", "B039"],
        strategy="date_shift",
        rows=10_000,
    ),
    _cell(
        "R007_redact_10k",
        "single_strategy",
        "single-table redact mask, 10k rows",
        ["B028", "B035"],
        strategy="redact",
        rows=10_000,
    ),
    _cell(
        "R008_truncate_10k",
        "single_strategy",
        "single-table truncate mask, 10k rows",
        ["B028", "B035"],
        strategy="truncate",
        rows=10_000,
    ),
    _cell(
        "R009_passthrough_10k",
        "single_strategy",
        "single-table passthrough mask, 10k rows",
        ["B028", "B035"],
        strategy="passthrough",
        rows=10_000,
    ),
    _cell(
        "R010_faker_pooled_10k",
        "single_strategy",
        "single-table pooled faker mask, 10k rows",
        ["B028"],
        strategy="faker_pooled",
        rows=10_000,
    ),
    _cell(
        "R011_fpe_10k",
        "single_strategy",
        "single-table FPE (FF1) mask, 10k rows",
        ["B028"],
        strategy="fpe",
        rows=10_000,
    ),
    _cell(
        "R012_text_mask_10k",
        "single_strategy",
        "single-table text_mask mask, 10k rows",
        ["B028"],
        strategy="text_mask",
        rows=10_000,
    ),
    # -- all-native mix, 10k rows, Parquet --
    _cell(
        "R013_mix_native_10k_parquet",
        "mix",
        "all-native mix (hash+redact+truncate+passthrough), 10k rows, Parquet",
        ["B001", "B028", "B035", "B039"],
        rows=10_000,
    ),
    _cell(
        "R014_mix_native_plus_faker_10k_parquet",
        "mix_faker",
        "all-native mix plus one faker column, 10k rows, Parquet (fallout check)",
        ["B028"],
        rows=10_000,
    ),
    # -- format widening: CSV and fixed_width --
    _cell(
        "R015_mix_native_10k_csv",
        "mix",
        "all-native mix, 10k rows, CSV (pandas read_csv(dtype=str) -> Arrow)",
        ["B008", "B028", "B035", "B039"],
        rows=10_000,
        source_format="csv",
    ),
    _cell(
        "R016_mix_native_10k_fixed_width",
        "mix",
        "all-native mix, 10k rows, fixed_width (decoy_engine.profile._fixed_width_reader.read_fixed_width)",
        ["B008", "B028", "B035", "B039"],
        rows=10_000,
        source_format="fixed_width",
    ),
    # -- when gate --
    _cell(
        "R017_mix_native_10k_when_gate",
        "mix_when",
        "all-native mix with a `when` gate on the hash column, 10k rows",
        ["B017"],
        rows=10_000,
    ),
    # -- hash-only size / auto-chunk knob matrix --
    _cell(
        "R018_hash_only_50k_default",
        "hash_size",
        "hash-only, 50k rows, default routing knobs",
        ["B054"],
        rows=50_000,
    ),
    _cell(
        "R019_hash_only_150k_default",
        "hash_size",
        "hash-only, 150k rows, default routing knobs (expect chunked)",
        ["B001", "B072"],
        rows=150_000,
    ),
    _cell(
        "R020_hash_only_150k_no_chunk",
        "hash_size",
        "hash-only, 150k rows, auto_chunk=False",
        ["B028", "B035", "B039"],
        rows=150_000,
        run_kwargs={"auto_chunk": False},
    ),
    _cell(
        "R021_hash_only_1M_no_chunk",
        "hash_size",
        "hash-only, 1,000,000 rows, auto_chunk=False",
        ["B028", "B035", "B039"],
        rows=1_000_000,
        run_kwargs={"auto_chunk": False},
    ),
    _cell(
        "R022_hash_only_1M_default",
        "hash_size",
        "hash-only, 1,000,000 rows, default routing knobs",
        ["B001", "B072"],
        rows=1_000_000,
    ),
    # -- generate / mask+generate --
    _cell(
        "R023_generate_only_10k",
        "generate_only",
        "single-table generate (one Faker generate column), 10k rows",
        [],
        rows=10_000,
    ),
    _cell(
        "R024_mask_plus_generate_10k",
        "mask_generate",
        "mask table (hash) + independent generate table, 10k rows each",
        ["B007"],
        rows=10_000,
    ),
]


def main() -> None:
    RUNS_LOG.parent.mkdir(parents=True, exist_ok=True)
    total = len(CELLS)
    for i, spec in enumerate(CELLS, start=1):
        print(f"[{i}/{total}] running {spec['id']} ...", file=sys.stderr)
        t0 = time.time()
        # Trusted input: VENV_PYTHON and PROBE are this file's own constants,
        # and spec comes from the CELLS list defined above in this same
        # file, not from an external or user-supplied source.
        proc = subprocess.run(  # noqa: S603
            [VENV_PYTHON, str(PROBE), json.dumps(spec)],
            capture_output=True,
            text=True,
        )
        dt = time.time() - t0
        if proc.returncode != 0:
            print(
                f"[{i}/{total}] {spec['id']} FAILED (exit {proc.returncode}) after {dt:.1f}s\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}",
                file=sys.stderr,
            )
            record = {
                "cell_id": spec["id"],
                "description": spec.get("description"),
                "kind": spec.get("kind"),
                "ledger_ids": spec.get("ledger_ids", []),
                "error": True,
                "returncode": proc.returncode,
                "stderr_tail": proc.stderr[-4000:],
                "wall_seconds": round(dt, 4),
            }
        else:
            line = proc.stdout.strip().splitlines()[-1]
            record = json.loads(line)
            print(
                f"[{i}/{total}] {spec['id']} OK in {dt:.1f}s "
                f"backend={record.get('overall_backend')} "
                f"vmhwm_kb={record.get('peak_memory_vmhwm_kb')}",
                file=sys.stderr,
            )
        with open(RUNS_LOG, "a") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
