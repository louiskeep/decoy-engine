"""Worker entrypoint for an isolated `run_pipeline` invocation.

Pattern: ported from `scripts/fk_memory_probe.py`'s worker convention (see
`_isolated_common` module docstring for the full citation) -- a fresh
`python -m` child, JSON payload read from a temp file (never argv, per the
driver's docstring), a hard `resource.setrlimit` applied BEFORE any workload
allocation, and a JSON result envelope written to a known file in work_root
(dennis review HIGH-1; see `_isolated_common.RESULT_FILENAME`'s docstring for
why NOT stdout's last line, the probe's own `--json` convention this used to
copy verbatim).

Invoked as `[sys.executable, "-m", "decoy_engine.execution._isolated_worker",
<payload_path>]` by `_isolated_run.run_pipeline_isolated`; not a public
entrypoint (no `if __main__` CLI ergonomics beyond that one positional arg).

Heavy imports happen at module load time, BEFORE `main()` applies the memory
cap -- matching fk_memory_probe's own ordering (imports are the interpreter's
baseline footprint, not "workload allocation"; capping before them would make
every capped run fail on import instead of on the job it is meant to bound).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._isolated_common import (
    RESULT_FILENAME,
    apply_mem_cap,
    exception_error_text,
    is_memory_failure,
    peak_rss_mb,
)
from decoy_engine.execution._pipeline import run_pipeline
from decoy_engine.execution._transactional_sink import StagedParquetWriter
from decoy_engine.fileaccess import FileRequest, FileRole, OutputTxn, write_private
from decoy_engine.fileaccess._local import _LocalStageDir
from decoy_engine.profile._readers import LazySource
from decoy_engine.reason_adapter import reason_from_exception


def _error_text(exc: BaseException) -> str:
    """Stored error text: the safe summary (no exception message), capped."""
    return exception_error_text(exc)[:500]


def _reason_payload(exc: BaseException) -> dict[str, Any]:
    """The failure's structured reason: the record `run_pipeline` captured at its boundary."""
    return reason_from_exception(exc).to_payload()


def _write_envelope_file(envelope: dict[str, Any], result_path: Path) -> None:
    """Write the result envelope to `result_path` -- NOT stdout (HIGH-1).

    Any other diagnostic output the worker or a dependency writes (atexit
    handlers, BLAS/OpenMP teardown chatter, a chatty provider, a stray user
    print) is free to land on stdout without risk: the driver reads this
    file, never stdout's last line. `default=str` is the safety net for
    `quality_metrics`: it is already meant to be manifest-safe (the platform
    stamps it straight into a job's JSON manifest today), but one unexpected
    non-serializable value in that free-form dict must not turn a genuinely
    completed run into a `crashed` one just because the envelope itself
    failed to serialize.
    """
    # Owner-only at creation: under a narrowing umask a plain write is unreadable to the parent.
    data = json.dumps(envelope, default=str).encode("utf-8")
    write_private(FileRequest(FileRole.CONFIG, "write", str(result_path), "isolated.result"), data)


def _load_sources(
    manifest: dict[str, str], *, lazy: bool
) -> dict[str, LazySource] | dict[str, pa.Table]:
    """Load manifest sources, lazily or eagerly.

    Pre-TB-1 this always called `pq.read_table` here, materializing every
    input table in the child BEFORE `run_pipeline` ever decides a route --
    the root cause of the governed out-of-core path's measured ~2x memory
    overhead (docs/plans/2026-07-12-track-b-completion-program.md's "two
    defects" section): even a job that goes on to stream through
    `run_fk_out_of_core` paid for a fully resident copy of its own input
    first.

    `_run` now passes `lazy=True` for every job. A non-relationship job used to stay
    eager because the auto-chunk classifier refused a `LazySource`, so a lazy 200k-row
    single-table job ran full-frame and crashed under a low rlimit. B6b admits a
    `LazySource` to auto-chunk from its Parquet footer facts: it streams when the output
    streams and is materialized once otherwise, so that regression cannot recur
    (`_chunked_input`). `lazy=False` keeps the eager load for a direct caller.

    `LazySource` (`decoy_engine.profile._readers`) is the SAME lazy-Parquet
    handle the direct/profiling path already uses (`scripts/
    fk_memory_probe.py`'s `_run_out_of_core`, `tests/perf_fixtures/
    fk_relational.py`'s `lazy_sources`) -- constructing one here is just
    wrapping a path string, no I/O, so this is cheap regardless of which
    relationship-route the job ends up taking. `run_pipeline` (`_pipeline.
    py`) resolves each `LazySource` at the point of use: the out-of-core
    route consumes it directly (`run_fk_out_of_core` already accepts
    `pa.Table | LazySource`), while sequential/full_frame -- routes that
    legitimately need whole-table residency -- call `.to_table()` only
    when they actually reach that point, never up front.
    """
    if lazy:
        return {name: LazySource(Path(path)) for name, path in manifest.items()}
    return {
        name: pq.read_table(path)  # type: ignore[no-untyped-call, unused-ignore]
        for name, path in manifest.items()
    }


