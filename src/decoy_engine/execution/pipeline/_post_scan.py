"""The post-scan gate: one check of a run's outputs before they become visible (plan 4.5).

Declared keys are measured over Arrow batches by `check_keys`. The column suite runs through
`run_post_mask` only when `post_scan.integrity` asks for it, so a run that only declares
relationships is never scanned for findings it did not opt in to. `post_scan_verdict` decides and a
block raises `PostScanBlockedError` after every `except` block has closed, so its chain is empty.
A coded engine error from a part propagates unchanged; any other keeps only its class name,
because a reader or DuckDB message can quote key values.

`execute_route` binds one gate per run (`bind_gate`); a route that holds its outputs resident
runs it before it commits, and `observed_commit` refuses a commit while a required gate has not
run. `settle` runs it over the returned outputs when no route did, and stamps its report on the
result.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine import fileaccess, reasons
from decoy_engine.config import (
    RelationshipConfig,
    parse_relationship_config,
    post_scan_gate_applies,
    post_scan_integrity_requested,
    post_scan_output_readable,
    post_scan_sink_kind,
)
from decoy_engine.execution._chunked_input import SourceFacts, capture_source_facts, check_opened
from decoy_engine.execution._pipeline_sources import materialize_source
from decoy_engine.execution._transforms_gate import transform_bearing_mask_tables
from decoy_engine.execution.out_of_core import resolve_ooc_memory_limit
from decoy_engine.execution.out_of_core._scratch import (
    close_private,
    open_private,
    scratch_access_for,
)
from decoy_engine.internal.memory_limit import require_megabytes
from decoy_engine.profile import LazySource
from decoy_engine.profiling import MEMORY_LIMIT, passes_through
from decoy_engine.progress import safe_error_class
from decoy_engine.reasons import reason_ref_payload
from decoy_engine.verification.postmask import (
    SCHEMA_VERSION,
    KeyResult,
    PostScanVerdict,
    blocked_error,
    check_keys,
    from_lazy,
    from_staged,
    from_table,
    keys_report,
    post_scan_verdict,
    run_post_mask,
    with_cast,
)

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._pipeline_context import PipelineRunContext
    from decoy_engine.verification.postmask import TableBatches

__all__ = [
    "PostScanGate",
    "active_gate",
    "attach_cast_run",
    "bind_gate",
    "note_quarantined",
    "settle",
    "tee_loader",
]

# `pass_failed_with` when the commit is reached and a required gate never ran.
_NOT_RUN: Final = "PostScanGateNotRun"


# A smaller cap starves DuckDB's hash join even at 100k keys (it fails to pin a block), so a small
# run budget never takes the key scan below this.
_FLOOR_MB: Final = 128


class PostScanGate:
    """One run's gate. `ran` is set once a check completes; every later call is a no-op. After a
    run, `report` is the value-free report and `verdict` the decision on it."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        budget_bytes: int | None = None,
        source_facts: Mapping[str, SourceFacts] | None = None,
        deferred: bool = False,
        sources: Mapping[str, pa.Table | LazySource] | None = None,
    ) -> None:
        self.required = post_scan_gate_applies(config)
        # True on a route whose inputs or output the gate cannot read back yet: the run is then
        # checked report-only, as before the gate, and says so on its result.
        self.deferred = deferred and self.required
        self.ran = False
        self.report: dict[str, Any] | None = None
        self.verdict: PostScanVerdict | None = None
        self._config = config
        self._budget_bytes = budget_bytes
        # Key columns of the parent rows quarantine removed. Kept only in memory, so a child
        # that pointed at one is told apart from an orphan the run introduced; never reported.
        self._quarantined: dict[tuple[str, tuple[str, ...]], list[pa.Table]] = {}
        # Footer facts of each lazy input, taken before any route read it: a re-read refuses a
        # file that changed in between.
        self._source_facts = source_facts or {}
        # The run's inputs, as the caller gave them: a commit with no resident output reads
        # them back to compare. Declared casting is applied to them on that re-read.
        self._sources = sources or {}
        self._cast_run: Any = None
        # Writes that carry row values (the quarantine file), held until the verdict passes.
        self._held: list[Callable[[], None]] = []
        # Baselines of tables only the route reads: key and cast columns of each table the
        # route loaded, copied once into scratch Parquet as the route read them.
        self._teed: dict[str, LazySource] = {}
        self._scratch = ExitStack()
        self._scratch_root: Path | None = None

    @property
    def memory_limit(self) -> str:
        """The key scan's DuckDB cap: its own fixed cap or the run's budget share, whichever is
        less, never below the working floor. The scan spills to its scratch directory past it."""
        budget = resolve_ooc_memory_limit(self._budget_bytes).memory_limit
        capped = min(MEMORY_LIMIT, budget, key=require_megabytes)
        return f"{max(_FLOOR_MB, require_megabytes(capped))}MB"

    def run_resident(
        self,
        outputs: Mapping[str, pa.Table],
        sources: Mapping[str, pa.Table | LazySource],
        table_kinds: Mapping[str, str],
        *,
        raw_sources: bool = False,
    ) -> None:
        """Check resident outputs against their sources (resident or lazy, re-read in batches);
        raise if the verdict blocks. `raw_sources` says the sources are the caller's own, before
        any declared cast, so the re-read casts them as the route did."""
        if self.ran or not self.required:
            return
        if self.deferred:
            self._report_only(outputs, sources, table_kinds)
            return
        if not _resident(outputs, table_kinds):
            return  # the route wrote to a sink as it went; the commit reads the staged files
        suite = None
        if post_scan_integrity_requested(self._config):

            def suite(keys: tuple[KeyResult, ...]) -> dict[str, Any]:
                return self._column_suite(outputs, sources, table_kinds, keys)

        views = {name: from_table(name, table) for name, table in outputs.items()}
        self._decide(views, sources, raw_sources, suite)

    def _decide(
        self,
        outputs: Mapping[str, TableBatches],
        sources: Mapping[str, pa.Table | LazySource],
        raw_sources: bool,
        suite: Callable[[tuple[KeyResult, ...]], dict[str, Any]] | None,
    ) -> None:
        failed: str | None = None
        keys: tuple[KeyResult, ...] = ()
        try:
            keys = self._keys(outputs, sources, raw_sources)
        except Exception as exc:
            if passes_through(exc):
                raise
            failed = safe_error_class(type(exc).__name__)
        report: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        if suite is not None:
            try:
                report = suite(keys)
            except Exception as exc:
                if passes_through(exc):
                    raise
                failed = failed or safe_error_class(type(exc).__name__)
        report["keys"] = keys_report(keys)
        if failed:
            report["pass_failed_with"] = failed
        verdict = post_scan_verdict(report)
        report["verdict"] = verdict.as_dict()
        self.report, self.verdict, self.ran = report, verdict, True
        error = blocked_error(report, verdict)
        if error is not None:
            raise error

    def tee_loader(self, loader: Callable[[str], pa.Table]) -> Callable[[str], pa.Table]:
        """`loader` that also copies the key and cast columns of each declared key table, as the
        route reads it, into scratch. The route's own read is the baseline: the loader is never
        called a second time, and a table transformed on load is baselined after its transforms."""
        if not self.required or self.deferred:
            return loader
        try:
            wanted = _key_columns(self._relationships())
        except Exception as exc:
            if passes_through(exc):
                raise
            return loader  # an unreadable relationship is the check's to report

        def teed(name: str) -> pa.Table:
            table = loader(name)
            if name in wanted and name not in self._teed:
                self._tee(name, table, wanted[name])
            return table

        return teed

    def _tee(self, name: str, table: pa.Table, columns: tuple[str, ...]) -> None:
        cast = self._cast_run
        if cast is not None and name in cast.tables():
            columns = (*columns, *cast.declared_columns(name))
        kept = table.select([c for c in dict.fromkeys(columns) if c in table.column_names])
        if self._scratch_root is None:
            self._scratch_root = self._scratch.enter_context(
                fileaccess.protected_scratch_dir(prefix="decoy-post-scan-tee-")
            )
        path = self._scratch_root / f"t{len(self._teed):04d}.parquet"
        sink = open_private(path)
        writer = None
        try:
            writer = pq.ParquetWriter(sink, kept.schema)
            writer.write_table(kept)
        finally:
            close_private(writer, sink)
        self._teed[name] = LazySource(
            path, access=scratch_access_for(self._scratch_root), field_key="engine.scratch"
        )

    def close(self) -> None:
        """Remove the tee's scratch files."""
        self._scratch.close()

    def attach_cast_run(self, cast_run: Any) -> None:
        """The run's declared casting, so a re-read of the raw inputs types them as the route did."""
        self._cast_run = cast_run

    def _report_only(
        self,
        outputs: Mapping[str, pa.Table],
        sources: Mapping[str, pa.Table | LazySource],
        table_kinds: Mapping[str, str],
    ) -> None:
        """The check a deferred route gets: the column suite over the mask tables the run still
        holds, reported and never blocking. Declared keys alone get no check yet."""
        self.ran = True
        if not post_scan_integrity_requested(self._config):
            return
        masked = [n for n in outputs if table_kinds.get(n, "mask") == "mask" and n in sources]
        if masked:
            self.report = run_post_mask(
                {n: materialize_source(sources[n]).to_pandas() for n in masked},
                {n: outputs[n].to_pandas() for n in masked},
                config=dict(self._config),
            )

    def note_quarantined(self, table: str, rows: pa.Table, removed: Collection[int]) -> None:
        """Record the declared parent keys of `rows` (a table before quarantine filtered it) at
        the `removed` row indices. A relationship that cannot be read is left for the check to
        report."""
        if not self.required or self.deferred or not removed:
            return
        try:
            relationships = self._relationships()
        except Exception as exc:
            if passes_through(exc):
                raise
            return
        for rel in relationships:
            columns = tuple(rel.parent.columns)
            if rel.parent.table == table and set(columns) <= set(rows.column_names):
                taken = rows.select(list(columns)).take(sorted(removed))
                self._quarantined.setdefault((table, columns), []).append(taken)

    def _relationships(self) -> list[RelationshipConfig]:
        return [
            parse_relationship_config(raw, prefix=f"relationship {n} is invalid")
            for n, raw in enumerate(self._config.get("relationships") or ())
        ]

    def before_commit(self, sink: Any) -> None:
        """A required gate that has not run when a sink commits reads the sink's staged files
        back and judges them before anything is published. A sink it cannot read, or a run whose
        column suite needs the whole output, is refused: fail closed. With no sink (the
        post-condition after the route) a gate that never ran is a wiring fault and refuses."""
        if self.ran or not self.required or self.deferred:
            return
        openers = _staged_openers(sink)
        if openers is not None and not post_scan_integrity_requested(self._config):
            staged = {name: from_staged(name, opener) for name, opener in openers.items()}
            self._decide(staged, self._sources, True, None)
            return
        report = {"schema_version": SCHEMA_VERSION, "pass_failed_with": _NOT_RUN}
        error = blocked_error(report, post_scan_verdict(report))
        if error is not None:
            raise error

    def _source(self, name: str, source: pa.Table | LazySource, raw: bool) -> TableBatches:
        if not isinstance(source, LazySource):
            base = from_table(name, source)
        else:
            # A teed file is the engine's own copy of the route's read, not the caller's input.
            expected = None if self._teed.get(name) is source else self._source_facts.get(name)
            if expected is None:
                base = from_lazy(name, source)
            else:
                base = from_lazy(
                    name, source, on_open=lambda opened: check_opened(opened, expected, table=name)
                )
        cast = self._cast_run
        if not raw or cast is None or name not in cast.tables():
            return base
        typed = cast.cast_batch(name, base.schema.empty_table(), record=False).table.schema
        # `record=False`: the route already counted this pass; the re-read must not count again.
        return with_cast(
            base,
            needs=cast.declared_columns(name),
            schema=typed,
            cast=lambda table, offset: (
                cast.cast_batch(name, table, offset=offset, record=False).table
            ),
        )

    def _keys(
        self,
        outputs: Mapping[str, TableBatches],
        sources: Mapping[str, pa.Table | LazySource],
        raw_sources: bool,
    ) -> tuple[KeyResult, ...]:
        rels = self._relationships()
        if not rels:
            return ()
        # The gate's connections go through the run's own recorder and thread cap, which
        # `execute_route` binds around the route and this check alike.
        with fileaccess.protected_scratch_dir(prefix="decoy-post-scan-gate-") as scratch:
            return check_keys(
                rels,
                {
                    name: self._source(name, src, raw_sources)
                    for name, src in {**sources, **self._teed}.items()
                },
                dict(outputs),
                scratch=scratch,
                memory_limit=self.memory_limit,
                quarantined=self._quarantined,
            )

    def _column_suite(
        self,
        outputs: Mapping[str, pa.Table],
        sources: Mapping[str, pa.Table | LazySource],
        table_kinds: Mapping[str, str],
        keys: tuple[KeyResult, ...],
    ) -> dict[str, Any]:
        # Generated tables have no source to compare against, as in the report-only suite.
        masked = [n for n in outputs if table_kinds.get(n, "mask") == "mask" and n in sources]
        return run_post_mask(
            {n: materialize_source(sources[n]).to_pandas() for n in masked},
            {n: outputs[n].to_pandas() for n in masked},
            config=dict(self._config),
            keys=keys,
        )


