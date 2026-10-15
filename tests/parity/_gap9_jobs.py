"""GAP9 shared jobs: one FK job per bounded route, run through the public `run_pipeline`.

The sequential job covers every native-admitted strategy that route owns (hash, categorical,
bucket, deterministic index-faker, fpe, date_shift) plus nulls, duplicate keys, orphans and a
REMAP child, so the parent decisions are read back after the parent frame is evicted. The
out-of-core job covers what that route admits (hash, categorical, bucket, fpe) on a lazy source
and an incremental sink, in both drivers: the batch joiner and the external-reorder lane.

Only counts and table names are recorded by the spies; never a cell value.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
from decoy_engine.execution._operator_registry import RUST_COMPANION
from decoy_engine.execution.out_of_core import _budget as _ooc_budget
from decoy_engine.execution.out_of_core import _runner
from decoy_engine.profile._readers import LazySource

VERSION = "0.1.0"
THREADS = 4
_MIB = 1 << 20

_DIGITS = [f"{(i * 7919) % 10**10:010d}" for i in range(97)]
_REGIONS = ["north", "south", "east", "west"]


def _parent_rows(n: int) -> pa.Table:
    ids = [f"p{i % (n - 3):04d}" for i in range(n)]  # 3 duplicate keys
    return pa.table(
        {
            "id": pa.array(ids),
            "name": pa.array([None if i % 11 == 0 else f"Name {i % 37}" for i in range(n)]),
            "region": pa.array([_REGIONS[i % 4] if i % 13 else None for i in range(n)]),
            "dob": pa.array(
                [
                    None if i % 17 == 0 else f"19{50 + i % 40}-{1 + i % 12:02d}-{1 + i % 27:02d}"
                    for i in range(n)
                ]
            ),
            "ssn": pa.array([None if i % 19 == 0 else _DIGITS[i % 97] for i in range(n)]),
            "nick": pa.array([None if i % 9 == 0 else f"n{i % 41}" for i in range(n)]),
            "alias": pa.array([None if i % 8 == 0 else f"a{i % 43}" for i in range(n)]),
            "email": pa.array([f"user{i}@example.com" for i in range(n)]),
            "joined": pa.array(
                [
                    None if i % 23 == 0 else f"2020-{1 + i % 12:02d}-{1 + i % 27:02d}"
                    for i in range(n)
                ]
            ),
        }
    )


def _child_rows(n: int, parents: int) -> pa.Table:
    refs: list[str | None] = []
    for i in range(n):
        if i % 29 == 0:
            refs.append(None)
        elif i % 7 == 0:
            refs.append(f"orphan{i}")
        else:
            refs.append(f"p{i % (parents - 3):04d}")
    return pa.table(
        {
            "id": pa.array([f"c{i:05d}" for i in range(n)]),
            "parent_id": pa.array(refs, type=pa.string()),
            "acct": pa.array([None if i % 31 == 0 else _DIGITS[i % 97] for i in range(n)]),
            "tag": pa.array([f"t{i}" for i in range(n)]),
        }
    )


def _key(name: str, ns: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": ns}


_DIGIT_FPE = {"charset": "digits"}


def _config(
    tmp: Path,
    tables: dict[str, pa.Table],
    parent_cols: list[dict[str, Any]],
    child_cols: list[dict[str, Any]],
    orphan_policy: str,
) -> tuple[dict[str, Any], dict[str, Path]]:
    paths = {}
    for name, table in tables.items():
        paths[name] = tmp / f"{name}.parquet"
        pq.write_table(table, paths[name])
    config = {
        "version": 1,
        "global_settings": {"job_name": "gap9", "seed": 11},
        "sources": {
            n: {"type": "file", "path": str(p), "format": "parquet"} for n, p in paths.items()
        },
        "targets": {
            n: {"type": "file", "path": str(tmp / f"{n}.cfg.out.parquet"), "format": "parquet"}
            for n in paths
        },
        "tables": [
            {"name": "parent", "columns": [_key("id", "pns"), *parent_cols]},
            {"name": "child", "columns": [_key("parent_id", "pns"), *child_cols]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": orphan_policy,
                "namespace": "pns",
            }
        ],
    }
    return config, paths


def seq_job(
    tmp: Path, *, orphan_policy: str = "remap", rows: int = 240, legacy_faker: bool = True
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """The sequential job and its resident sources.

    `nick` is a deterministic faker, which the sequential route still serves from the legacy
    pool sampler (a documented GAP9 remaining gap that passes no thread budget). The kernel-level
    wiring checks drop it with `legacy_faker=False` so every kernel call is one GAP9 owns."""
    parent_cols: list[dict[str, Any]] = [
        {"name": "name", "strategy": "hash", "namespace": "nm"},
        {
            "name": "region",
            "strategy": "categorical",
            "namespace": "rg",
            "deterministic": True,
            "provider_config": {"categories": [*_REGIONS, "none"]},
        },
        {
            "name": "dob",
            "strategy": "bucket_perturb",
            "namespace": "bk",
            "provider_config": {"bucket": "month", "date_format": "%Y-%m-%d"},
        },
        {
            "name": "ssn",
            "strategy": "fpe",
            "namespace": "ss",
            "provider_config": _DIGIT_FPE,
        },
        {
            "name": "alias",
            "strategy": "faker",
            "provider": "person_first_name",
            "deterministic": False,
            "namespace": "al",
        },
        {
            "name": "joined",
            "strategy": "date_shift",
            "namespace": "js",
            "provider_config": {"min_days": -5, "max_days": 5, "date_format": "%Y-%m-%d"},
        },
    ]
    if legacy_faker:
        parent_cols.append(
            {
                "name": "nick",
                "strategy": "faker",
                "provider": "person_first_name",
                "deterministic": True,
                "namespace": "em",
            }
        )
    child_cols: list[dict[str, Any]] = [
        {"name": "acct", "strategy": "fpe", "namespace": "ac", "provider_config": _DIGIT_FPE},
        {"name": "tag", "strategy": "passthrough"},
    ]
    parent, child = _parent_rows(rows), _child_rows(rows * 2, rows)
    config, paths = _config(
        tmp, {"parent": parent, "child": child}, parent_cols, child_cols, orphan_policy
    )
    return config, {n: pq.read_table(p) for n, p in paths.items()}


def ooc_job(
    tmp: Path, *, orphan_policy: str = "preserve", rows: int = 600
) -> tuple[dict[str, Any], dict[str, Path]]:
    """The out-of-core job and its parquet paths (lazy sources)."""
    parent_cols: list[dict[str, Any]] = [
        {"name": "name", "strategy": "hash", "namespace": "nm"},
        {
            "name": "region",
            "strategy": "categorical",
            "namespace": "rg",
            "deterministic": True,
            "provider_config": {"categories": [*_REGIONS, "none"]},
        },
        {
            "name": "dob",
            "strategy": "bucket_perturb",
            "namespace": "bk",
            "provider_config": {"bucket": "month", "date_format": "%Y-%m-%d"},
        },
        {"name": "ssn", "strategy": "fpe", "namespace": "ss", "provider_config": _DIGIT_FPE},
        {"name": "email", "strategy": "redact"},
        {"name": "joined", "strategy": "truncate", "provider_config": {"length": 4}},
    ]
    child_cols: list[dict[str, Any]] = [
        {"name": "acct", "strategy": "fpe", "namespace": "ac", "provider_config": _DIGIT_FPE},
        {"name": "tag", "strategy": "passthrough"},
    ]
    parent, child = _parent_rows(rows), _child_rows(rows * 2, rows)
    return _config(tmp, {"parent": parent, "child": child}, parent_cols, child_cols, orphan_policy)


def composite_job(
    tmp: Path, *, rows: int = 300
) -> tuple[dict[str, Any], dict[str, pa.Table], dict[str, Path]]:
    """A two-column FK (composite key) job: sources for sequential, paths for out-of-core."""
    parent = pa.table(
        {
            "a": pa.array([f"a{i % 50}" for i in range(rows)]),
            "b": pa.array([f"b{i // 50}" for i in range(rows)]),
            "name": pa.array([f"Name {i % 37}" for i in range(rows)]),
        }
    )
    n = rows * 2
    child = pa.table(
        {
            "a_fk": pa.array(
                [
                    None if i % 29 == 0 else (f"zz{i}" if i % 7 == 0 else f"a{i % 50}")
                    for i in range(n)
                ]
            ),
            "b_fk": pa.array([f"b{(i // 2) % (rows // 50)}" for i in range(n)]),
            "tag": pa.array([f"t{i}" for i in range(n)]),
        }
    )
    paths = {}
    for name, table in (("parent", parent), ("child", child)):
        paths[name] = tmp / f"{name}.parquet"
        pq.write_table(table, paths[name])
    config = {
        "version": 1,
        "global_settings": {"job_name": "gap9-composite", "seed": 11},
        "sources": {
            n_: {"type": "file", "path": str(p), "format": "parquet"} for n_, p in paths.items()
        },
        "targets": {
            n_: {"type": "file", "path": str(tmp / f"{n_}.cfg.out.parquet"), "format": "parquet"}
            for n_ in paths
        },
        "tables": [
            {
                "name": "parent",
                "columns": [
                    _key("a", "pns_a"),
                    _key("b", "pns_b"),
                    {"name": "name", "strategy": "hash", "namespace": "nm"},
                ],
            },
            {
                "name": "child",
                "columns": [
                    _key("a_fk", "pns_a"),
                    _key("b_fk", "pns_b"),
                    {"name": "tag", "strategy": "passthrough"},
                ],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["a", "b"]},
                "children": [{"table": "child", "columns": ["a_fk", "b_fk"]}],
                "orphan_policy": "preserve",
                "namespace": "pns",
            }
        ],
    }
    return config, {n_: pq.read_table(p) for n_, p in paths.items()}, paths


def run_seq(config: dict[str, Any], sources: dict[str, pa.Table], **kwargs: Any) -> Any:
    return run_pipeline(
        config, dict(sources), engine_version=VERSION, execution_mode="sequential", **kwargs
    )


@contextmanager
def small_batches(batch_rows: int) -> Iterator[None]:
    """Force several batches per table on the out-of-core route (output is batch-size invariant)."""
    real = _ooc_budget.resolve_ooc_memory_limit

    def resolve(*a: Any, **k: Any) -> Any:
        return dataclasses.replace(real(*a, **k), batch_rows=batch_rows)

    import decoy_engine.execution.out_of_core as pkg

    with mock.patch.object(pkg, "resolve_ooc_memory_limit", resolve):
        yield


def run_ooc(
    config: dict[str, Any],
    paths: dict[str, Path],
    out_dir: Path,
    *,
    lane: str,
    batch_rows: int | None = None,
    **kwargs: Any,
) -> tuple[Any, dict[str, pa.Table]]:
    """Run out-of-core into a parquet sink; `lane` is "batch" or "reorder"."""
    assert lane in ("batch", "reorder")
    sink = ParquetTransactionalSink(out_dir)
    lazy = {n: LazySource(path=p) for n, p in paths.items()}
    with small_batches(batch_rows) if batch_rows else _null():
        result = run_pipeline(
            config,
            lazy,
            engine_version=VERSION,
            execution_mode="out_of_core",
            sink=sink,
            out_of_core_budget_bytes=256 * _MIB,
            out_of_core_reorder_threshold_rows=0 if lane == "reorder" else 10**12,
            **kwargs,
        )
    return result, {n: pq.read_table(out_dir / f"{n}.parquet") for n in paths}


@contextmanager
def _null() -> Iterator[None]:
    yield


def digest(tables: dict[str, pa.Table]) -> str:
    """Schema + row order + values (+ field metadata) of every table, as one hex digest."""
    h = hashlib.sha256()
    for name in sorted(tables):
        table = tables[name].combine_chunks()
        h.update(name.encode())
        h.update(table.schema.serialize().to_pybytes())
        sink = pa.BufferOutputStream()
        with pa.ipc.new_stream(sink, table.schema) as writer:
            for batch in table.to_batches():
                writer.write_batch(batch)
        h.update(sink.getvalue().to_pybytes())
    return h.hexdigest()


def native_columns(quality_metrics: dict[str, Any]) -> set[tuple[str, str]]:
    """(table, column) pairs a disposition entry says executed on the compiled kernel."""
    return {
        (e["table"], e["column"])
        for v in quality_metrics.values()
        if isinstance(v, list)
        for e in v
        if isinstance(e, dict)
        and e.get("executed_backend") == RUST_COMPANION
        and "column" in e
        and "table" in e
    }


# ---- spies --------------------------------------------------------------------------


@dataclasses.dataclass
class KernelCalls:
    """Every `native_threads` the compiled kernel wrappers were called with, per kind."""

    calls: list[tuple[str, Any]] = dataclasses.field(default_factory=list)

    def values(self, kind: str | None = None) -> set[Any]:
        return {v for k, v in self.calls if kind is None or k == kind}

    def kinds(self) -> set[str]:
        return {k for k, _ in self.calls}


@contextmanager
def spy_kernels() -> Iterator[KernelCalls]:
    """Record the `native_threads` argument at the compiled-kernel wrapper boundary."""
    from decoy_engine.execution.native import _crypto_ext, _fpe_ext, _index_ext

    rec = KernelCalls()
    targets = [
        ("hash", _crypto_ext._CompiledKeyedDerivationKernel, "derive_batch"),
        ("index", _index_ext._CompiledIndexKernel, "derive_index_batch"),
        ("fpe", _fpe_ext._CompiledFpeKernel, "run"),
    ]
    patches = []
    for kind, cls, attr in targets:
        original: Callable[..., Any] = getattr(cls, attr)

        def wrapper(
            self: Any, *a: Any, _o: Callable[..., Any] = original, _k: str = kind, **k: Any
        ) -> Any:
            rec.calls.append((_k, k.get("native_threads", "MISSING")))
            return _o(self, *a, **k)

        patches.append(mock.patch.object(cls, attr, wrapper))
    for p in patches:
        p.start()
    try:
        yield rec
    finally:
        for p in patches:
            p.stop()


@dataclasses.dataclass
class RunObjects:
    """Constructed run objects and what each OOC driver received."""

    seq_runs: list[int | None] = dataclasses.field(default_factory=list)
    ooc_runs: list[int | None] = dataclasses.field(default_factory=list)
    # (table, driver, id(hash_run), hash_run's native_threads) per driver call
    drivers: list[tuple[str, str, int, int | None]] = dataclasses.field(default_factory=list)
    ooc_run_ids: list[int] = dataclasses.field(default_factory=list)
    bound_threads: list[int | None] = dataclasses.field(default_factory=list)


@contextmanager
def spy_run_objects() -> Iterator[RunObjects]:
    """Spy on SeqNativeRun / OocHashRun construction and on the hash_run each driver gets."""
    from decoy_engine.execution._sequential_native import SeqNativeRun
    from decoy_engine.execution.out_of_core._hash_run import OocHashRun

    rec = RunObjects()
    seq_init, ooc_init = SeqNativeRun.__init__, OocHashRun.__init__
    real_stream, real_batch = _runner.stream_table, _runner._stream_table

    def seq(self: Any, *a: Any, **k: Any) -> None:
        seq_init(self, *a, **k)
        rec.seq_runs.append(self._native_threads)

    def ooc(self: Any, *a: Any, **k: Any) -> None:
        ooc_init(self, *a, **k)
        rec.ooc_runs.append(self._native_threads)
        rec.ooc_run_ids.append(id(self))

    def driver(name: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def call(**kw: Any) -> Any:
            hr = kw["hash_run"]
            rec.drivers.append((kw["table_name"], name, id(hr), hr._native_threads))
            return real(**kw)

        return call

    with (
        mock.patch.object(SeqNativeRun, "__init__", seq),
        mock.patch.object(OocHashRun, "__init__", ooc),
        mock.patch.object(_runner, "stream_table", driver("reorder", real_stream)),
        mock.patch.object(_runner, "_stream_table", driver("batch", real_batch)),
    ):
        yield rec