class _StagingSink:
    """The child's sink: stages each table into the parent's stage and publishes nothing.

    The write half is `StagedParquetWriter` (owner-only files, replace on rewrite, stored-size
    evidence); there is no lock, descriptor or rename here. `commit` and `abort` do nothing:
    the parent admits, measures, describes and binds the stage, or discards it (plan 5.7)."""

    bounded_write_batches = True

    def __init__(self, staging_output_dir: str) -> None:
        self._dir = Path(staging_output_dir)
        os.makedirs(self._dir, exist_ok=True)
        # The parent made this stage (or names one in its own scratch); the child only adds
        # files to it, so it holds the stage by path rather than through an output capability.
        stage = _LocalStageDir(self._dir, "isolated.staging", private=True)
        # The writer only creates, opens and removes stage files: the `StageDir` half.
        self._writer = StagedParquetWriter(lambda: cast(OutputTxn, stage))
        self.publication_evidence = self._writer.publication_evidence

    @property
    def spill_parent(self) -> Path:
        """A streamed run's hold-back spills beside the stage, on the same filesystem."""
        return self._dir.parent

    def committed_table_path(self, table: str) -> Path:
        return self._dir / f"{table}.parquet"

    def write(self, table: str, data: pa.Table) -> None:
        self._writer.write(table, data)

    def write_batches(
        self, table: str, batches: Iterable[pa.RecordBatch], *, schema: pa.Schema
    ) -> None:
        self._writer.write_batches(table, batches, schema=schema)

    def staged_tables(self) -> list[str]:
        return [component.removesuffix(".parquet") for component in self._writer.staged]

    def commit(self) -> None:
        return None

    def abort(self) -> None:
        return None


def _finalize_outputs(result: ExecutionResult, sink: _StagingSink) -> list[str]:
    """Stage `result.outputs` for the parent's publication (TB-1 fix #2); return what is staged.

    Two shapes, distinguished by `execution_telemetry`'s `outputs_streamed` stamp
    (`_pipeline_route_exec.py`):

    - STREAMED (sequential/out_of_core, the auto-chunk lane and a fully dispatched multi-table
      split; since B6b those lanes also read their input lazily, as batches): `result.outputs`
      is `{}` by construction and the sink already staged bounded batches, so this worker never
      holds a full output table resident.
    - RESIDENT (full_frame; the sink is passed but never touched on that route): the ordinary
      in-memory `result.outputs`, staged here through the same sink.

    Peak-bias caveat for B5 (LOW-4): staging allocates Parquet write buffers, so a peak sampled
    after this call includes the staging write's own footprint.
    """
    execution = result.quality_metrics.get("execution") or {}
    if result.outputs or not execution.get("outputs_streamed"):
        for table, data in result.outputs.items():
            sink.write(table, data)
    return sink.staged_tables()


def _row_errors(row_errors: tuple[Any, ...]) -> list[dict[str, Any]]:
    """Row errors as plain objects for the envelope (MED-4): the user-facing quarantine surface
    crosses the process boundary with the result, never as a file in the parent's stage."""
    return [
        {
            "table": r.table,
            "column": r.column,
            "row_index": r.row_index,
            "trigger": r.trigger,
            "reason": r.reason,
        }
        for r in row_errors
    ]


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    mem_cap_bytes = payload.get("mem_cap_bytes")
    rlimit_kind = payload.get("rlimit_kind", "data")
    if mem_cap_bytes is not None:
        # Must run before ANY workload allocation -- source loading and
        # run_pipeline both count. Imports above already happened at module
        # load, which is the intended split (see module docstring).
        apply_mem_cap(mem_cap_bytes, rlimit_kind)

    staging_output_dir = payload["staging_output_dir"]
    try:
        # Every job loads lazily (B6b); see `_load_sources`'s docstring.
        sources = _load_sources(payload.get("sources") or {}, lazy=True)
        # TB-1 fix #2: always hand run_pipeline a sink on the parent's stage. A route that
        # streams writes bounded batches straight through it; full_frame never touches it
        # (see `_finalize_outputs`). The worker does not know the route in advance.
        sink = _StagingSink(staging_output_dir)
        result = run_pipeline(payload["config"], sources, sink=sink, **payload["kwargs"])
        # Staging allocates too, so a memory failure here must self-report as well.
        staged_tables = _finalize_outputs(result, sink)
        row_errors = _row_errors(result.row_errors)
    except BaseException as exc:
        outcome = "oom_killed" if is_memory_failure(exc) else "crashed"
        return {
            "outcome": outcome,
            "peak_rss_mb": round(peak_rss_mb(), 1),
            "error": _error_text(exc),
            "reason": _reason_payload(exc),
        }

    return {
        "outcome": "completed",
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "staged_tables": staged_tables,
        "row_errors": row_errors,
        "quality_metrics": dict(result.quality_metrics),
        "table_kinds": dict(result.table_kinds),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write(
            "usage: python -m decoy_engine.execution._isolated_worker "
            f"<payload_path>, got {argv!r}\n"
        )
        return 2

    # payload_path's own parent IS work_root by construction (the driver
    # writes payload.json at work_root's top level) -- result_path is
    # derived from it rather than passed separately, so worker and driver
    # can never disagree about where the envelope lands.
    payload_path = Path(argv[1])
    result_path = payload_path.parent / RESULT_FILENAME
    try:
        with open(payload_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        envelope = _run(payload)
    except BaseException as exc:  # payload unreadable, or a failure outside `_run`'s try
        envelope = {
            "outcome": "oom_killed" if is_memory_failure(exc) else "crashed",
            "peak_rss_mb": round(peak_rss_mb(), 1),
            "error": _error_text(exc),
            "reason": _reason_payload(exc),
        }
    _write_envelope_file(envelope, result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