_ACTIVE: ContextVar[PostScanGate | None] = ContextVar("decoy_post_scan_gate", default=None)


def active_gate() -> PostScanGate | None:
    """The gate bound to this run, or `None` outside `bind_gate`."""
    return _ACTIVE.get()


def _resident(outputs: Mapping[str, pa.Table], table_kinds: Mapping[str, str]) -> bool:
    """Whether every table the run produces is still held in `outputs`."""
    return all(name in outputs for name in table_kinds)


def _staged_openers(sink: Any) -> Mapping[str, Callable[[], int]] | None:
    """The staged files of a sink the gate may read back, or `None` for no sink or a sink class
    it does not trust (a foreign producer's files are never read)."""
    if sink is None or not post_scan_output_readable(post_scan_sink_kind(sink)):
        return None
    staged = getattr(sink, "staged_tables", None)
    return staged() if callable(staged) else None


def _key_columns(rels: list[RelationshipConfig]) -> dict[str, tuple[str, ...]]:
    """Each table's declared key columns, over every end of every relationship."""
    out: dict[str, list[str]] = {}
    for rel in rels:
        for end in (rel.parent, *rel.children):
            out.setdefault(end.table, []).extend(end.columns)
    return {table: tuple(dict.fromkeys(cols)) for table, cols in out.items()}


