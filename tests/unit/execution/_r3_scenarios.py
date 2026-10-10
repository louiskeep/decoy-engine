"""Scenario catalogue for the R3 pre-extraction baselines.

Plan: docs/plans/2026-10-10-r3-run-context-route-executors.md sections 6 and 8.
Every scenario drives the PUBLIC `run_pipeline` entry, so the same catalogue
characterizes the code before and after the executors are extracted. The recorded
baselines (`r3_within_route_baseline.json`, `r3_failure_trace_baseline.json`) were
captured on origin/main @ ac6a0e8e before any production edit; regenerating them
needs reviewer approval (`R3_WRITE_BASELINES=1`), because a regenerated baseline can
bless a behavior change.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import ParquetTransactionalSink, run_pipeline
from decoy_engine.keyprovider import SecretKeyProvider
from decoy_engine.profile._readers import LazySource
from tests.unit.execution._r3_project import canon, project, project_table

ENGINE_VERSION = "r3-baseline"
N = 20
NOW_ISO = "2026-10-10T00:00:00+00:00"


@dataclass
class Run:
    """A scenario's outcome: the result, and the tables its sink received (if any)."""

    result: Any
    sink_dir: Path | None = None
    files: tuple[Path, ...] = ()


@contextlib.contextmanager
def without_companion() -> Iterator[None]:
    """Hide the compiled companion, as on a machine without it, so the recorded evidence does
    not depend on whether the box that ran the scenario has it. Restores only its own entry:
    `mock.patch.dict(sys.modules)` would also drop every module imported inside the block."""
    missing = object()
    old = sys.modules.get("decoy_engine_native", missing)
    sys.modules["decoy_engine_native"] = None  # type: ignore[assignment]
    try:
        yield
    finally:
        if old is missing:
            sys.modules.pop("decoy_engine_native", None)
        else:
            sys.modules["decoy_engine_native"] = old  # type: ignore[assignment]


def key_provider() -> SecretKeyProvider:
    return SecretKeyProvider(secret=b"r3-baseline-mask-key-0123456789ab", key_version="v1")


def _write(tmp: Path, table: pa.Table, name: str) -> str:
    path = tmp / f"{name}.parquet"
    pq.write_table(table, path)
    return str(path)


def _target(tmp: Path, name: str) -> dict[str, Any]:
    return {"type": "file", "format": "parquet", "path": str(tmp / f"{name}.out.parquet")}


def _hash(name: str, ns: str) -> dict[str, Any]:
    return {"name": name, "strategy": "hash", "namespace": ns}


# ---------------------------------------------------------------------------
# Config builders
# ---------------------------------------------------------------------------


def single_table(tmp: Path, *, hash_column: bool = True) -> tuple[dict[str, Any], pa.Table]:
    data = {
        "p": pa.array([f"x{i}" for i in range(N)]),
        "r": pa.array([f"secret{i}" for i in range(N)]),
        "tr": pa.array([f"abcdef{i:02d}" for i in range(N)]),
    }
    columns: list[dict[str, Any]] = [
        {"name": "p", "strategy": "passthrough"},
        {"name": "r", "strategy": "redact"},
        {"name": "tr", "strategy": "truncate", "provider_config": {"length": 3}},
    ]
    if hash_column:
        data["h"] = pa.array([f"user{i}@example.test" for i in range(N)])
        columns.append(_hash("h", "ns_h"))
    table = pa.table(data)
    cfg = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                "t": {"type": "file", "format": "parquet", "path": _write(tmp, table, "t")}
            },
            "tables": [{"name": "t", "columns": columns}],
            "targets": {"t": _target(tmp, "t")},
        }
    ).model_dump()
    return cfg, table


