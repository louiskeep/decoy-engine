#!/usr/bin/env python3
"""Stage 2c, scope A: FK/relationship shapes through the engine-direct entry
point (decoy_engine.execution.run_pipeline).

Per docs/plans/2026-09-30-rust-coverage-evidence-audit.md scope A: FK tree,
FK diamond, self-referential FK, cross-table FK cycle, FK with validators,
FK with a generate table (mixed mask+generate). Key strategy: hash with an
explicit shared namespace (parent + child). Payload strategies: a native mix
(hash/redact/truncate/passthrough), plus one OOC-compatible payload strategy
(bucket_perturb, explicit date_format) and one OOC-incompatible payload
strategy (group_key, a cross-row strategy _CROSS_ROW_STRATEGIES rejects in
`execution/out_of_core/_compat.py`).

One cell per fresh process (this script does ONE shape per invocation, called
from the driver shell loop), VmHWM read from /proc/self/status at the end of
this process, matching stage 2a/2b's probe.py convention.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_RECORD = REPO_ROOT / "docs" / "records" / "audit-2026-09-30" / "environment.json"
SCRATCH = Path("/dev/shm/audit-2026-09-30/scratch")  # noqa: S108


def _vmhwm_kb() -> int | None:
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def _env_record() -> dict[str, Any]:
    if not ENV_RECORD.exists():
        return {}
    return json.loads(ENV_RECORD.read_text())


def _key_provider():
    from decoy_engine.keyprovider import SecretKeyProvider

    return SecretKeyProvider(secret=bytes(range(32)), key_version="v1")


def _validate_config(raw: dict) -> dict:
    from decoy_engine.config import PipelineConfig

    return PipelineConfig.model_validate(raw).model_dump()


def _write_parquet(name: str, table) -> str:
    import pyarrow.parquet as pq

    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def _payload_mix_columns(prefix: str, *, include_ooc_incompatible: bool = True) -> list[dict]:
    """A native-mix payload: hash + redact + truncate + passthrough, plus one
    OOC-compatible strategy (bucket_perturb). ``include_ooc_incompatible``
    adds group_key, a cross-row strategy `execution/out_of_core/_compat.py`
    (`_CROSS_ROW_STRATEGIES`) declines -- set False to build an
    all-OOC-compatible variant for contrast."""
    cols = [
        {"name": f"{prefix}_h", "strategy": "hash", "namespace": f"ns_{prefix}_payload"},
        {"name": f"{prefix}_r", "strategy": "redact", "provider_config": {"char": "*"}},
        {"name": f"{prefix}_t", "strategy": "truncate", "provider_config": {"length": 4}},
        {"name": f"{prefix}_p", "strategy": "passthrough"},
        {
            "name": f"{prefix}_bp",
            "strategy": "bucket_perturb",
            "namespace": f"ns_{prefix}_bp",
            "provider_config": {"bucket": "week", "date_format": "%Y-%m-%d"},
        },
    ]
    if include_ooc_incompatible:
        cols.append(
            {
                "name": f"{prefix}_gk",
                "strategy": "group_key",
                "provider_config": {"group_by": f"{prefix}_gksib", "length": 16},
            }
        )
        cols.append({"name": f"{prefix}_gksib", "strategy": "passthrough"})
    return cols


def _payload_source(prefix: str, n: int, *, include_ooc_incompatible: bool = True) -> dict:
    import pyarrow as pa

    src = {
        f"{prefix}_h": pa.array([f"user{i}@ex.com" for i in range(n)], type=pa.string()),
        f"{prefix}_r": pa.array([f"secret{i}" for i in range(n)], type=pa.string()),
        f"{prefix}_t": pa.array([f"card{i:08d}" for i in range(n)], type=pa.string()),
        f"{prefix}_p": pa.array([i for i in range(n)], type=pa.int64()),
        f"{prefix}_bp": pa.array(
            [f"2024-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}" for i in range(n)], type=pa.string()
        ),
    }
    if include_ooc_incompatible:
        src[f"{prefix}_gksib"] = pa.array([f"grp{i % 7}" for i in range(n)], type=pa.string())
        src[f"{prefix}_gk"] = pa.array([f"k{i}" for i in range(n)], type=pa.string())
    return src


def _key_column(name: str, namespace: str) -> dict:
    return {"name": name, "strategy": "hash", "namespace": namespace}


def _base_relational_config(
    tables: list[dict], sources: dict[str, str], relationships: list[dict], *, extra: dict | None = None
) -> dict:
    cfg: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {t["name"]: {"type": "file", "format": "parquet", "path": sources[t["name"]]} for t in tables if t["name"] in sources},
        "targets": {t["name"]: {"type": "file", "format": "parquet", "path": f"{sources.get(t['name'], t['name'])}.out.parquet"} for t in tables},
        "tables": tables,
        "relationships": relationships,
    }
    if extra:
        cfg.update(extra)
    return cfg


# ---------------------------------------------------------------------------
# Shape builders: each returns (config_dict, sources_dict[table]->pa.Table)
# ---------------------------------------------------------------------------


def build_fk_tree(n_parent: int, n_child: int) -> tuple[dict, dict]:
    import pyarrow as pa

    parent_id = pa.array([f"p{i}" for i in range(n_parent)], type=pa.string())
    parent_cols = [_key_column("id", "ns_key")] + _payload_mix_columns("par")
    parent_src = {"id": parent_id, **_payload_source("par", n_parent)}
    parent_table = pa.table(parent_src)

    child_parent_id = pa.array([f"p{i % n_parent}" for i in range(n_child)], type=pa.string())
    child_cols = [_key_column("parent_id", "ns_key")] + _payload_mix_columns("chi")
    child_src = {"parent_id": child_parent_id, **_payload_source("chi", n_child)}
    child_table = pa.table(child_src)

    p_path = _write_parquet("fk_tree_parent", parent_table)
    c_path = _write_parquet("fk_tree_child", child_table)

    tables = [
        {"name": "parent", "columns": parent_cols},
        {"name": "child", "columns": child_cols},
    ]
    relationships = [
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["parent_id"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_key",
        }
    ]
    cfg = _base_relational_config(tables, {"parent": p_path, "child": c_path}, relationships)
    return cfg, {"parent": pa.table(parent_src), "child": pa.table(child_src)}


def build_fk_tree_ooc_compatible(n_parent: int, n_child: int) -> tuple[dict, dict]:
    """Same FK tree, payload restricted to OOC-compatible strategies only
    (drops group_key) -- the contrast case that lets a forced out_of_core run
    actually succeed instead of declining at the compat gate."""
    import pyarrow as pa

    parent_id = pa.array([f"p{i}" for i in range(n_parent)], type=pa.string())
    parent_cols = [_key_column("id", "ns_key")] + _payload_mix_columns(
        "par", include_ooc_incompatible=False
    )
    parent_src = {"id": parent_id, **_payload_source("par", n_parent, include_ooc_incompatible=False)}
    parent_table = pa.table(parent_src)

    child_parent_id = pa.array([f"p{i % n_parent}" for i in range(n_child)], type=pa.string())
    child_cols = [_key_column("parent_id", "ns_key")] + _payload_mix_columns(
        "chi", include_ooc_incompatible=False
    )
    child_src = {
        "parent_id": child_parent_id,
        **_payload_source("chi", n_child, include_ooc_incompatible=False),
    }
    child_table = pa.table(child_src)

    p_path = _write_parquet("fk_tree_ooc_parent", parent_table)
    c_path = _write_parquet("fk_tree_ooc_child", child_table)

    tables = [
        {"name": "parent", "columns": parent_cols},
        {"name": "child", "columns": child_cols},
    ]
    relationships = [
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["parent_id"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_key",
        }
    ]
    cfg = _base_relational_config(tables, {"parent": p_path, "child": c_path}, relationships)
    return cfg, {"parent": pa.table(parent_src), "child": pa.table(child_src)}


def build_fk_diamond(n_parent: int, n_child: int) -> tuple[dict, dict]:
    """Child with two parents on the SAME child key column (the ambiguous
    shape out_of_core/_compat.py's out_of_core_multi_parent_child_unsupported
    declines)."""
    import pyarrow as pa

    a_id = pa.array([f"a{i}" for i in range(n_parent)], type=pa.string())
    b_id = pa.array([f"a{i}" for i in range(n_parent)], type=pa.string())  # same key space
    a_cols = [_key_column("id", "ns_a")] + _payload_mix_columns("pa")
    b_cols = [_key_column("id", "ns_b")] + _payload_mix_columns("pb")
    a_src = {"id": a_id, **_payload_source("pa", n_parent)}
    b_src = {"id": b_id, **_payload_source("pb", n_parent)}

    child_ref = pa.array([f"a{i % n_parent}" for i in range(n_child)], type=pa.string())
    child_cols = [
        {"name": "ref", "strategy": "hash", "namespace": "ns_a"},
    ] + _payload_mix_columns("chi")
    child_src = {"ref": child_ref, **_payload_source("chi", n_child)}

    a_path = _write_parquet("fk_diamond_a", pa.table(a_src))
    b_path = _write_parquet("fk_diamond_b", pa.table(b_src))
    c_path = _write_parquet("fk_diamond_child", pa.table(child_src))

    tables = [
        {"name": "parent_a", "columns": a_cols},
        {"name": "parent_b", "columns": b_cols},
        {"name": "child", "columns": child_cols},
    ]
    relationships = [
        {
            "parent": {"table": "parent_a", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["ref"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_a",
        },
        {
            "parent": {"table": "parent_b", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["ref"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_a",
        },
    ]
    cfg = _base_relational_config(
        tables, {"parent_a": a_path, "parent_b": b_path, "child": c_path}, relationships
    )
    return cfg, {
        "parent_a": pa.table(a_src),
        "parent_b": pa.table(b_src),
        "child": pa.table(child_src),
    }


def build_fk_self_ref(n: int) -> tuple[dict, dict]:
    import pyarrow as pa

    ids = [f"e{i}" for i in range(n)]
    mgr = [None] + [f"e{i - 1}" for i in range(1, n)]
    id_arr = pa.array(ids, type=pa.string())
    mgr_arr = pa.array(mgr, type=pa.string())
    cols = [
        _key_column("id", "ns_key"),
        {"name": "manager_id", "strategy": "hash", "namespace": "ns_key"},
    ] + _payload_mix_columns("emp")
    src = {"id": id_arr, "manager_id": mgr_arr, **_payload_source("emp", n)}
    path = _write_parquet("fk_selfref", pa.table(src))
    tables = [{"name": "employees", "columns": cols}]
    relationships = [
        {
            "parent": {"table": "employees", "columns": ["id"]},
            "children": [{"table": "employees", "columns": ["manager_id"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_key",
        }
    ]
    cfg = _base_relational_config(tables, {"employees": path}, relationships)
    return cfg, {"employees": pa.table(src)}


def build_fk_cross_cycle(n: int) -> tuple[dict, dict]:
    import pyarrow as pa

    a_id = pa.array([f"a{i}" for i in range(n)], type=pa.string())
    a_ref_b = pa.array([f"b{i}" for i in range(n)], type=pa.string())
    b_id = pa.array([f"b{i}" for i in range(n)], type=pa.string())
    b_ref_a = pa.array([f"a{i}" for i in range(n)], type=pa.string())

    a_cols = [
        _key_column("id", "ns_a"),
        {"name": "ref_b", "strategy": "hash", "namespace": "ns_b"},
    ] + _payload_mix_columns("a")
    b_cols = [
        _key_column("id", "ns_b"),
        {"name": "ref_a", "strategy": "hash", "namespace": "ns_a"},
    ] + _payload_mix_columns("b")

    a_src = {"id": a_id, "ref_b": a_ref_b, **_payload_source("a", n)}
    b_src = {"id": b_id, "ref_a": b_ref_a, **_payload_source("b", n)}

    a_path = _write_parquet("fk_cycle_a", pa.table(a_src))
    b_path = _write_parquet("fk_cycle_b", pa.table(b_src))

    tables = [{"name": "a", "columns": a_cols}, {"name": "b", "columns": b_cols}]
    relationships = [
        {
            "parent": {"table": "a", "columns": ["id"]},
            "children": [{"table": "b", "columns": ["ref_a"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_a",
        },
        {
            "parent": {"table": "b", "columns": ["id"]},
            "children": [{"table": "a", "columns": ["ref_b"]}],
            "orphan_policy": "preserve",
            "namespace": "ns_b",
        },
    ]
    cfg = _base_relational_config(tables, {"a": a_path, "b": b_path}, relationships)
    return cfg, {"a": pa.table(a_src), "b": pa.table(b_src)}


def build_fk_validators(n_parent: int, n_child: int) -> tuple[dict, dict]:
    cfg, sources = build_fk_tree(n_parent, n_child)
    cfg["validators"] = [{"name": "fk_intact"}, {"name": "no_orphan_children"}]
    return cfg, sources


def build_fk_generate_mixed(n_parent: int, n_child: int) -> tuple[dict, dict]:
    """FK parent is a generate-kind table; child is a mask table referencing
    the generated parent key (B184: the generate step runs first and its
    output feeds the mask adapter's sources directly)."""
    import pyarrow as pa

    child_parent_id = pa.array([f"row{i % n_parent}" for i in range(n_child)], type=pa.string())
    child_cols = [_key_column("parent_id", "ns_key")] + _payload_mix_columns("chi")
    child_src = {"parent_id": child_parent_id, **_payload_source("chi", n_child)}
    c_path = _write_parquet("fk_gen_child", pa.table(child_src))

    tables = [
        {
            "name": "parent",
            "row_count": n_parent,
            "generate_columns": [
                {"name": "id", "type": "sequence", "prefix": "row", "start": 0},
            ],
        },
        {"name": "child", "columns": child_cols},
    ]
    cfg = {
        "version": 1,
        "global_settings": {"seed": 20260930},
        "sources": {"child": {"type": "file", "format": "parquet", "path": c_path}},
        "targets": {
            "parent": {"type": "file", "format": "parquet", "path": f"{c_path}.parent.out.parquet"},
            "child": {"type": "file", "format": "parquet", "path": f"{c_path}.child.out.parquet"},
        },
        "tables": tables,
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": "preserve",
                "namespace": "ns_key",
            }
        ],
    }
    return cfg, {"child": pa.table(child_src)}


