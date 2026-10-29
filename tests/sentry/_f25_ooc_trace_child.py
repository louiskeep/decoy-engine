"""Child driver for the F25 allocation-sentry syscall trace (A04/A12).

Runs one real out-of-core FK job through either inner lane with a sink, forcing
spill, so the parent test can strace it and prove every data write lands under
the attempt arena directory. Not a test module (no ``test_`` prefix); it is
exec'd in a fresh interpreter under strace.

argv: <repo_root> <base_dir> <lane:batch|reorder>
env DECOY_F25_SEED_BYPASS=1 seeds a deliberate unregistered write (a leak file
written straight under the OOC temp root, outside any arena) so the parent test
can confirm the trace check is non-vacuous.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    repo_root = Path(sys.argv[1]).resolve()
    base = Path(sys.argv[2]).resolve()
    lane = sys.argv[3]
    sys.path.insert(0, str(repo_root / "src"))
    sys.path.insert(0, str(repo_root))

    import pyarrow as pa

    from decoy_engine.execution._transactional_sink import ParquetTransactionalSink
    from decoy_engine.execution.out_of_core._runner import run_fk_out_of_core
    from tests.physical._helpers import build_job

    if os.environ.get("DECOY_F25_SEED_BYPASS") == "1":
        # Seed a bypass inside a migrated producer: every lane opens a DuckDB
        # connection through connect_duckdb, so wrapping it to also create a raw
        # file straight under the OOC temp root (outside any `.decoy-arena-*`
        # directory) models a producer that forgot the capability seam. The
        # parent's trace check must catch this write.
        import decoy_engine.execution.out_of_core._duckdb as _duckdb_mod

        _real_connect = _duckdb_mod.connect_duckdb
        leak = base / "work" / "F25_SENTRY_BYPASS_LEAK.bin"

        def _leaky_connect(**kwargs: object) -> object:
            conn = _real_connect(**kwargs)  # type: ignore[arg-type]
            leak.parent.mkdir(parents=True, exist_ok=True)
            with open(leak, "wb") as handle:
                handle.write(b"unregistered")
            return conn

        _duckdb_mod.connect_duckdb = _leaky_connect  # type: ignore[assignment]
        # The runner imported the symbol by reference paths; patch those too.
        import decoy_engine.execution.out_of_core._batch_join as _bj
        import decoy_engine.execution.out_of_core._relation as _rel

        _bj.connect_duckdb = _leaky_connect  # type: ignore[assignment]
        _rel.connect_duckdb = _leaky_connect  # type: ignore[assignment]

    n = 24
    parent = pa.table(
        {
            "id": pa.array([f"p{i}" for i in range(n)], type=pa.string()),
            "note": pa.array([f"note-{i}" for i in range(n)], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "cid": pa.array([f"c{i}" for i in range(n)], type=pa.string()),
            "pid": pa.array([f"p{i % n}" for i in range(n)], type=pa.string()),
        }
    )
    specs = [
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
            ],
        },
    ]
    relationships = [
        {
            "parent": {"table": "parent", "columns": ["id"]},
            "children": [{"table": "child", "columns": ["pid"]}],
            "orphan_policy": "preserve",
            "namespace": "pns",
        }
    ]
    fixtures = base / "fix"
    fixtures.mkdir(parents=True, exist_ok=True)
    job = build_job(
        fixtures, {"parent": parent, "child": child}, specs, relationships=relationships
    )

    work = base / "work"
    work.mkdir(parents=True, exist_ok=True)
    out = base / "out"
    kwargs: dict[str, object] = dict(
        registry=job.registry,
        relationship_graph=job.graph,
        sink=ParquetTransactionalSink(out),
        temp_dir=work,
    )
    if lane == "reorder":
        kwargs["budget_bytes"] = 8 * 1024 * 1024
        kwargs["out_of_core_reorder_threshold_rows"] = 0
    run_fk_out_of_core(job.plan, job.sources, **kwargs)  # type: ignore[arg-type]
    print("OOC_RUN_OK", lane)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
