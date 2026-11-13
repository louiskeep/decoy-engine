"""The subset stage of a run: select the rows once, stage the survivors, then run as usual.

The stage sits between the source declaration check and profiling, so every name a subset
message can show is a declared name. Lazy sources are re-read once and written through the
one staging writer (`declared_casting.stage_typed_sources`) as scratch Parquet, so a source of
10M rows with a 1% subset never becomes a 10M-row table. The scope owns the scratch root and
removes it when the run ends, whatever the exit.
"""

from __future__ import annotations

from bisect import bisect_left
from contextlib import ExitStack
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.config._subset_run import refuse_subset_source_loader
from decoy_engine.declared_casting import stage_typed_sources
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._chunked_input import check_opened, source_facts
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._pipeline_route_exec import resolve_temp_disk_budget
from decoy_engine.execution.out_of_core._scratch import engine_scratch_root
from decoy_engine.execution.pipeline._sample import approximate_columns
from decoy_engine.profile import LazySource, Profile, TableProfile

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping

    from decoy_engine.subset import SubsetRequest, SubsetSelection

_BATCH_ROWS = 65_536
_STEM = "subset"


def _head_batches(src: LazySource, rows: int) -> Iterator[pa.RecordBatch]:
    """Batches of the first `rows` rows of a lazy source, then close it. No row count or file
    facts are read. One batch holds them unless the reader cuts a short one."""
    taken = 0
    batches = src.iter_batches(rows)
    try:
        for batch in batches:
            yield batch
            taken += batch.num_rows
            if taken >= rows:
                return
    finally:
        close = getattr(batches, "close", None)
        if close is not None:
            close()