def two_tables(tmp: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    tables = {
        "a": pa.table({"k": pa.array([f"a{i}" for i in range(N)]), "v": pa.array(range(N))}),
        "b": pa.table({"k": pa.array([f"b{i}" for i in range(N)]), "w": pa.array(range(N))}),
    }
    cfg = PipelineConfig.model_validate(
        {
            "version": 1,
            "global_settings": {"seed": 42},
            "sources": {
                n: {"type": "file", "format": "parquet", "path": _write(tmp, t, n)}
                for n, t in tables.items()
            },
            "tables": [
                {
                    "name": "a",
                    "columns": [_hash("k", "ns_a"), {"name": "v", "strategy": "passthrough"}],
                },
                {
                    "name": "b",
                    "columns": [
                        {"name": "k", "strategy": "redact"},
                        {"name": "w", "strategy": "passthrough"},
                    ],
                },
            ],
            "targets": {n: _target(tmp, n) for n in tables},
        }
    ).model_dump()
    return cfg, tables


def bad_date_table(tmp: Path, *, quarantine: Path | None) -> tuple[dict[str, Any], pa.Table]:
    """One date_shift column with a single unparseable cell: a row error the job either
    fails on or, with quarantine on, routes to the quarantine file."""
    values = [f"2020-01-{1 + i % 28:02d}" for i in range(N)]
    values[7] = "not-a-date"
    table = pa.table({"d": pa.array(values), "k": pa.array(range(N))})
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": 42},
        "sources": {"t": {"type": "file", "format": "parquet", "path": _write(tmp, table, "t")}},
        "tables": [
            {
                "name": "t",
                "columns": [
                    {
                        "name": "d",
                        "strategy": "date_shift",
                        "namespace": "d_ns",
                        "provider_config": {
                            "min_days": -3,
                            "max_days": 3,
                            "date_format": "%Y-%m-%d",
                        },
                    },
                    {"name": "k", "strategy": "passthrough"},
                ],
            }
        ],
        "targets": {"t": _target(tmp, "t")},
    }
    if quarantine is not None:
        raw["quarantine"] = {
            "enabled": True,
            "output_path": str(quarantine),
            "triggers": ["format_error"],
        }
    return PipelineConfig.model_validate(raw).model_dump(), table


def fk_job(
    tmp: Path,
    *,
    orphan_policy: str = "preserve",
    orphans: bool = False,
    parent_transforms: bool = False,
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """Pure-mask parent/child FK job, every strategy inside the out-of-core supported set."""
    child_refs = [f"p{i}" if not (orphans and i % 5 == 0) else f"zz{i}" for i in range(N)]
    tables = {
        "parent": pa.table(
            {
                "id": pa.array([f"p{i}" for i in range(N)]),
                "note": pa.array([f"secret{i}" for i in range(N)]),
            }
        ),
        "child": pa.table(
            {"cid": pa.array([f"c{i}" for i in range(N)]), "parent_id": pa.array(child_refs)}
        ),
    }
    cfg = {
        "version": 1,
        "global_settings": {"seed": 7},
        "sources": {
            n: {"type": "file", "path": _write(tmp, t, n), "format": "parquet"}
            for n, t in tables.items()
        },
        "targets": {n: _target(tmp, n) for n in tables},
        "tables": [
            {
                "name": "parent",
                "columns": [_hash("id", "ns"), {"name": "note", "strategy": "redact"}],
            },
            {"name": "child", "columns": [_hash("cid", "cns"), _hash("parent_id", "ns")]},
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["parent_id"]}],
                "orphan_policy": orphan_policy,
                "namespace": "ns",
            }
        ],
    }
    if parent_transforms:
        # A no-op dedupe still marks the table transform-bearing, which declines out-of-core.
        cfg["tables"][0]["transforms"] = [{"op": "dedupe", "columns": ["id"]}]
        cfg = PipelineConfig.model_validate(cfg).model_dump()
    return cfg, tables


def generate_plus_mask(tmp: Path) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    """FK mask pair plus a generate table: not sequential-eligible, so full_frame."""
    cfg, tables = fk_job(tmp)
    cfg["tables"] = [
        {"name": "parent", "columns": [_hash("id", "ns")]},
        {"name": "child", "columns": [_hash("parent_id", "ns")]},
        {
            "name": "extra",
            "row_count": 3,
            "generate_columns": [{"name": "seq", "type": "sequence", "start": 1, "step": 1}],
        },
    ]
    cfg["targets"]["extra"] = _target(tmp, "extra")
    return cfg, tables


# ---------------------------------------------------------------------------
# Success scenarios: (config, sources, kwargs) per scenario, run through run_pipeline
# ---------------------------------------------------------------------------


