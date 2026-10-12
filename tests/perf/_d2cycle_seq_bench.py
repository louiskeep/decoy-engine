"""Predeclared D2-cycle micro-benchmark: a hash-keyed three-table cycle on the SEQ route.

One mode per process so each run's peak RSS is its own. Workload, frozen before any measurement
(docs/plans/2026-10-12-phase-d-d2cycle-bounded-fk-cycles.md, perf gate: capability first, then
native speed on the key columns):

* a cycle `a.id -> b.x`, `b.y -> c.p`, `c.q -> a.z` over three tables of 500,000 rows. `id`, `y`
  and `q` are unique utf8 producer keys; `x`, `p` and `z` each draw 500,000 rows from their
  parent plus 1% keys the parent does not own, so every hop mints 5,000 orphans through REMAP;
* `a` also carries a hashed 24-byte utf8 payload column, so the pass-2 table work is not only keys;
* every key and the payload is a `hash` column; `run_sequential` with a digesting callable sink and
  a `CycleContext` whose projected read serves only the requested columns; the timed region is the
  whole call;
* native: 1 warmup round discarded + 3 measured; python: 1 measured; `full_frame`: the oracle
  `run`, 1 measured, for the memory comparison only.

Run as `python -m tests.perf._d2cycle_seq_bench --mode native|python|full_frame`; prints one JSON
object.
"""

from __future__ import annotations

import argparse
import gc
import json
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pyarrow as pa

from tests.perf._d2a_seq_hash_bench import DigestSink, _peak_rss_mib, _percentile, _seed

SEED = 20261012
ROWS = 500_000
ORPHANS = ROWS // 100
ROUNDS = {"native": (1, 3), "python": (0, 1), "full_frame": (0, 1)}  # (warmup, measured)


def _keys(prefix: str, count: int, offset: int = 0) -> pa.Array:
    return pa.array(
        np.char.add(prefix, (np.arange(count) + offset).astype(str)).tolist(), type=pa.string()
    )


def build_sources() -> dict[str, pa.Table]:
    rng = np.random.default_rng(SEED)
    a_id, b_y, c_q = _keys("a", ROWS), _keys("b", ROWS), _keys("c", ROWS)

    def child(parent: pa.Array, orphan_prefix: str) -> pa.Array:
        owned = parent.take(pa.array(rng.integers(0, ROWS, ROWS - ORPHANS)))
        return pa.concat_arrays([owned, _keys(orphan_prefix, ORPHANS)])

    payload = pa.array(
        np.char.add("payload-", rng.integers(0, 10**15, ROWS).astype(str)).tolist(), pa.string()
    )
    return {
        "a": pa.table({"id": a_id, "z": child(c_q, "oz"), "pay": payload}),
        "b": pa.table({"x": child(a_id, "ox"), "y": b_y}),
        "c": pa.table({"p": child(b_y, "op"), "q": c_q}),
    }


def _shape() -> tuple[Any, Any]:
    from decoy_engine.plan._types import SeedEnvelope, TableSeed
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

    def edge(p: str, pc: str, c: str, cc: str, ns: str) -> Any:
        return RelationshipEdge(p, (pc,), c, (cc,), ns, OrphanPolicy.REMAP)

    tables = (
        (
            "a",
            TableSeed(
                per_column=(("id", _seed("ns_a")), ("z", _seed("ns_c")), ("pay", _seed("ns_pay"))),
                per_group=(),
            ),
        ),
        ("b", TableSeed(per_column=(("x", _seed("ns_a")), ("y", _seed("ns_b"))), per_group=())),
        ("c", TableSeed(per_column=(("p", _seed("ns_b")), ("q", _seed("ns_c"))), per_group=())),
    )
    plan = SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=b"\x11" * 8, per_table=tables))
    graph = RelationshipGraph(
        edges=(
            edge("a", "id", "b", "x", "ns_a"),
            edge("b", "y", "c", "p", "ns_b"),
            edge("c", "q", "a", "z", "ns_c"),
        ),
        ordering=(),
    )
    return plan, graph


def run(mode: str) -> dict[str, Any]:
    from decoy_engine.execution import PandasExecutionAdapter, _sequential_native
    from decoy_engine.execution._sequential import run_sequential
    from decoy_engine.execution._sequential_cycle_admit import CycleContext
    from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships._namespace import NamespaceRegistry

    if mode == "python":

        def unavailable() -> None:
            raise CryptoExtensionUnavailableError("companion hidden for the python round")

        _sequential_native.load_compiled_crypto_kernel = unavailable  # type: ignore[assignment]
    sources = build_sources()
    plan, graph = _shape()
    ctx = CycleContext(
        budget_bytes=8 * 1024**3,
        table_rows={t: tbl.num_rows for t, tbl in sources.items()},
        table_bytes={t: 3 * tbl.nbytes for t, tbl in sources.items()},
        projected_loader=lambda table, columns: sources[table].select(list(columns)),
    )
    baseline_mib = _peak_rss_mib()
    warmup, measured = ROUNDS[mode]
    times: list[float] = []
    digests: list[str] = []
    backends: set[str] = set()
    for round_no in range(warmup + measured):
        gc.collect()
        sink = DigestSink()
        start = time.perf_counter()
        if mode == "full_frame":
            outputs = (
                PandasExecutionAdapter()
                .run(
                    plan,
                    sources,
                    registry=get_default_registry(),
                    relationship_graph=graph,
                    namespace_registry=NamespaceRegistry(bindings=()),
                )
                .outputs
            )
            for name in sorted(outputs):
                sink(name, outputs[name])
            del outputs
        else:
            result = run_sequential(
                PandasExecutionAdapter(),
                plan,
                lambda name: sources[name],
                registry=get_default_registry(),
                relationship_graph=graph,
                namespace_registry=NamespaceRegistry(bindings=()),
                sink=sink,
                cycle_context=ctx,
            )
            backends.update(
                e["executed_backend"]
                for e in result.quality_metrics[_sequential_native.SEQ_HASH_DISPOSITION_KEY]
                if e["path"] in ("scalar", "fk_remap")
            )
        elapsed = time.perf_counter() - start
        digests.append(sink.digest.hexdigest())
        if round_no >= warmup:
            times.append(elapsed)
    return {
        "mode": mode,
        "rows_per_table": ROWS,
        "rounds_measured": len(times),
        "seconds": times,
        "p50_seconds": _percentile(times, 50),
        "p95_seconds": _percentile(times, 95),
        "backends": sorted(backends),
        "baseline_rss_mib": baseline_mib,
        "peak_rss_mib": _peak_rss_mib(),
        "output_digest": digests[0],
        "digest_stable_across_rounds": len(set(digests)) == 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=sorted(ROUNDS), required=True)
    print(json.dumps(run(parser.parse_args().mode)))


if __name__ == "__main__":
    main()
