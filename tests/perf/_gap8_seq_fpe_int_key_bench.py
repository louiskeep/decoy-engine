"""Predeclared GAP8 benchmark: native vs Python fpe on an integer FK key, SEQ route.

Fixture (fixed before measuring; docs/plans/2026-10-14-seq-fpe-int-key-native.md section 5):

* parent `P.pid`: 1,000,000 distinct int64 10-digit keys, no nulls, default digits charset;
* child `C.cid`: 1,000,000 rows, 5% genuine orphans (minted through REMAP), 95% drawn from the
  parent keys; orphans are also 10-digit and absent from the parent;
* `native_threads` pinned to 1; seed fixed;
* timed region: the whole `run_sequential` call (conversion, kernel, warnings, materialization),
  which holds only this fpe key pair; the fpe-handler time from the strategy timing records is
  reported next to it. Peak RSS is per process, one mode per process.
* native: 1 warmup + 4 measured rounds; python: 2 measured rounds.

Run as `python -m tests.perf._gap8_seq_fpe_int_key_bench --mode native|python --rows N`.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import resource
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pyarrow as pa

SEED = 20261014
ROUNDS = {"native": (1, 4), "python": (0, 2)}  # (warmup, measured)
ORPHAN_FRACTION = 0.05
_DIGEST_SLICE = 500_000


def _seed(namespace: str) -> Any:
    from decoy_engine.plan._types import ColumnSeed

    return ColumnSeed(
        namespace=namespace,
        strategy="fpe",
        provider="fpe",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def build_sources(rows: int) -> dict[str, pa.Table]:
    rng = np.random.default_rng(SEED)
    parent = rng.choice(9 * 10**9, size=rows, replace=False).astype(np.int64) + 10**9
    n_orphan = int(rows * ORPHAN_FRACTION)
    orphans: np.ndarray[Any, Any] = np.empty(0, dtype=np.int64)
    while len(orphans) < n_orphan:
        cand = rng.integers(10**9, 10**10, size=n_orphan * 2, dtype=np.int64)
        cand = np.unique(cand[~np.isin(cand, parent)])
        orphans = np.unique(np.concatenate([orphans, cand]))
    orphans = rng.permutation(orphans)[:n_orphan]
    matched = parent[rng.integers(0, rows, size=rows - n_orphan)]
    child = rng.permutation(np.concatenate([matched, orphans]))
    return {
        "P": pa.table({"pid": pa.array(parent, pa.int64())}),
        "C": pa.table({"cid": pa.array(child, pa.int64())}),
    }


def _plan() -> Any:
    from decoy_engine.plan._types import SeedEnvelope, TableSeed

    tables = (
        ("P", TableSeed(per_column=(("pid", _seed("ns_p")),), per_group=())),
        ("C", TableSeed(per_column=(("cid", _seed("ns_c")),), per_group=())),
    )
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=b"\x11" * 8, per_table=tables))


def _graph() -> Any:
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

    edge = RelationshipEdge(
        parent_table="P",
        parent_columns=("pid",),
        child_table="C",
        child_columns=("cid",),
        namespace="ns_p",
        orphan_policy=OrphanPolicy.REMAP,
    )
    return RelationshipGraph(edges=(edge,), ordering=())


def _peak_rss_mib() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # KiB on Linux


class DigestSink:
    """Folds each emitted table into a digest by logical content and keeps nothing."""

    def __init__(self) -> None:
        self.digest = hashlib.sha256()

    def __call__(self, name: str, table: pa.Table) -> None:
        self.digest.update(name.encode())
        for column in table.column_names:
            self.digest.update(f"{column}:{table.schema.field(column).type}".encode())
            col = table.column(column)
            for start in range(0, len(col), _DIGEST_SLICE):
                part = col.slice(start, _DIGEST_SLICE).to_pylist()
                self.digest.update(
                    "\x1f".join("\x00" if v is None else str(v) for v in part).encode()
                )


def run(mode: str, rows: int) -> dict[str, Any]:
    from decoy_engine.execution import PandasExecutionAdapter, _sequential, _sequential_native
    from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships._namespace import NamespaceRegistry

    if mode == "python":

        def unavailable() -> None:
            raise CryptoExtensionUnavailableError("fpe companion hidden for the python round")

        _sequential_native.load_compiled_fpe_kernel = unavailable  # type: ignore[assignment]
    pinned = _sequential_native.SeqNativeRun
    _sequential.SeqNativeRun = lambda: pinned(native_threads=1)  # type: ignore[assignment,misc]
    sources = build_sources(rows)
    plan, graph = _plan(), _graph()
    baseline_mib = _peak_rss_mib()
    warmup, measured = ROUNDS[mode]
    times: list[float] = []
    fpe_times: list[float] = []
    digests: list[str] = []
    backends: set[str] = set()
    for round_no in range(warmup + measured):
        gc.collect()
        sink = DigestSink()
        start = time.perf_counter()
        result = PandasExecutionAdapter().run_sequential(
            plan,
            lambda name: sources[name],
            registry=get_default_registry(),
            relationship_graph=graph,
            namespace_registry=NamespaceRegistry(bindings=()),
            sink=sink,
        )
        elapsed = time.perf_counter() - start
        digests.append(sink.digest.hexdigest())
        backends.update(
            e["executed_backend"]
            for e in result.quality_metrics[_sequential_native.SEQ_FPE_DISPOSITION_KEY]
        )
        if round_no >= warmup:
            times.append(elapsed)
            fpe_times.append(
                sum(r.elapsed_ms for r in result.timings if r.strategy_type == "fpe") / 1000.0
            )
    return {
        "mode": mode,
        "rows_per_table": rows,
        "native_threads": 1,
        "rounds_measured": len(times),
        "seconds": times,
        "median_seconds": float(np.median(times)),
        "min_seconds": min(times),
        "max_seconds": max(times),
        "fpe_handler_median_seconds": float(np.median(fpe_times)),
        "scalar_backends": sorted(backends),
        "baseline_rss_mib": baseline_mib,
        "peak_rss_mib": _peak_rss_mib(),
        "output_digest": digests[0],
        "digest_stable_across_rounds": len(set(digests)) == 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=sorted(ROUNDS), required=True)
    parser.add_argument("--rows", type=int, default=1_000_000)
    args = parser.parse_args()
    print(json.dumps(run(args.mode, args.rows)))


if __name__ == "__main__":
    main()