_SHAPES = {
    "fk_tree": lambda sizes: build_fk_tree(*sizes),
    "fk_tree_ooc_compatible": lambda sizes: build_fk_tree_ooc_compatible(*sizes),
    "fk_diamond": lambda sizes: build_fk_diamond(*sizes),
    "fk_self_ref": lambda sizes: build_fk_self_ref(sizes[0]),
    "fk_cross_cycle": lambda sizes: build_fk_cross_cycle(sizes[0]),
    "fk_validators": lambda sizes: build_fk_validators(*sizes),
    "fk_generate_mixed": lambda sizes: build_fk_generate_mixed(*sizes),
}


def _read_route_evidence(result) -> dict:
    qm = result.quality_metrics
    return {
        "execution": qm.get("execution"),
        "execution_plan": qm.get("execution_plan"),
        "out_of_core": qm.get("out_of_core"),
        "sequential": qm.get("sequential"),
        "unified_slice_activation": qm.get("unified_slice_activation"),
    }


def run_cell(spec: dict) -> dict:
    from decoy_engine.execution import run_pipeline
    from decoy_engine.errors import ConfigError
    from decoy_engine.execution._errors import ExecutionError

    shape = spec["shape"]
    sizes = spec.get("sizes", [200, 1000])
    run_kwargs = dict(spec.get("run_kwargs", {}))
    builder = _SHAPES[shape]
    config_raw, sources = builder(sizes)

    from decoy_engine.config import PipelineConfig

    try:
        config = PipelineConfig.model_validate(config_raw).model_dump()
    except Exception as exc:  # schema-level rejection is itself a result
        return {
            "cell_id": spec["id"],
            "kind": "stage2c_fk_shape",
            "description": spec.get("description"),
            "shape": shape,
            "outcome": {"status": "schema_rejected", "error": str(exc)[:2000]},
            "params": {"sizes": sizes, "run_kwargs": run_kwargs, "entry_point": "engine_direct"},
        }

    t0 = time.time()
    status = "ok"
    error = None
    result = None
    try:
        result = run_pipeline(
            config,
            sources,
            engine_version="stage2c-probe",
            key_provider=_key_provider(),
            explain_plan=True,
            **run_kwargs,
        )
    except (ConfigError, ExecutionError) as exc:
        status = "rejected"
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # pragma: no cover - record, don't hide
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
    wall = time.time() - t0

    record: dict[str, Any] = {
        "cell_id": spec["id"],
        "kind": "stage2c_fk_shape",
        "description": spec.get("description"),
        "shape": shape,
        "params": {
            "sizes": sizes,
            "run_kwargs": run_kwargs,
            "entry_point": "engine_direct (decoy_engine.execution.run_pipeline)",
        },
        "outcome": {"status": status, "error": error, "wall_seconds": round(wall, 4)},
        "peak_memory_vmhwm_kb": _vmhwm_kb(),
    }
    if result is not None:
        record["route_evidence"] = _read_route_evidence(result)
        record["row_counts_out"] = {k: v.num_rows for k, v in result.outputs.items()}
        record["table_kinds"] = result.table_kinds
    env_record = _env_record()
    record["commits"] = {
        "engine_commit": env_record.get("engine", {}).get("commit"),
        "platform_commit": env_record.get("platform", {}).get("commit"),
    }
    record["native_companion_status"] = env_record.get("native_companion", {}).get(
        "native_companion_status", {}
    )
    return record


def main() -> None:
    spec = json.loads(sys.argv[1]) if len(sys.argv) > 1 else json.loads(sys.stdin.read())
    record = run_cell(spec)
    print(json.dumps(record, default=str))


if __name__ == "__main__":
    main()