def _deferred(ctx: PipelineRunContext, route: str | None = None) -> bool:
    """Whether this run's inputs cannot be read back by the gate before the route runs: a loader
    supplies tables nothing can re-read, and a lazy table that declares transforms is
    transformed inside the route. The sequential route loads each such table once through a
    loader the gate wraps (`tee_loader`), so it is the one route that is not deferred.
    Outputs that are not resident are read at commit."""
    lazy_transformed = any(
        isinstance(ctx.caller_sources.get(name), LazySource)
        for name in transform_bearing_mask_tables(ctx.config)
    )
    return (ctx.source_loader is not None or lazy_transformed) and route != "sequential"


@contextmanager
def bind_gate(ctx: PipelineRunContext, route: str | None = None) -> Iterator[PostScanGate]:
    """Bind one gate for the run. Its lazy-input snapshot is taken here, before any route reads."""
    deferred = _deferred(ctx, route)
    facts = (
        capture_source_facts(ctx.caller_sources, ctx.table_kinds)
        if post_scan_gate_applies(ctx.config) and not deferred
        else {}
    )
    gate = PostScanGate(
        ctx.config,
        budget_bytes=ctx.out_of_core_budget_bytes,
        source_facts=facts,
        deferred=deferred,
        sources=ctx.caller_sources,
    )
    token = _ACTIVE.set(gate)
    try:
        yield gate
    finally:
        _ACTIVE.reset(token)
        gate.close()


