"""Predeclared D2c-i micro-benchmark: native vs Python fpe on the SEQ route, FK workload.

One mode per process so each run's peak RSS is its own. Workload, frozen before any
measurement (docs/plans/2026-10-12-phase-d-d2c-i-seq-native-framework-fpe.md, perf gate):

* parent `p`: 400,000 rows, a unique utf8 key `pk` ("k<row>", hash) and one utf8 `fpe` payload
  column of 10-digit strings with 5% nulls (default digits charset, separators preserved);
* child `c`: 400,000 rows whose `ck` references `pk` uniformly at random (every key matches);
* the hash keys run native in BOTH modes, so the only thing that differs is the fpe column;
* `run_sequential` with a digesting callable sink; the timed region is the whole call; the
  fpe-handler time is the sum of the per-strategy `fpe` timing records inside it;
* native: 1 warmup round discarded + 4 measured; python: 2 measured (Python FF1 is slow, so
  the sample is small and the p95 is coarse).

Run as `python -m tests.perf._d2c_seq_fpe_bench --mode native|python`; prints one JSON object.
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

SEED = 20261012
ROWS = 400_000
ROUNDS = {"native": (1, 4), "python": (0, 2)}  # (warmup, measured)
_DIGEST_SLICE = 500_000


def _seed(namespace: str, strategy: str = "hash") -> Any:
    from decoy_engine.plan._types import ColumnSeed

    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def build_sources() -> dict[str, pa.Table]:
    rng = np.random.default_rng(SEED)
    digits = rng.integers(10**9, 10**10, ROWS).astype(str).tolist()
    nulls = rng.random(ROWS) < 0.05
    payload = pa.array([None if n else d for d, n in zip(digits, nulls, strict=True)], pa.string())
    keys = pa.array(np.char.add("k", np.arange(ROWS).astype(str)).tolist(), type=pa.string())
    picks = np.random.default_rng(SEED).integers(0, ROWS, ROWS)
    return {
        "p": pa.table({"pk": keys, "payload": payload}),
        "c": pa.table({"ck": keys.take(pa.array(picks))}),
    }


def _plan() -> Any:
    from decoy_engine.plan._types import SeedEnvelope, TableSeed

    tables = (
        (
            "p",
            TableSeed(
                per_column=(("pk", _seed("ns_pk")), ("payload", _seed("ns_pay", "fpe"))),
                per_group=(),
            ),
        ),
        ("c", TableSeed(per_column=(("ck", _seed("ns_pk")),), per_group=())),
    )
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=b"\x11" * 8, per_table=tables))


def _graph() -> Any:
    from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph

    edge = RelationshipEdge(
        parent_table="p",
        parent_columns=("pk",),
        child_table="c",
        child_columns=("ck",),
        namespace="ns_pk",
        orphan_policy=OrphanPolicy.PRESERVE,
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


def _percentile(samples: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(samples), q))


def run(mode: str) -> dict[str, Any]:
    from decoy_engine.execution import PandasExecutionAdapter, _sequential_native
    from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
    from decoy_engine.providers_v2 import get_default_registry
    from decoy_engine.relationships._namespace import NamespaceRegistry

    if mode == "python":

        def unavailable() -> None:
            raise CryptoExtensionUnavailableError("fpe companion hidden for the python round")

        _sequential_native.load_compiled_fpe_kernel = unavailable  # type: ignore[assignment]
    sources = build_sources()
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
    peak_mib = _peak_rss_mib()
    return {
        "mode": mode,
        "rows": ROWS,
        "rounds_measured": len(times),
        "seconds": times,
        "p50_seconds": _percentile(times, 50),
        "p95_seconds": _percentile(times, 95),
        "fpe_handler_p50_seconds": _percentile(fpe_times, 50),
        "scalar_backends": sorted(backends),
        "baseline_rss_mib": baseline_mib,
        "peak_rss_mib": peak_mib,
        "output_digest": digests[0],
        "digest_stable_across_rounds": len(set(digests)) == 1,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=sorted(ROUNDS), required=True)
    print(json.dumps(run(parser.parse_args().mode)))


if __name__ == "__main__":
    main()
