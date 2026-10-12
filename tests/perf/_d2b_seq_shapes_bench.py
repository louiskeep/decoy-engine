"""Predeclared D2b micro-benchmark: native vs Python hash across FK shapes on the SEQ route.

One mode per process so each run's peak RSS is its own. Workload, frozen before any measurement
(docs/plans/2026-10-12-phase-d-d2b-seq-fk-shapes.md, perf gate):

* chain `a -> b -> c` on a utf8 `k`: `a` has 1,000,000 unique keys; `b` and `c` each hold
  1,000,000 rows drawn uniformly from the table above them plus 1% keys that table does not own,
  so every hop mints 10,000 orphans through REMAP;
* composite `p.(k, j) -> q.(k, j)`: `p` has 1,000,000 rows of two unique utf8 columns, `q` draws
  1,000,000 tuples from `p`, so the composite child inherits and no member is rehashed;
* every key column is a `hash` column; `run_sequential` with a digesting callable sink; the timed
  region is the whole call;
* native: 1 warmup round discarded + 3 measured; python: 1 measured (the Python round is minutes
  long, so its p95 is coarse).

Run as `python -m tests.perf._d2b_seq_shapes_bench --mode native|python`; prints one JSON object.
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
ROWS = 1_000_000
ORPHANS = ROWS // 100
ROUNDS = {"native": (1, 3), "python": (0, 1)}  # (warmup, measured)


def _keys(prefix: str, count: int, offset: int = 0) -> pa.Array:
    return pa.array(
        np.char.add(prefix, (np.arange(count) + offset).astype(str)).tolist(), type=pa.string()
    )


def build_sources() -> dict[str, pa.Table]:
    rng = np.random.default_rng(SEED)
    a = _keys("a", ROWS)
    owned_b = a.take(pa.array(rng.integers(0, ROWS, ROWS - ORPHANS)))
    b = pa.concat_arrays([owned_b, _keys("ob", ORPHANS)])
    owned_c = b.take(pa.array(rng.integers(0, ROWS, ROWS - ORPHANS)))
    c = pa.concat_arrays([owned_c, _keys("oc", ORPHANS)])
    pk, pj = _keys("pk", ROWS), _keys("pj", ROWS)
    picks = pa.array(rng.integers(0, ROWS, ROWS))
    return {
        "a": pa.table({"k": a}),
        "b": pa.table({"k": b}),
        "c": pa.table({"k": c}),
        "p": pa.table({"k": pk, "j": pj}),
        "q": pa.table({"k": pk.take(picks), "j": pj.take(picks)}),
    }


def _shape() -> tuple[Any, Any]:
    from decoy_engine.plan._types import GroupSeed, SeedEnvelope, TableSeed
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

    def edge(p: str, pc: tuple[str, ...], c: str, cc: tuple[str, ...], ns: str) -> Any:
        return RelationshipEdge(p, pc, c, cc, ns, OrphanPolicy.REMAP)

    tables = (
        ("a", TableSeed(per_column=(("k", _seed("ns_ab")),), per_group=())),
        ("b", TableSeed(per_column=(("k", _seed("ns_bc")),), per_group=())),
        ("c", TableSeed(per_column=(("k", _seed("ns_bc")),), per_group=())),
        ("p", TableSeed(per_column=(("k", _seed("ns_pk")), ("j", _seed("ns_pj"))), per_group=())),
        (
            "q",
            TableSeed(
                per_column=(),
                per_group=(("j__k", GroupSeed(namespace="ns_q", coherent_columns=("k", "j"))),),
            ),
        ),
    )
    plan = SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=b"\x11" * 8, per_table=tables))
    graph = RelationshipGraph(
        edges=(
            edge("a", ("k",), "b", ("k",), "ns_ab"),
            edge("b", ("k",), "c", ("k",), "ns_bc"),
            edge("p", ("k", "j"), "q", ("k", "j"), "ns_q"),
        ),
        ordering=(),
    )
    return plan, graph


def run(mode: str) -> dict[str, Any]:
    from decoy_engine.execution import PandasExecutionAdapter, _sequential_native
    from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships._namespace import NamespaceRegistry

    if mode == "python":

        def unavailable() -> None:
            raise CryptoExtensionUnavailableError("companion hidden for the python round")

        _sequential_native.load_compiled_crypto_kernel = unavailable  # type: ignore[assignment]
    sources = build_sources()
    plan, graph = _shape()
    baseline_mib = _peak_rss_mib()
    warmup, measured = ROUNDS[mode]
    times: list[float] = []
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
            for e in result.quality_metrics[_sequential_native.SEQ_HASH_DISPOSITION_KEY]
            if e["path"] in ("scalar", "fk_remap")
        )
        if round_no >= warmup:
            times.append(elapsed)
    return {
        "mode": mode,
        "rows": ROWS,
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