def tee_loader(loader: Callable[[str], pa.Table]) -> Callable[[str], pa.Table]:
    """The route's loader, wrapped so the run's gate takes its baseline from the route's own read.
    Outside `bind_gate` there is no gate and the loader is returned as it is."""
    gate = active_gate()
    return loader if gate is None else gate.tee_loader(loader)


def attach_cast_run(cast_run: Any) -> None:
    """Tell the run's gate about the declared casting a route opened. Outside `bind_gate`, or
    with no cast run, there is nothing to attach."""
    if cast_run is not None and (gate := active_gate()) is not None:
        gate.attach_cast_run(cast_run)


def note_quarantined(
    tables: Mapping[str, pa.Table], removed: Mapping[str, Collection[int]]
) -> None:
    """Tell the run's gate which rows quarantine is about to remove from `tables` (still
    unfiltered). Outside `bind_gate` there is no gate and nothing to tell."""
    if (gate := active_gate()) is None:
        return
    for name, rows in removed.items():
        if name in tables:
            gate.note_quarantined(name, tables[name], rows)


def publish_after_gate(write: Callable[[], None]) -> None:
    """Run `write`, a publish that carries row values, once nothing can block the run: now when
    no gate will judge it, else in `settle` after the verdict passes. A blocked run drops it."""
    gate = active_gate()
    if gate is None or not gate.required or gate.ran or gate.deferred:
        write()
    else:
        gate._held.append(write)


def settle(gate: PostScanGate, ctx: PipelineRunContext, result: ExecutionResult) -> None:
    """After a route returns: run the gate over the outputs when no route did, refuse a required
    gate that still has not run, publish what waited on the verdict, and stamp the report."""
    if not gate.ran:
        gate.run_resident(result.outputs, ctx.caller_sources, ctx.table_kinds, raw_sources=True)
    if gate.required and not gate.ran:
        gate.before_commit(None)
    held, gate._held = gate._held, []
    for write in held:
        write()
    if gate.report is not None:
        result.quality_metrics["post_scan_integrity"] = gate.report
    if gate.deferred:
        result.quality_metrics["post_scan_gate_deferred"] = reason_ref_payload(
            reasons.post_scan.GATE_DEFERRED_ROUTE.spec.ref
        )