class SubsetStageScope:
    """One run's subset stage. Enter it around the run; `apply` selects and stages. With no
    request the scope is inert, so a run without a subset takes the same path."""

    def __init__(
        self, request: SubsetRequest | None, fingerprint: str | None, declared: bool = False
    ) -> None:
        self._request = request
        self.fingerprint = fingerprint
        self.declared = declared  # the config has a `subset:` block, applied or skipped
        self._stack = ExitStack()
        self.selection: SubsetSelection | None = None
        self.head_rows: dict[str, int] = {}  # a sample's head tables and the rows kept of each
        self._approximate: list[tuple[str, str]] = []
        self._source_rows: dict[str, pa.Table] | None = None

    def __enter__(self) -> SubsetStageScope:
        self._stack.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    def apply(
        self, sources: Mapping[str, pa.Table | LazySource], config: dict[str, Any]
    ) -> tuple[dict[str, pa.Table | LazySource], SubsetSelection]:
        from decoy_engine.subset import SubsetInternalError, scope_for, select_subset

        # Callers test `active` first, so a request is set.
        request = cast("SubsetRequest", self._request)
        sample = request.sample
        head: frozenset[str] = frozenset()
        inputs: Mapping[str, pa.Table | LazySource] = sources
        if sample is not None:
            # Scope first: a head table (no relationship edge) is read as its first rows and never
            # reaches the selection, so it is not counted or key-loaded. Its seed leaves the request
            # too, because preflight refuses a seed whose table was not supplied.
            head = scope_for(request, sources).head
            inputs = {n: s for n, s in sources.items() if n not in head}
            request = replace(request, seeds=tuple(s for s in request.seeds if s.table not in head))
            self._approximate = approximate_columns(config)
        rows = 0 if sample is None else sample.rows
        display = 0 if sample is None else sample.display_rows
        # Facts first: for a CSV this also records the file identity the read checks.
        expected = {n: source_facts(s) for n, s in inputs.items() if isinstance(s, LazySource)}
        selection = select_subset(request, inputs, config=config)
        self.selection = selection
        check = selection.materialization_check
        survivors = {n: sorted(v) for n, v in selection.survivors.items()}
        kept_keys: dict[str, list[pa.Table]] = {n: [] for n in survivors}
        self.head_rows = dict.fromkeys(sorted(head), 0)
        shown: dict[str, list[pa.Table]] = {}

        def note(table: str, kept: pa.Table) -> pa.Table:
            have = sum(t.num_rows for t in shown.get(table, ()))
            if have < display:
                shown.setdefault(table, []).append(kept.slice(0, display - have))
            return kept

        def batch_fn(table: str, batch: pa.RecordBatch, offset: int) -> pa.Table:
            if table in head:
                take = max(0, min(batch.num_rows, rows - self.head_rows[table]))
                self.head_rows[table] += take
                return note(table, pa.Table.from_batches([batch]).slice(0, take))
            kept_rows = survivors[table]
            lo = bisect_left(kept_rows, offset)
            hi = bisect_left(kept_rows, offset + batch.num_rows)
            local = pa.array([i - offset for i in kept_rows[lo:hi]], type=pa.int64())
            kept = pa.Table.from_batches([batch]).take(local)
            names = check.columns(table) if check is not None else ()
            if names:
                kept_keys[table].append(kept.select(list(names)))
            return note(table, kept)

        def resident_fn(table: str, value: pa.Table) -> pa.Table:
            if table in head:
                kept = value.slice(0, rows)
                self.head_rows[table] = kept.num_rows
            else:
                kept = value.take(pa.array(survivors[table], type=pa.int64()))
            return note(table, kept)

        def open_fn(table: str, src: LazySource) -> Iterator[pa.RecordBatch]:
            if table in head:
                yield from _head_batches(src, rows)
                return
            opened = src.open_batches(_BATCH_ROWS)
            try:
                check_opened(opened, expected[table], table=table)
                yield from opened.batches
            finally:
                opened.close()

        root = self._stack.enter_context(engine_scratch_root(None))
        staged = stage_typed_sources(
            [*sorted(survivors), *sorted(head)],
            sources,
            batch_fn=batch_fn,
            empty_fn=lambda _table, src: src.schema.empty_table(),
            resident_fn=resident_fn,
            root=root,
            batch_rows=_BATCH_ROWS,
            max_bytes=resolve_temp_disk_budget(),
            stem=_STEM,
            open_fn=open_fn,
        )
        for table, parts in kept_keys.items():
            src = sources[table]
            if check is not None and check.columns(table):
                if isinstance(src, LazySource):
                    schema = pa.schema([src.schema.field(c) for c in check.columns(table)])
                    check.verify(table, pa.concat_tables(parts) if parts else schema.empty_table())
            if staged[table].num_rows != selection.counts[table][1]:
                raise SubsetInternalError(
                    code=reasons.runtime.SUBSET_MATERIALIZE_COUNT_MISMATCH,
                    message=f"subset table {table!r}: staged rows differ from the selection",
                )
        if display > 0:
            self._source_rows = {t: pa.concat_tables(parts) for t, parts in shown.items()}
        return staged, selection

    @property
    def active(self) -> bool:
        return self._request is not None

    def stamp(self, result: ExecutionResult) -> ExecutionResult:
        """Record the evidence block: counts and the fingerprint when applied, `applied: false`
        when the block was skipped, nothing when the config has no block."""
        if self.active:
            result.quality_metrics["subset"] = self._metrics()
            if self._source_rows is not None:
                return replace(result, sample_source_rows=self._source_rows)
        elif self.declared:
            result.quality_metrics["subset"] = {"applied": False}
        return result

    def _metrics(self) -> dict[str, Any]:
        sel = cast("SubsetSelection", self.selection)  # `apply` ran before the route
        metrics: dict[str, Any] = {
            "applied": True,
            "fingerprint": self.fingerprint,
            "tables": {
                name: {
                    "source_rows": source,
                    "kept_rows": kept,
                    "whole": name in sel.scope.whole,
                }
                for name, (source, kept) in sorted(sel.counts.items())
            },
            "excluded_edges": list(sel.excluded_edges),
        }
        sample = cast("SubsetRequest", self._request).sample
        if sample is not None:
            metrics["sample"] = {
                "rows": sample.rows,
                "roots": list(sample.roots),
                "head_tables": dict(sorted(self.head_rows.items())),
                "approximate_columns": [list(pair) for pair in self._approximate],
            }
        return metrics

    @staticmethod
    def restrict_profile(
        profile: Profile, selection: SubsetSelection, head_rows: Mapping[str, int] | None = None
    ) -> Profile:
        """Row counts of the in-scope tables become the kept counts; every other fact stays the
        full source's, so compile reads the counts only for its refusals, warnings and hash. A
        sample's head tables take the rows read from them."""
        narrowed: list[TableProfile] = []
        for table in profile.tables:
            if table.name in selection.scope.tables:
                kept = selection.counts[table.name][1]
            elif head_rows is not None and table.name in head_rows:
                kept = head_rows[table.name]
            else:
                narrowed.append(table)
                continue
            columns = tuple(
                replace(
                    col,
                    row_count=kept,
                    null_count=min(col.null_count, kept),
                    distinct_count=(
                        None if col.distinct_count is None else min(col.distinct_count, kept)
                    ),
                )
                for col in table.columns
            )
            narrowed.append(replace(table, row_count=kept, row_count_exact=True, columns=columns))
        return replace(profile, tables=tuple(narrowed))


def resolve_subset(
    config: dict[str, Any],
    subset_request: object,
    source_loader: Callable[[str], pa.Table] | None,
) -> tuple[dict[str, Any], SubsetStageScope]:
    """The run config (its `subset` block split off) and the stage that applies the request.

    Refuses before any read: an unknown `subset_request` value, and a subset on a
    `source_loader` run. `"skip"` leaves the stage inert; a `SubsetRequest` replaces the block."""
    if subset_request in ("config", "skip") and config.get("subset") is None:
        return config, SubsetStageScope(None, None)
    from decoy_engine.subset import SubsetRequest, split_subset_config

    if not (isinstance(subset_request, SubsetRequest) or subset_request in ("config", "skip")):
        raise ExecutionError(
            code=reasons.runtime.INVALID_EXECUTION_KNOB,
            message="subset_request must be 'config', 'skip' or a SubsetRequest.",
        )
    split = split_subset_config(config)
    request, fingerprint = split.request, split.fingerprint
    if subset_request == "skip":
        request = None
    elif isinstance(subset_request, SubsetRequest):
        request, fingerprint = subset_request, subset_request.fingerprint()
    if request is not None and source_loader is not None:
        refuse_subset_source_loader()
    return split.run_config, SubsetStageScope(request, fingerprint, split.request is not None)


__all__ = ["SubsetStageScope", "resolve_subset"]