def _go(cfg: dict[str, Any], sources: Any, **kw: Any) -> Any:
    return run_pipeline(cfg, sources, engine_version=ENGINE_VERSION, **kw)


def _sink_run(tmp: Path, cfg: dict[str, Any], sources: Any, **kw: Any) -> Run:
    out = tmp / "sink_out"
    return Run(_go(cfg, sources, sink=ParquetTransactionalSink(out), **kw), out)


# Routing facts shared by the bounded FK scenarios: byte-estimate routing is pinned off so
# the 20-row fixtures route by the row-count thresholds the kwargs set.
_ROW_ROUTING = {"use_byte_estimate_routing": False}


def ff_legacy_single(tmp: Path) -> Run:
    cfg, table = single_table(tmp)
    return Run(_go(cfg, {"t": table}, unified_slice_enabled=False))


def ff_unified_admitted(tmp: Path) -> Run:
    cfg, table = single_table(tmp, hash_column=False)
    return Run(_go(cfg, {"t": table}, key_provider=key_provider()))


def ff_unified_fallback(tmp: Path) -> Run:
    """Cheap admission passes, the physical plan build raises, the lane returns None and the
    pandas oracle completes the job."""
    cfg, table = single_table(tmp, hash_column=False)
    boom = mock.Mock(side_effect=RuntimeError("injected: physical plan inputs"))
    with mock.patch(
        "decoy_engine.execution.physical._live_inputs.build_live_physical_plan_inputs", boom
    ):
        return Run(_go(cfg, {"t": table}, key_provider=key_provider()))


def ff_unified_flag_off(tmp: Path) -> Run:
    cfg, table = single_table(tmp, hash_column=False)
    return Run(_go(cfg, {"t": table}, key_provider=key_provider(), unified_slice_enabled=False))


def ff_multi_table(tmp: Path) -> Run:
    cfg, tables = two_tables(tmp)
    return Run(_go(cfg, tables))


def ff_generate_mask(tmp: Path) -> Run:
    cfg, tables = generate_plus_mask(tmp)
    return Run(_go(cfg, tables))


def ff_multi_table_split(tmp: Path) -> Run:
    cfg, tables = two_tables(tmp)
    return Run(
        _go(
            cfg,
            tables,
            auto_chunk_threshold_rows=10,
            chunk_size_rows=8,
            use_byte_estimate_routing=False,
        )
    )


def ff_quarantine_format_error(tmp: Path) -> Run:
    q = tmp / "quarantine.jsonl"
    cfg, table = bad_date_table(tmp, quarantine=q)
    result = _go(cfg, {"t": table}, unified_slice_enabled=False)
    return Run(result, files=(q,))


def ff_fidelity(tmp: Path) -> Run:
    cfg, table = single_table(tmp)
    return Run(_go(cfg, {"t": table}, fidelity_report=True, now_iso=NOW_ISO))


def ff_post_validation(tmp: Path) -> Run:
    cfg, table = single_table(tmp)
    return Run(_go(cfg, {"t": table}, post_validation=True, now_iso=NOW_ISO))


def ff_explain_plan(tmp: Path) -> Run:
    cfg, table = single_table(tmp)
    return Run(_go(cfg, {"t": table}, explain_plan=True))


def ff_auto_chunk_oracle_lane(tmp: Path) -> Run:
    cfg, table = single_table(tmp)
    return Run(
        _go(
            cfg,
            {"t": table},
            auto_chunk_threshold_rows=10,
            chunk_size_rows=8,
            chunked_dispatcher_enabled=False,
            use_byte_estimate_routing=False,
            explain_plan=True,
        )
    )


def ff_auto_chunk_dispatcher_lane(tmp: Path) -> Run:
    cfg, table = single_table(tmp, hash_column=False)
    return Run(
        _go(
            cfg,
            {"t": table},
            auto_chunk_threshold_rows=10,
            chunk_size_rows=8,
            use_byte_estimate_routing=False,
            key_provider=key_provider(),
        )
    )


def ff_streamed_sink(tmp: Path) -> Run:
    cfg, table = single_table(tmp)
    return _sink_run(
        tmp,
        cfg,
        {"t": table},
        auto_chunk_threshold_rows=10,
        chunk_size_rows=8,
        use_byte_estimate_routing=False,
    )


