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

from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Final

import pyarrow as pa

from decoy_engine import fileaccess, reasons
from decoy_engine._lexeme import lexeme_columns_by_table
from decoy_engine.config import (
    RelationshipConfig,
    parse_relationship_config,
    post_scan_gate_applies,
    post_scan_integrity_requested,
)
from decoy_engine.execution._chunked_input import SourceFacts, capture_source_facts, check_opened
from decoy_engine.execution._pipeline_sources import materialize_source
from decoy_engine.execution.out_of_core import resolve_ooc_memory_limit
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
    from_table,
    keys_report,
    post_scan_verdict,
    run_post_mask,
)

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.execution._pipeline_context import PipelineRunContext
    from decoy_engine.verification.postmask import TableBatches

__all__ = ["PostScanGate", "active_gate", "bind_gate", "note_quarantined", "settle"]

# `pass_failed_with` when the commit is reached and a required gate never ran.
_NOT_RUN: Final = "PostScanGateNotRun"


# A smaller cap starves DuckDB's hash join even at 100k keys (it fails to pin a block), so a small
# run budget never takes the key scan below this.
_FLOOR_MB: Final = 128


def _megabytes(limit: str) -> int:
    return int(limit.removesuffix("MB"))


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

    @property
    def memory_limit(self) -> str:
        """The key scan's DuckDB cap: its own fixed cap or the run's budget share, whichever is
        less, never below the working floor. The scan spills to its scratch directory past it."""
        budget = resolve_ooc_memory_limit(self._budget_bytes).memory_limit
        capped = min(MEMORY_LIMIT, budget, key=_megabytes)
        return f"{max(_FLOOR_MB, _megabytes(capped))}MB"

    def run_resident(
        self,
        outputs: Mapping[str, pa.Table],
        sources: Mapping[str, pa.Table | LazySource],
        table_kinds: Mapping[str, str],
    ) -> None:
        """Check resident outputs against their sources (resident or lazy, re-read in batches);
        raise if the verdict blocks."""
        if self.ran or not self.required:
            return
        if self.deferred:
            self._report_only(outputs, sources, table_kinds)
            return
        failed: str | None = None
        keys: tuple[KeyResult, ...] = ()
        try:
            keys = self._keys(outputs, sources)
        except Exception as exc:
            if passes_through(exc):
                raise
            failed = safe_error_class(type(exc).__name__)
        report: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
        if post_scan_integrity_requested(self._config):
            try:
                report = self._column_suite(outputs, sources, table_kinds, keys)
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
        """Refuse the commit when a required gate never ran. No sink's staged files can be checked
        yet, so every sink is refused; `ran` stays false, so a retry is refused too."""
        if self.ran or not self.required or self.deferred:
            return
        report = {"schema_version": SCHEMA_VERSION, "pass_failed_with": _NOT_RUN}
        error = blocked_error(report, post_scan_verdict(report))
        if error is not None:
            raise error

    def _source(self, name: str, source: pa.Table | LazySource) -> TableBatches:
        if not isinstance(source, LazySource):
            return from_table(name, source)
        expected = self._source_facts.get(name)
        if expected is None:
            return from_lazy(name, source)
        return from_lazy(
            name, source, on_open=lambda opened: check_opened(opened, expected, table=name)
        )

    def _keys(
        self, outputs: Mapping[str, pa.Table], sources: Mapping[str, pa.Table | LazySource]
    ) -> tuple[KeyResult, ...]:
        rels = self._relationships()
        if not rels:
            return ()
        # The gate's connections go through the run's own recorder and thread cap, which
        # `execute_route` binds around the route and this check alike.
        with fileaccess.protected_scratch_dir(prefix="decoy-post-scan-gate-") as scratch:
            return check_keys(
                rels,
                {name: self._source(name, source) for name, source in sources.items()},
                {name: from_table(name, table) for name, table in outputs.items()},
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


def _deferred(ctx: PipelineRunContext) -> bool:
    """Whether the gate cannot read this run back yet: a sink publishes staged output, a loader
    or a lazy source feeds tables the gate does not hold, and declared casting renders them per
    chunk. Each of these routes runs as it did before the gate until it is made readable."""
    return (
        ctx.sink is not None
        or ctx.source_loader is not None
        or any(isinstance(s, LazySource) for s in ctx.caller_sources.values())
        or bool(lexeme_columns_by_table(ctx.config))
    )


@contextmanager
def bind_gate(ctx: PipelineRunContext) -> Iterator[PostScanGate]:
    """Bind one gate for the run. Its lazy-input snapshot is taken here, before any route reads."""
    deferred = _deferred(ctx)
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
    )
    token = _ACTIVE.set(gate)
    try:
        yield gate
    finally:
        _ACTIVE.reset(token)


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


def settle(gate: PostScanGate, ctx: PipelineRunContext, result: ExecutionResult) -> None:
    """After a route returns: run the gate over the outputs when no route did, refuse a required
    gate that still has not run, and stamp the report on the result."""
    if not gate.ran:
        gate.run_resident(result.outputs, ctx.caller_sources, ctx.table_kinds)
    if gate.required and not gate.ran:
        gate.before_commit(None)
    if gate.report is not None:
        result.quality_metrics["post_scan_integrity"] = gate.report
    if gate.deferred:
        result.quality_metrics["post_scan_gate_deferred"] = reason_ref_payload(
            reasons.post_scan.GATE_DEFERRED_ROUTE.spec.ref
        )
