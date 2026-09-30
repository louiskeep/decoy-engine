#!/usr/bin/env python3
"""Stage 2a evidence-audit probe: runs ONE cell in this fresh process.

Reads a single cell spec (a JSON object) from argv[1] or stdin, drives
`decoy_engine.execution.run_pipeline` (or, for the two "read via platform
readers" cells, builds the resident source through those readers first),
and emits ONE JSON line on stdout: the cell id and parameters, the ledger
branch ids it is meant to witness, the route/backend evidence read back
from the ExecutionResult, a parity check against the pandas oracle where a
non-pandas backend ran, wall time, this process's own peak RSS
(`/proc/self/status` `VmHWM`, read at the end of this run, in THIS
process -- never `resource.getrusage(RUSAGE_SELF).ru_maxrss`, which
survives `execve` and would blend in whatever ran before this interpreter
started), row count, and the engine/companion commit identity.

Run ONE cell per process (`run_cells.py` launches a fresh interpreter per
cell) so VmHWM reflects only this cell's own allocation, not a running
peak carried over from a previous cell in the same process.

Per docs/plans/2026-09-30-rust-coverage-evidence-audit.md: "Rust companion"
is claimed only when `compiled_kernel_executed=True`; passthrough/redact/
truncate are Arrow/Python native even when they ran inside the unified
slice with `executed=True`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_RECORD = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "environment.json"

# --- Backend classification per the plan's "What a cell records" section ---
# hash/categorical/bucket_perturb/group_key/date_shift are Rust-companion
# operators; they count as "Rust" ONLY when compiled_kernel_executed=True.
# passthrough/redact/truncate are Arrow/Python native kernels on the unified
# lane -- executed=True there is expected and is NOT a Rust claim.
_RUST_ELIGIBLE_STRATEGIES = {"hash", "categorical", "bucket_perturb", "group_key", "date_shift"}
_ARROW_NATIVE_STRATEGIES = {"passthrough", "redact", "truncate"}


def _vmhwm_kb() -> int | None:
    """This process's own peak RSS, read from /proc/self/status.

    Not `ru_maxrss`: that field survives `execve` and (per the plan) is not
    trusted as a per-job isolation boundary. Reading our own `/proc/self/status`
    inside this one-cell-per-process worker gives a peak that started at zero
    when this interpreter started.
    """
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    parts = line.split()
                    return int(parts[1])  # kB
    except OSError:
        return None
    return None


def _read_environment_record() -> dict[str, Any]:
    if not ENV_RECORD.exists():
        return {}
    with open(ENV_RECORD) as fh:
        return json.load(fh)


def _mk_tmp_dir() -> Path:
    # Fixed, non-guessable-attack-surface path by design: this is an audit
    # tool's own scratch space (never shipped in src/), scoped to a single
    # dated run, deleted by the operator when the run finishes per the
    # plan's devbox-memory rule. Not a general "insecure temp file" pattern.
    base = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108
    base.mkdir(parents=True, exist_ok=True)
    return base


# ---------------------------------------------------------------------------
# Per-strategy column-config + source-column builders. Each returns the
# masking column config dict(s) and the source pa.Array(s) needed, sized to
# `n` rows. Validated interactively against this worktree's HEAD before
# being wired into this module (see the audit session notes / stage-2a
# summary for the interactive checks).
# ---------------------------------------------------------------------------


def _strategy_hash(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col = {"name": "h", "strategy": "hash", "namespace": "ns_h"}
    src = {"h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string())}
    return [col], src


def _strategy_categorical(n: int, *, deterministic: bool) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col: dict[str, Any] = {
        "name": "c",
        "strategy": "categorical",
        "namespace": "ns_cat",
        "provider_config": {"categories": ["red", "green", "blue"]},
    }
    if deterministic:
        col["deterministic"] = True
    src = {"c": pa.array([["red", "green", "blue"][i % 3] for i in range(n)], type=pa.string())}
    return [col], src


def _strategy_bucket_perturb(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col = {
        "name": "d",
        "strategy": "bucket_perturb",
        "namespace": "ns_bp",
        "provider_config": {"bucket": "month", "date_format": "%Y-%m-%d"},
    }
    src = {"d": pa.array([f"2024-{(i % 12) + 1:02d}-01" for i in range(n)], type=pa.string())}
    return [col], src


def _strategy_date_shift(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col = {
        "name": "d",
        "strategy": "date_shift",
        "namespace": "ns_ds",
        "provider_config": {"date_format": "%Y-%m-%d"},
    }
    src = {"d": pa.array([f"2023-{(i % 12) + 1:02d}-15" for i in range(n)], type=pa.string())}
    return [col], src


def _strategy_group_key(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    cols: list[dict[str, Any]] = [
        {"name": "gb", "strategy": "passthrough"},
        {
            "name": "gk",
            "strategy": "group_key",
            "provider_config": {"group_by": "gb", "length": 16},
        },
    ]
    src = {
        "gb": pa.array([f"g{i % 4}" for i in range(n)], type=pa.string()),
        "gk": pa.array(["seed"] * n, type=pa.string()),
    }
    return cols, src


def _strategy_redact(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col = {"name": "r", "strategy": "redact"}
    src = {"r": pa.array([f"5{i % 900:03d}-11-2222" for i in range(n)], type=pa.string())}
    return [col], src


def _strategy_truncate(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col = {"name": "t", "strategy": "truncate", "provider_config": {"length": 4, "keep": "head"}}
    src = {"t": pa.array([f"4000{i % 9999:04d}" for i in range(n)], type=pa.string())}
    return [col], src


def _strategy_passthrough(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col = {"name": "p", "strategy": "passthrough"}
    src = {"p": pa.array(list(range(n)), type=pa.int64())}
    return [col], src


def _strategy_faker_pooled(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col = {
        "name": "nm",
        "strategy": "faker",
        "provider": "person_first_name",
        "deterministic": True,
        "namespace": "ns_faker",
        "pool_size": 30,
    }
    src = {"nm": pa.array([f"id-{i}" for i in range(n)], type=pa.string())}
    return [col], src


def _strategy_fpe(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col = {
        "name": "f",
        "strategy": "fpe",
        "deterministic": True,
        "namespace": "ns_fpe",
        "provider_config": {"charset": "digits"},
    }
    src = {"f": pa.array([f"{100000 + (i % 900000):06d}" for i in range(n)], type=pa.string())}
    return [col], src


def _strategy_text_mask(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    col = {
        "name": "cell",
        "strategy": "text_mask",
        "provider_config": {"detectors": ["ssn"], "unmatched_span_policy": "passthrough"},
    }
    rows = [
        f"call re acct 123-45-6789 thanks {i}" if i % 3 == 0 else f"plain filler row {i}"
        for i in range(n)
    ]
    src = {"cell": pa.array(rows, type=pa.string())}
    return [col], src


def _mix_columns_and_source(n: int) -> tuple[list[dict], dict]:
    import pyarrow as pa

    cols: list[dict[str, Any]] = [
        {"name": "h", "strategy": "hash", "namespace": "ns_h"},
        {"name": "r", "strategy": "redact"},
        {"name": "t", "strategy": "truncate", "provider_config": {"length": 4, "keep": "head"}},
        {"name": "p", "strategy": "passthrough"},
    ]
    src = {
        "h": pa.array([f"user{i}@example.com" for i in range(n)], type=pa.string()),
        "r": pa.array([f"5{i % 900:03d}-11-2222" for i in range(n)], type=pa.string()),
        "t": pa.array([f"4000{i % 9999:04d}" for i in range(n)], type=pa.string()),
        "p": pa.array(list(range(n)), type=pa.int64()),
    }
    return cols, src


_STRATEGY_BUILDERS = {
    "hash": lambda n: _strategy_hash(n),
    "categorical_det_true": lambda n: _strategy_categorical(n, deterministic=True),
    "categorical_det_false": lambda n: _strategy_categorical(n, deterministic=False),
    "bucket_perturb": lambda n: _strategy_bucket_perturb(n),
    "date_shift": lambda n: _strategy_date_shift(n),
    "group_key": lambda n: _strategy_group_key(n),
    "redact": lambda n: _strategy_redact(n),
    "truncate": lambda n: _strategy_truncate(n),
    "passthrough": lambda n: _strategy_passthrough(n),
    "faker_pooled": lambda n: _strategy_faker_pooled(n),
    "fpe": lambda n: _strategy_fpe(n),
    "text_mask": lambda n: _strategy_text_mask(n),
}


# ---------------------------------------------------------------------------
# Source materialization per format (task step 3's platform-reader
# instruction: CSV via pandas read_csv(dtype=str) then to Arrow; fixed_width
# via decoy_engine.profile._fixed_width_reader.read_fixed_width).
# ---------------------------------------------------------------------------


def _write_and_reload_parquet(tmp_dir: Path, name: str, table: Any) -> Any:
    import pyarrow.parquet as pq

    path = tmp_dir / f"{name}.parquet"
    pq.write_table(table, path)
    reloaded = pq.read_table(path)
    return reloaded, str(path)


def _write_and_reload_csv(tmp_dir: Path, name: str, table: Any) -> Any:
    import pandas as pd
    import pyarrow as pa

    path = tmp_dir / f"{name}.csv"
    table.to_pandas().to_csv(path, index=False)
    df = pd.read_csv(path, dtype=str)
    reloaded = pa.Table.from_pandas(df, preserve_index=False)
    return reloaded, str(path)


def _fixed_width_layout_for_mix(n: int) -> list[dict]:
    # widths sized generously for the mix's own value shapes (see
    # _mix_columns_and_source): email up to ~30 chars, ssn-like 11, a
    # truncated 8-char card fragment, and an int amount up to 6 digits.
    return [
        {"name": "h", "start": 0, "width": 32, "type": "str"},
        {"name": "r", "start": 32, "width": 12, "type": "str"},
        {"name": "t", "start": 44, "width": 8, "type": "str"},
        {"name": "p", "start": 52, "width": 8, "type": "int"},
    ]


def _render_fixed_width_row(layout_columns: list[dict], values: dict) -> str:
    width = max(c["start"] + c["width"] for c in layout_columns)
    chars = [" "] * width
    for c in layout_columns:
        raw = str(values[c["name"]])
        w = c["width"]
        if len(raw) > w:
            raise ValueError(f"{raw!r} does not fit width {w} for column {c['name']}")
        field = raw + " " * (w - len(raw))
        chars[c["start"] : c["start"] + w] = list(field)
    return "".join(chars)


def _write_and_reload_fixed_width(tmp_dir: Path, name: str, n: int) -> Any:
    import pyarrow as pa

    from decoy_engine.profile._fixed_width_reader import read_fixed_width

    layout_columns = _fixed_width_layout_for_mix(n)
    path = tmp_dir / f"{name}.fw.txt"
    with open(path, "w") as fh:
        for i in range(n):
            row = {
                "h": f"user{i}@example.com",
                "r": f"5{i % 900:03d}-11-2222",
                "t": f"4000{i % 9999:04d}"[:8],
                "p": i,
            }
            fh.write(_render_fixed_width_row(layout_columns, row) + "\n")
    df = read_fixed_width(str(path), {"columns": layout_columns})
    reloaded = pa.Table.from_pandas(df, preserve_index=False)
    return reloaded, str(path), layout_columns


# ---------------------------------------------------------------------------
# Config assembly
# ---------------------------------------------------------------------------


def _base_config(
    table: str,
    source_path: str,
    source_format: str,
    columns: list[dict],
    *,
    layout_columns: list[dict] | None = None,
    seed: int = 20260930,
) -> dict:
    source_entry: dict[str, Any] = {"type": "file", "format": source_format, "path": source_path}
    if source_format == "fixed_width":
        source_entry["layout"] = {"columns": layout_columns}
    return {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {table: source_entry},
        "targets": {
            table: {"type": "file", "format": "parquet", "path": f"{source_path}.out.parquet"}
        },
        "tables": [{"name": table, "columns": columns}],
    }


def _validate_config(raw: dict) -> dict:
    from decoy_engine.config import PipelineConfig

    return PipelineConfig.model_validate(raw).model_dump()


def _key_provider():
    from decoy_engine.keyprovider import SecretKeyProvider

    # Fixed, non-secret 32-byte pattern: this is a masking derivation key for
    # a throwaway audit fixture, never a real production secret, so it is
    # safe to keep inline rather than reading it from the environment.
    return SecretKeyProvider(secret=bytes(range(32)), key_version="v1")


# ---------------------------------------------------------------------------
# Route-evidence extraction + backend classification
# ---------------------------------------------------------------------------


def _classify_nodes(quality_metrics: dict) -> dict[str, dict]:
    """Per-node backend classification per the plan's rule.

    Returns {node_id: {"operator", "executed", "compiled_kernel_executed", "backend"}}.
    Empty when the unified slice did not activate (table declined; every
    node ran the legacy pandas adapter -- recorded separately as
    `unified_slice_activated: False`).
    """
    leaf = quality_metrics.get("unified_slice_activation")
    if not leaf or not leaf.get("activated"):
        return {}
    out = {}
    for node_id, ev in leaf.get("nodes", {}).items():
        operator = ev.get("operator", "")
        compiled = bool(ev.get("compiled_kernel_executed"))
        if compiled:
            backend = "rust_companion"
        elif any(operator.endswith(s) for s in ("passthrough", "redact", "truncate")):
            backend = "arrow_python_native"
        else:
            # An operator eligible for the compiled kernel (hash/categorical/
            # bucket_perturb/group_key/date_shift) that executed WITHOUT
            # compiled_kernel_executed=True would land here -- flagged as a
            # surprise by the caller, never silently reported as Rust.
            backend = "arrow_python_native_or_uncompiled"
        out[node_id] = {
            "operator": operator,
            "executed": ev.get("executed"),
            "compiled_kernel_executed": compiled,
            "backend": backend,
        }
    return out


def _overall_backend(node_backends: dict[str, dict], unified_activated: bool) -> str:
    if not unified_activated:
        return "pandas"
    backends = {v["backend"] for v in node_backends.values()}
    if len(backends) == 1:
        return next(iter(backends))
    return "mixed:" + "+".join(sorted(backends))


def _outputs_match(off_outputs: dict, on_outputs: dict) -> dict:
    if set(off_outputs) != set(on_outputs):
        return {"match": False, "reason": "table set differs"}
    for table in off_outputs:
        off_t, on_t = off_outputs[table], on_outputs[table]
        if off_t.column_names != on_t.column_names:
            return {"match": False, "reason": f"{table}: column_names differ"}
        if off_t.num_rows != on_t.num_rows:
            return {"match": False, "reason": f"{table}: row count differs"}
        for name in off_t.column_names:
            if off_t.schema.field(name).type != on_t.schema.field(name).type:
                return {"match": False, "reason": f"{table}.{name}: type differs"}
            if off_t.column(name).to_pylist() != on_t.column(name).to_pylist():
                return {"match": False, "reason": f"{table}.{name}: values differ"}
    return {"match": True, "reason": None}


# ---------------------------------------------------------------------------
# Cell runners
# ---------------------------------------------------------------------------


def _run_pipeline_call(config: dict, sources: dict, *, run_kwargs: dict):
    from decoy_engine.execution import run_pipeline

    kwargs = dict(run_kwargs)
    kwargs.setdefault("explain_plan", True)
    return run_pipeline(
        config,
        sources,
        engine_version="stage2a-probe",
        key_provider=_key_provider(),
        **kwargs,
    )


def run_cell(spec: dict) -> dict:
    tmp_dir = _mk_tmp_dir()
    cell_id = spec["id"]
    kind = spec["kind"]
    rows = spec.get("rows", 10_000)
    source_format = spec.get("source_format", "parquet")
    run_kwargs = dict(spec.get("run_kwargs", {}))
    table = "t"

    columns: list[dict]
    src_columns: dict
    layout_columns = None

    if kind == "single_strategy":
        strategy = spec["strategy"]
        columns, src_columns = _STRATEGY_BUILDERS[strategy](rows)
    elif kind == "mix":
        columns, src_columns = _mix_columns_and_source(rows)
    elif kind == "mix_faker":
        columns, src_columns = _mix_columns_and_source(rows)
        faker_cols, faker_src = _strategy_faker_pooled(rows)
        columns = columns + faker_cols
        src_columns = {**src_columns, **faker_src}
    elif kind == "mix_when":
        columns, src_columns = _mix_columns_and_source(rows)
    elif kind == "hash_size":
        columns, src_columns = _strategy_hash(rows)
    else:
        raise ValueError(f"unhandled kind for run_cell: {kind}")

    import pyarrow as pa

    source_table = pa.table(src_columns)

    if source_format == "parquet":
        resident, path = _write_and_reload_parquet(tmp_dir, cell_id, source_table)
    elif source_format == "csv":
        resident, path = _write_and_reload_csv(tmp_dir, cell_id, source_table)
    elif source_format == "fixed_width":
        resident, path, layout_columns = _write_and_reload_fixed_width(tmp_dir, cell_id, rows)
    else:
        raise ValueError(f"unhandled source_format: {source_format}")

    raw_config = _base_config(table, path, source_format, columns, layout_columns=layout_columns)

    if kind == "mix_when":
        validated = _validate_config(raw_config)
        # `when` is schema-rejected (extra_forbidden) -- reachable today only
        # by injecting it into the already-validated dict and calling
        # run_pipeline directly, bypassing PipelineConfig.model_validate.
        # This mirrors the branch-witness ledger's own note on B017/B049.
        validated["tables"][0]["columns"][0]["when"] = "p > 0"
        config = validated
        schema_bypassed_for_when_gate = True
    else:
        config = _validate_config(raw_config)
        schema_bypassed_for_when_gate = False

    sources = {table: resident}

    t0 = time.time()
    result = _run_pipeline_call(config, sources, run_kwargs=run_kwargs)
    wall_seconds = time.time() - t0

    quality_metrics = result.quality_metrics
    unified_leaf = quality_metrics.get("unified_slice_activation")
    unified_activated = bool(unified_leaf and unified_leaf.get("activated"))
    node_backends = _classify_nodes(quality_metrics)
    overall_backend = _overall_backend(node_backends, unified_activated)

    faker_pool_evidence = None
    if kind == "single_strategy" and spec.get("strategy") == "faker_pooled":
        # Established during the interactive validation pass (and confirmed
        # by grep across engine/platform/CLI): `execution.native._dispatch`'s
        # NativeRouteEvidence.pool_select_executed / pool_select_calls is not
        # reachable from run_pipeline's production routing today (faker
        # declines the unified slice per B028 and the chunked route always
        # calls the pandas-oracle-only `_chunked.run_mask_pipeline_chunked`,
        # never `execution.native._dispatch.run_native_or_oracle_chunked`).
        # Recorded explicitly as "not observed / not reachable", never
        # inferred as True.
        faker_pool_evidence = {
            "pool_select_executed": None,
            "pool_select_calls": None,
            "note": (
                "unreachable from this run: no production entry point calls "
                "execution.native._dispatch.run_native_or_oracle_chunked "
                "(confirmed by grep across engine src/, decoy-platform, and decoy-cli)"
            ),
        }

    parity = {"checked": False, "match": None, "reason": "backend already pandas or n/a"}
    if unified_activated:
        oracle_kwargs = dict(run_kwargs)
        oracle_kwargs["unified_slice_enabled"] = False
        oracle_result = _run_pipeline_call(config, dict(sources), run_kwargs=oracle_kwargs)
        cmp = _outputs_match(oracle_result.outputs, result.outputs)
        parity = {"checked": True, "match": cmp["match"], "reason": cmp["reason"]}

    vmhwm_kb = _vmhwm_kb()

    env_record = _read_environment_record()
    native_status = env_record.get("native_companion", {}).get("native_companion_status", {})
    companion_sha = env_record.get("native_companion", {}).get("module_sha256")

    record = {
        "cell_id": cell_id,
        "description": spec.get("description"),
        "kind": kind,
        "ledger_ids": spec.get("ledger_ids", []),
        "params": {
            "strategy": spec.get("strategy"),
            "rows": rows,
            "source_format": source_format,
            "run_kwargs": run_kwargs,
            "entry_point": "engine_direct",
            "schema_bypassed_for_when_gate": schema_bypassed_for_when_gate,
        },
        "route_evidence": {
            "execution": quality_metrics.get("execution"),
            "auto_chunk": quality_metrics.get("auto_chunk"),
            "execution_plan": quality_metrics.get("execution_plan"),
            "unified_slice_activated": unified_activated,
            "unified_slice_nodes": node_backends,
        },
        "faker_pool_evidence": faker_pool_evidence,
        "overall_backend": overall_backend,
        "parity_vs_pandas_oracle": parity,
        "wall_seconds": round(wall_seconds, 4),
        "peak_memory_vmhwm_kb": vmhwm_kb,
        "row_count_in": rows,
        "row_counts_out": {k: v.num_rows for k, v in result.outputs.items()},
        "table_kinds": result.table_kinds,
        "commits": {
            "engine_commit": env_record.get("engine", {}).get("commit"),
            "platform_commit": env_record.get("platform", {}).get("commit"),
            "cli_commit": env_record.get("cli", {}).get("commit"),
        },
        "native_companion_status": native_status,
        "native_companion_sha256": companion_sha,
        "python_version": env_record.get("python", {}).get("version"),
    }
    return record


def run_generate_only(spec: dict) -> dict:
    rows = spec.get("rows", 10_000)
    table = "g"
    raw_config = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {},
        "targets": {
            table: {
                "type": "file",
                "format": "parquet",
                "path": "/dev/shm/audit-2026-09-30/scratch/gen.out.parquet",  # noqa: S108
            }
        },
        "tables": [
            {
                "name": table,
                "row_count": rows,
                "generate_columns": [{"name": "fn", "type": "faker", "faker_type": "first_name"}],
            }
        ],
    }
    config = _validate_config(raw_config)
    t0 = time.time()
    result = _run_pipeline_call(config, {}, run_kwargs=spec.get("run_kwargs", {}))
    wall_seconds = time.time() - t0
    vmhwm_kb = _vmhwm_kb()
    env_record = _read_environment_record()
    return {
        "cell_id": spec["id"],
        "description": spec.get("description"),
        "kind": "generate_only",
        "ledger_ids": spec.get("ledger_ids", []),
        "params": {"rows": rows, "entry_point": "engine_direct"},
        "route_evidence": {
            "execution": result.quality_metrics.get("execution"),
            "execution_plan": result.quality_metrics.get("execution_plan"),
        },
        "overall_backend": "pandas (generate path; no mask nodes)",
        "parity_vs_pandas_oracle": {"checked": False, "match": None, "reason": "no mask node"},
        "wall_seconds": round(wall_seconds, 4),
        "peak_memory_vmhwm_kb": vmhwm_kb,
        "row_count_in": 0,
        "row_counts_out": {k: v.num_rows for k, v in result.outputs.items()},
        "table_kinds": result.table_kinds,
        "commits": {
            "engine_commit": env_record.get("engine", {}).get("commit"),
            "platform_commit": env_record.get("platform", {}).get("commit"),
            "cli_commit": env_record.get("cli", {}).get("commit"),
        },
        "native_companion_status": env_record.get("native_companion", {}).get(
            "native_companion_status", {}
        ),
        "native_companion_sha256": env_record.get("native_companion", {}).get("module_sha256"),
        "python_version": env_record.get("python", {}).get("version"),
    }


def run_mask_generate(spec: dict) -> dict:
    import pyarrow as pa

    tmp_dir = _mk_tmp_dir()
    rows = spec.get("rows", 10_000)
    mask_table = "m"
    gen_table = "g"
    _, src_columns = _strategy_hash(rows)
    source_table = pa.table(src_columns)
    resident, path = _write_and_reload_parquet(tmp_dir, spec["id"], source_table)
    raw_config = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {mask_table: {"type": "file", "format": "parquet", "path": path}},
        "targets": {
            mask_table: {"type": "file", "format": "parquet", "path": f"{path}.m.out.parquet"},
            gen_table: {"type": "file", "format": "parquet", "path": f"{path}.g.out.parquet"},
        },
        "tables": [
            {
                "name": mask_table,
                "columns": [{"name": "h", "strategy": "hash", "namespace": "ns_h"}],
            },
            {
                "name": gen_table,
                "row_count": rows,
                "generate_columns": [{"name": "fn", "type": "faker", "faker_type": "first_name"}],
            },
        ],
    }
    config = _validate_config(raw_config)
    sources = {mask_table: resident}
    t0 = time.time()
    result = _run_pipeline_call(config, sources, run_kwargs=spec.get("run_kwargs", {}))
    wall_seconds = time.time() - t0
    quality_metrics = result.quality_metrics
    unified_leaf = quality_metrics.get("unified_slice_activation")
    unified_activated = bool(unified_leaf and unified_leaf.get("activated"))
    node_backends = _classify_nodes(quality_metrics)
    overall_backend = _overall_backend(node_backends, unified_activated)
    vmhwm_kb = _vmhwm_kb()
    env_record = _read_environment_record()
    return {
        "cell_id": spec["id"],
        "description": spec.get("description"),
        "kind": "mask_generate",
        "ledger_ids": spec.get("ledger_ids", []),
        "params": {"rows": rows, "entry_point": "engine_direct"},
        "route_evidence": {
            "execution": quality_metrics.get("execution"),
            "execution_plan": quality_metrics.get("execution_plan"),
            "unified_slice_activated": unified_activated,
            "unified_slice_nodes": node_backends,
        },
        "overall_backend": overall_backend,
        "parity_vs_pandas_oracle": {
            "checked": False,
            "match": None,
            "reason": "mixed generate+mask config declines the unified slice by construction (B007); backend is pandas",
        },
        "wall_seconds": round(wall_seconds, 4),
        "peak_memory_vmhwm_kb": vmhwm_kb,
        "row_count_in": rows,
        "row_counts_out": {k: v.num_rows for k, v in result.outputs.items()},
        "table_kinds": result.table_kinds,
        "commits": {
            "engine_commit": env_record.get("engine", {}).get("commit"),
            "platform_commit": env_record.get("platform", {}).get("commit"),
            "cli_commit": env_record.get("cli", {}).get("commit"),
        },
        "native_companion_status": env_record.get("native_companion", {}).get(
            "native_companion_status", {}
        ),
        "native_companion_sha256": env_record.get("native_companion", {}).get("module_sha256"),
        "python_version": env_record.get("python", {}).get("version"),
    }


def main() -> None:
    if len(sys.argv) > 1:
        raw = sys.argv[1]
        if os.path.exists(raw):
            spec = json.loads(Path(raw).read_text())
        else:
            spec = json.loads(raw)
    else:
        spec = json.loads(sys.stdin.read())

    kind = spec["kind"]
    if kind == "generate_only":
        record = run_generate_only(spec)
    elif kind == "mask_generate":
        record = run_mask_generate(spec)
    else:
        record = run_cell(spec)

    print(json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    main()
