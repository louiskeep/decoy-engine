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
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.declared_casting import stage_typed_sources
from decoy_engine.execution._chunked_input import check_opened, source_facts
from decoy_engine.execution._pipeline_route_exec import resolve_temp_disk_budget
from decoy_engine.execution.out_of_core._scratch import engine_scratch_root
from decoy_engine.profile import Profile, TableProfile
from decoy_engine.profile._readers import LazySource
from decoy_engine.subset import SubsetInternalError, SubsetRequest, SubsetSelection, select_subset

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_BATCH_ROWS = 65_536
_STEM = "subset"


class SubsetStageScope:
    """One run's subset stage. Enter it around the run; `apply` selects and stages."""

    def __init__(self, request: SubsetRequest, fingerprint: str) -> None:
        self._request = request
        self.fingerprint = fingerprint
        self._stack = ExitStack()
        self.selection: SubsetSelection | None = None

    def __enter__(self) -> SubsetStageScope:
        self._stack.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()

    def apply(
        self, sources: Mapping[str, pa.Table | LazySource], config: dict[str, Any]
    ) -> tuple[dict[str, pa.Table | LazySource], SubsetSelection]:
        # Facts first: for a CSV this also records the file identity the read checks.
        expected = {n: source_facts(s) for n, s in sources.items() if isinstance(s, LazySource)}
        selection = select_subset(self._request, sources, config=config)
        self.selection = selection
        check = selection.materialization_check
        survivors = {n: sorted(v) for n, v in selection.survivors.items()}
        kept_keys: dict[str, list[pa.Table]] = {n: [] for n in survivors}

        def batch_fn(table: str, batch: pa.RecordBatch, offset: int) -> pa.Table:
            rows = survivors[table]
            lo, hi = bisect_left(rows, offset), bisect_left(rows, offset + batch.num_rows)
            local = pa.array([i - offset for i in rows[lo:hi]], type=pa.int64())
            kept = pa.Table.from_batches([batch]).take(local)
            names = check.columns(table) if check is not None else ()
            if names:
                kept_keys[table].append(kept.select(list(names)))
            return kept

        def open_fn(table: str, src: LazySource) -> Iterator[pa.RecordBatch]:
            opened = src.open_batches(_BATCH_ROWS)
            try:
                check_opened(opened, expected[table], table=table)
                yield from opened.batches
            finally:
                opened.close()

        root = self._stack.enter_context(engine_scratch_root(None))
        staged = stage_typed_sources(
            sorted(survivors),
            sources,
            batch_fn=batch_fn,
            empty_fn=lambda _table, src: src.schema.empty_table(),
            resident_fn=lambda table, value: value.take(
                pa.array(survivors[table], type=pa.int64())
            ),
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
        return staged, selection

    def metrics(self) -> dict[str, Any]:
        """The value-free `quality_metrics["subset"]` block for an applied subset."""
        sel = self.selection
        if sel is None:  # no `apply` ran: nothing was selected
            return {"applied": False}
        return {
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

    @staticmethod
    def restrict_profile(profile: Profile, selection: SubsetSelection) -> Profile:
        """Row counts of the in-scope tables become the kept counts; every other fact stays the
        full source's, so compile reads the counts only for its refusals, warnings and hash."""
        narrowed: list[TableProfile] = []
        for table in profile.tables:
            if table.name not in selection.scope.tables:
                narrowed.append(table)
                continue
            kept = selection.counts[table.name][1]
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


__all__ = ["SubsetStageScope"]