def ff_streamed_sink_native_lane(tmp: Path) -> Run:
    cfg, table = single_table(tmp, hash_column=False)
    return _sink_run(
        tmp,
        cfg,
        {"t": table},
        auto_chunk_threshold_rows=10,
        chunk_size_rows=8,
        use_byte_estimate_routing=False,
        key_provider=key_provider(),
    )


def ff_resident_sink_untouched(tmp: Path) -> Run:
    cfg, table = single_table(tmp)
    return _sink_run(tmp, cfg, {"t": table}, unified_slice_enabled=False)


def seq_fk(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    return Run(_go(cfg, tables, out_of_core_threshold_rows=1_000, **_ROW_ROUTING))


def seq_fk_orphans_remap(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp, orphan_policy="remap", orphans=True)
    return Run(_go(cfg, tables, out_of_core_threshold_rows=1_000, **_ROW_ROUTING))


def seq_fk_orphans_warn(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp, orphan_policy="warn", orphans=True)
    return Run(_go(cfg, tables, out_of_core_threshold_rows=1_000, **_ROW_ROUTING))


def seq_fk_sink(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    return _sink_run(tmp, cfg, tables, out_of_core_threshold_rows=1_000, **_ROW_ROUTING)


def seq_fk_loader(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    return Run(
        _go(
            cfg,
            {},
            source_loader=lambda name: tables[name],
            out_of_core_threshold_rows=1_000,
            **_ROW_ROUTING,
        )
    )


def seq_fk_explain(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    return Run(
        _go(cfg, tables, explain_plan=True, out_of_core_threshold_rows=1_000, **_ROW_ROUTING)
    )


def seq_fk_transforms_declined(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp, parent_transforms=True)
    return Run(_go(cfg, tables, out_of_core_threshold_rows=1, **_ROW_ROUTING))


def ff_fk_transforms_declined_byte_routing(tmp: Path) -> Run:
    """Default byte-estimate routing sizes the small job as fitting, so it runs full_frame while
    the out-of-core decline is still stamped."""
    cfg, tables = fk_job(tmp, parent_transforms=True)
    return Run(_go(cfg, tables))


def ooc_fk_forced_loader(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    return Run(_go(cfg, {}, source_loader=lambda name: tables[name], execution_mode="out_of_core"))


def ooc_fk_forced(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    return Run(_go(cfg, tables, execution_mode="out_of_core"))


def ooc_fk_auto(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    return Run(_go(cfg, tables, out_of_core_threshold_rows=10, explain_plan=True, **_ROW_ROUTING))


def ooc_fk_forced_sink(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    return _sink_run(tmp, cfg, tables, execution_mode="out_of_core")


def ooc_fk_orphans_remap(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp, orphan_policy="remap", orphans=True)
    return Run(_go(cfg, tables, execution_mode="out_of_core"))


def ooc_fk_orphans_warn_sink(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp, orphan_policy="warn", orphans=True)
    return _sink_run(tmp, cfg, tables, execution_mode="out_of_core")


def ooc_fk_lazy_sources_sink(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    lazy = {n: LazySource(Path(spec["path"])) for n, spec in cfg["sources"].items()}
    return _sink_run(tmp, cfg, lazy, execution_mode="out_of_core")


def ooc_fk_budget_bytes(tmp: Path) -> Run:
    cfg, tables = fk_job(tmp)
    return Run(
        _go(
            cfg,
            tables,
            execution_mode="out_of_core",
            out_of_core_budget_bytes=512 * 1024 * 1024,
            out_of_core_reorder_threshold_rows=0,
        )
    )


SUCCESS_SCENARIOS: dict[str, Callable[[Path], Run]] = {
    f.__name__: f
    for f in (
        ff_legacy_single,
        ff_unified_admitted,
        ff_unified_fallback,
        ff_unified_flag_off,
        ff_multi_table,
        ff_multi_table_split,
        ff_quarantine_format_error,
        ff_generate_mask,
        ff_fidelity,
        ff_post_validation,
        ff_explain_plan,
        ff_auto_chunk_oracle_lane,
        ff_auto_chunk_dispatcher_lane,
        ff_streamed_sink,
        ff_streamed_sink_native_lane,
        ff_resident_sink_untouched,
        seq_fk,
        seq_fk_orphans_remap,
        seq_fk_orphans_warn,
        seq_fk_sink,
        seq_fk_loader,
        seq_fk_explain,
        seq_fk_transforms_declined,
        ff_fk_transforms_declined_byte_routing,
        ooc_fk_forced_loader,
        ooc_fk_forced,
        ooc_fk_auto,
        ooc_fk_forced_sink,
        ooc_fk_orphans_remap,
        ooc_fk_orphans_warn_sink,
        ooc_fk_lazy_sources_sink,
        ooc_fk_budget_bytes,
    )
}


def _relocate(doc: Any, tmp: Path) -> Any:
    """Replace the per-run temp directory so recorded paths compare across runs."""
    return json.loads(json.dumps(doc).replace(json.dumps(str(tmp))[1:-1], "<TMP>"))


def success_snapshot(name: str, tmp: Path) -> dict[str, Any]:
    with without_companion():
        return _relocate(snapshot(SUCCESS_SCENARIOS[name](tmp)), tmp)


def snapshot(run: Run) -> dict[str, Any]:
    """The comparison surface: the result projection plus whatever the sink received."""
    doc = project(run.result)
    if run.files:
        doc["files"] = {p.name: p.read_text() if p.exists() else None for p in run.files}
    if run.sink_dir is not None:
        doc["sink_tables"] = {
            p.stem: project_table(pq.read_table(p)) for p in sorted(run.sink_dir.glob("*.parquet"))
        }
    return doc


# ---------------------------------------------------------------------------
# Failure / publication scenarios
# ---------------------------------------------------------------------------


class TraceSink:
    """A `TransactionalSink` that records the order of every call and can fail on the Nth write."""

    def __init__(
        self, inner: ParquetTransactionalSink, *, fail_on_write: int | None = None
    ) -> None:
        self.inner = inner
        self.calls: list[str] = []
        self.fail_on_write = fail_on_write
        self._writes = 0

    def _maybe_fail(self, what: str) -> None:
        self._writes += 1
        if self.fail_on_write is not None and self._writes == self.fail_on_write:
            raise OSError(f"injected sink failure on {what}")

    def write(self, table: str, data: pa.Table) -> None:
        self.calls.append(f"write:{table}")
        self._maybe_fail(table)
        self.inner.write(table, data)

    def write_batches(self, table: str, batches: Any, *, schema: pa.Schema) -> None:
        self.calls.append(f"write_batches:{table}")
        self._maybe_fail(table)
        self.inner.write_batches(table, batches, schema=schema)

    def commit(self) -> None:
        self.calls.append("commit")
        self.inner.commit()

    def abort(self) -> None:
        self.calls.append("abort")
        self.inner.abort()

    def __getattr__(self, name: str) -> Any:
        if name == "spill_parent":
            return self.__dict__["inner"].spill_parent
        raise AttributeError(name)


def _exc(exc: BaseException | None) -> dict[str, Any] | None:
    if exc is None:
        return None
    return {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "code": getattr(exc, "code", None),
        "message": str(getattr(exc, "message", None) or exc),
    }


@contextlib.contextmanager
def _traced(tmp: Path, *, fail_on_write: int | None = None) -> Iterator[tuple[TraceSink, Path]]:
    out = tmp / "sink_out"
    yield TraceSink(ParquetTransactionalSink(out), fail_on_write=fail_on_write), out


def _attempt(call: Callable[[], Any]) -> tuple[Any, BaseException | None]:
    try:
        return call(), None
    except BaseException as exc:  # a trace records whatever a route raises
        return None, exc


def _trace(
    exc: BaseException | None,
    sink: TraceSink | None = None,
    out: Path | None = None,
    loader_calls: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "exception": _exc(exc),
        "sink_calls": list(sink.calls) if sink is not None else None,
        "sink_dir_exists": out.exists() if out is not None else None,
        "published_files": sorted(p.name for p in out.glob("*")) if out and out.exists() else [],
        "source_loader_calls": loader_calls,
    }


def f_reject_before_read(tmp: Path) -> dict[str, Any]:
    cfg, tables = generate_plus_mask(tmp)
    calls: list[str] = []

    def loader(name: str) -> pa.Table:
        calls.append(name)
        return tables[name]

    with _traced(tmp) as (sink, out):
        _, exc = _attempt(
            lambda: _go(cfg, {}, source_loader=loader, sink=sink, full_frame_reject_rows=10)
        )
        return _trace(exc, sink, out, calls)


def f_forced_ooc_incompatible(tmp: Path) -> dict[str, Any]:
    cfg, tables = generate_plus_mask(tmp)
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(lambda: _go(cfg, tables, execution_mode="out_of_core", sink=sink))
        return _trace(exc, sink, out)


def f_invalid_substrate_before_profile(tmp: Path) -> dict[str, Any]:
    _, exc = _attempt(lambda: _go({"tables": []}, {}, substrate="not-a-substrate"))
    return _trace(exc)


def f_knob_precedence(tmp: Path) -> dict[str, Any]:
    """Several invalid knobs at once: which one the entry reports pins validation order."""
    out: dict[str, Any] = {}
    cases: dict[str, dict[str, Any]] = {
        "substrate_then_chunk": {"substrate": "zzz", "chunk_size_rows": -1},
        "chunk_then_threshold": {"chunk_size_rows": -1, "auto_chunk_threshold_rows": 0},
        "auto_chunk_type": {"auto_chunk": "yes"},
        "ooc_threshold": {"out_of_core_threshold_rows": 0},
        "reject_rows": {"full_frame_reject_rows": 0},
        "budget": {"out_of_core_budget_bytes": 0},
        "byte_flag": {"use_byte_estimate_routing": 1},
        "probe_flag": {"use_probe_routing": 1},
        "unified_flag": {"unified_slice_enabled": 1},
        "post_validation_flag": {"post_validation": 1},
        "post_validation_sample": {"post_validation_sample_size": 0},
        "multi_table_flag": {"multi_table_dispatch_enabled": 1},
        "stream_flag": {"stream_chunked_output": 1},
        "reorder_threshold": {"out_of_core_reorder_threshold_rows": -5},
        "stream_then_substrate": {"stream_chunked_output": 1, "substrate": "zzz"},
        "native_threads": {"native_threads": 0},
    }
    # Every knob alone, then every adjacent pair with BOTH invalid and the later knob listed
    # first, so swapping the order of any two neighbouring checks changes the recorded error.
    ordered: list[tuple[str, Any]] = [
        ("substrate", "zzz"),
        ("fpe_chunk_count", 0),
        ("max_workers", 0),
        ("auto_chunk", 1),
        ("chunk_size_rows", 0),
        ("auto_chunk_threshold_rows", 0),
        ("native_threads", 0),
        ("chunked_dispatcher_enabled", 1),
        ("stream_chunked_output", 1),
        ("multi_table_dispatch_enabled", 1),
        ("out_of_core_threshold_rows", 0),
        ("full_frame_reject_rows", 0),
        ("out_of_core_budget_bytes", 0),
        ("use_byte_estimate_routing", 1),
        ("use_probe_routing", 1),
        ("unified_slice_enabled", 1),
        ("post_validation", 1),
        ("post_validation_enforce", 1),
        ("post_validation_sample_size", 0),
        ("out_of_core_reorder_threshold_rows", -5),
    ]
    for (earlier, ev), (later, lv) in itertools.pairwise(ordered):
        cases[f"pair:{earlier}+{later}"] = {later: lv, earlier: ev}
    for name, kw in cases.items():
        _, exc = _attempt(lambda kw=kw: _go({"tables": []}, {}, **kw))
        out[name] = _exc(exc)
    return out


def f_seq_sink_success(tmp: Path) -> dict[str, Any]:
    cfg, tables = fk_job(tmp)
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(
            lambda: _go(cfg, tables, sink=sink, out_of_core_threshold_rows=1_000, **_ROW_ROUTING)
        )
        return _trace(exc, sink, out)


def f_seq_sink_write_failure(tmp: Path) -> dict[str, Any]:
    cfg, tables = fk_job(tmp)
    with _traced(tmp, fail_on_write=2) as (sink, out):
        _, exc = _attempt(
            lambda: _go(cfg, tables, sink=sink, out_of_core_threshold_rows=1_000, **_ROW_ROUTING)
        )
        return _trace(exc, sink, out)


def f_seq_orphan_fail(tmp: Path) -> dict[str, Any]:
    cfg, tables = fk_job(tmp, orphan_policy="fail", orphans=True)
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(
            lambda: _go(cfg, tables, sink=sink, out_of_core_threshold_rows=1_000, **_ROW_ROUTING)
        )
        return _trace(exc, sink, out)


def f_ooc_sink_success(tmp: Path) -> dict[str, Any]:
    cfg, tables = fk_job(tmp)
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(lambda: _go(cfg, tables, sink=sink, execution_mode="out_of_core"))
        return _trace(exc, sink, out)


def f_ooc_sink_write_failure(tmp: Path) -> dict[str, Any]:
    cfg, tables = fk_job(tmp)
    with _traced(tmp, fail_on_write=2) as (sink, out):
        _, exc = _attempt(lambda: _go(cfg, tables, sink=sink, execution_mode="out_of_core"))
        return _trace(exc, sink, out)


def f_ooc_orphan_fail(tmp: Path) -> dict[str, Any]:
    cfg, tables = fk_job(tmp, orphan_policy="fail", orphans=True)
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(lambda: _go(cfg, tables, sink=sink, execution_mode="out_of_core"))
        return _trace(exc, sink, out)


def f_ff_streamed_success(tmp: Path) -> dict[str, Any]:
    cfg, table = single_table(tmp)
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(
            lambda: _go(
                cfg,
                {"t": table},
                sink=sink,
                auto_chunk_threshold_rows=10,
                chunk_size_rows=8,
                use_byte_estimate_routing=False,
            )
        )
        return _trace(exc, sink, out)


def f_ff_streamed_write_failure(tmp: Path) -> dict[str, Any]:
    cfg, table = single_table(tmp)
    with _traced(tmp, fail_on_write=1) as (sink, out):
        _, exc = _attempt(
            lambda: _go(
                cfg,
                {"t": table},
                sink=sink,
                auto_chunk_threshold_rows=10,
                chunk_size_rows=8,
                use_byte_estimate_routing=False,
            )
        )
        return _trace(exc, sink, out)


def f_ff_streamed_post_validation_failure(tmp: Path) -> dict[str, Any]:
    """A failure at the post-validation seam, AFTER the streamed output was written."""
    cfg, table = single_table(tmp)
    boom = mock.Mock(side_effect=RuntimeError("injected: post validation"))
    with (
        _traced(tmp) as (sink, out),
        mock.patch("decoy_engine.execution._pipeline_finalize.compute_post_validation", boom),
    ):
        _, exc = _attempt(
            lambda: _go(
                cfg,
                {"t": table},
                sink=sink,
                auto_chunk_threshold_rows=10,
                chunk_size_rows=8,
                use_byte_estimate_routing=False,
            )
        )
        return _trace(exc, sink, out)


def f_ff_post_validation_failure_resident(tmp: Path) -> dict[str, Any]:
    cfg, table = single_table(tmp)
    boom = mock.Mock(side_effect=RuntimeError("injected: post validation"))
    with (
        _traced(tmp) as (sink, out),
        mock.patch("decoy_engine.execution._pipeline_finalize.compute_post_validation", boom),
    ):
        _, exc = _attempt(
            lambda: _go(
                cfg, {"t": table}, sink=sink, post_validation=True, unified_slice_enabled=False
            )
        )
        return _trace(exc, sink, out)


def f_ff_resident_sink_untouched(tmp: Path) -> dict[str, Any]:
    cfg, table = single_table(tmp)
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(lambda: _go(cfg, {"t": table}, sink=sink, unified_slice_enabled=False))
        return _trace(exc, sink, out)


def _unified_fixture(tmp: Path) -> tuple[dict[str, Any], pa.Table]:
    return single_table(tmp, hash_column=False)


def f_unified_fallback_miss(tmp: Path) -> dict[str, Any]:
    """A plain exception inside the admitted lane reroutes to the oracle and the job completes."""
    cfg, table = _unified_fixture(tmp)
    boom = mock.Mock(side_effect=RuntimeError("injected: physical plan inputs"))
    with mock.patch(
        "decoy_engine.execution.physical._live_inputs.build_live_physical_plan_inputs", boom
    ):
        res, exc = _attempt(lambda: _go(cfg, {"t": table}, key_provider=key_provider()))
    doc = _trace(exc)
    doc["completed"] = res is not None
    doc["unified_slice_activation_stamped"] = (
        res is not None and "unified_slice_activation" in res.quality_metrics
    )
    return doc


def f_unified_invariant_failure(tmp: Path) -> dict[str, Any]:
    """A coded shadow difference on an admitted table is an invariant failure, not a reroute."""
    from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
    from decoy_engine.execution.physical._shadow_diff_codes import CELL_VALUE_DIFF, ShadowDifference

    cfg, table = _unified_fixture(tmp)

    def _raise(self: Any, *a: Any, **k: Any) -> Any:
        raise ShadowDifference(CELL_VALUE_DIFF, "injected")

    with mock.patch.object(ShadowCoordinator, "run", _raise):
        _, exc = _attempt(lambda: _go(cfg, {"t": table}, key_provider=key_provider()))
    return _trace(exc)


def f_unified_provider_failure(tmp: Path) -> dict[str, Any]:
    """Provider code raising while the pool builds surfaces the ORIGINAL exception."""
    from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator
    from decoy_engine.execution.physical._shadow_diff_codes import PoolBuildFailed

    cfg, table = _unified_fixture(tmp)
    original = ValueError("injected provider failure")

    def _raise(self: Any, *a: Any, **k: Any) -> Any:
        raise PoolBuildFailed(original)

    with mock.patch.object(ShadowCoordinator, "run", _raise):
        _, exc = _attempt(lambda: _go(cfg, {"t": table}, key_provider=key_provider()))
    doc = _trace(exc)
    doc["is_original"] = exc is original
    return doc


def f_row_error_fails_closed(tmp: Path) -> dict[str, Any]:
    """An unquarantined row error fails the job before anything is published."""
    cfg, table = bad_date_table(tmp, quarantine=None)
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(lambda: _go(cfg, {"t": table}, sink=sink, unified_slice_enabled=False))
        doc = _trace(exc, sink, out)
        doc["record_count"] = len(getattr(exc, "records", ()) or ())
        return doc


def f_row_error_fails_closed_streamed(tmp: Path) -> dict[str, Any]:
    cfg, table = bad_date_table(tmp, quarantine=None)
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(
            lambda: _go(
                cfg,
                {"t": table},
                sink=sink,
                auto_chunk_threshold_rows=10,
                chunk_size_rows=8,
                use_byte_estimate_routing=False,
            )
        )
        doc = _trace(exc, sink, out)
        doc["record_count"] = len(getattr(exc, "records", ()) or ())
        return doc


def f_lazy_not_materialized_on_reject(tmp: Path) -> dict[str, Any]:
    """A rejected FK job never opens or resolves its lazy sources."""
    cfg, _ = generate_plus_mask(tmp)
    lazy = {n: LazySource(Path(spec["path"])) for n, spec in cfg["sources"].items()}
    with _traced(tmp) as (sink, out):
        _, exc = _attempt(lambda: _go(cfg, lazy, sink=sink, full_frame_reject_rows=10))
        return _trace(exc, sink, out)


FAILURE_SCENARIOS: dict[str, Callable[[Path], dict[str, Any]]] = {
    f.__name__: f
    for f in (
        f_reject_before_read,
        f_forced_ooc_incompatible,
        f_invalid_substrate_before_profile,
        f_knob_precedence,
        f_seq_sink_success,
        f_seq_sink_write_failure,
        f_seq_orphan_fail,
        f_ooc_sink_success,
        f_ooc_sink_write_failure,
        f_ooc_orphan_fail,
        f_ff_streamed_success,
        f_ff_streamed_write_failure,
        f_ff_streamed_post_validation_failure,
        f_ff_post_validation_failure_resident,
        f_ff_resident_sink_untouched,
        f_unified_fallback_miss,
        f_unified_invariant_failure,
        f_unified_provider_failure,
        f_row_error_fails_closed,
        f_row_error_fails_closed_streamed,
        f_lazy_not_materialized_on_reject,
    )
}


def failure_snapshot(name: str, tmp: Path) -> Any:
    with without_companion():
        return _relocate(canon(FAILURE_SCENARIOS[name](tmp)), tmp)
