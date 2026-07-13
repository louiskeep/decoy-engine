"""Quarantine-row support for SP-05 (P5.B.quarantine_rows) + the Sprint 2
honesty pack (D9).

When a row triggers any configured quarantine trigger, ``apply_quarantine``
routes it to a JSONL file at ``config["quarantine"]["output_path"]`` instead
of the main pipeline output. The job continues and completes successfully.

Wired triggers:
  - ``validation_fail`` (SP-05): row failed a job-level validator
    (ValidationReport finding with row indices).
  - ``format_error`` (Sprint 2 honesty pack S5): a cell could not be
    coerced/parsed under its declared strategy (bucketize, date_shift).
  - ``mask_error`` (Sprint 2 honesty pack S6): a per-value masking operation
    raised (code_set chapter_preserve edge cases).

The quarantine output is JSON-lines (one JSON object per distinct quarantined
row). Each record carries the original column values plus extra fields:

  _quarantine_trigger
      Name of the first trigger that fired for this row.
  _quarantine_reason
      Human-readable explanation from the first finding/row-error for this row.

Deduplication: a row failing multiple validators/triggers appears ONCE in the
quarantine output (the first entry wins, validator findings before row
errors -- see the normalization order in `_normalize_worklist`).
``total_quarantined`` equals the number of distinct rows removed from main;
``counts_by_trigger`` tallies per finding/row-error and may sum higher than
``total_quarantined`` when a row fails multiple triggers.

The main output has all quarantined rows removed; its schema is unchanged.
If no rows are quarantined, the JSONL file is not written.

Evidence manifest:
  ``quality_metrics["quarantine"]`` carries a ``QuarantineSummary`` dict
  (serialised via ``dataclasses.asdict``) with ``enabled``, ``output_path``,
  ``counts_by_trigger``, and ``total_quarantined``.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.validators._types import QuarantineSummary

if TYPE_CHECKING:
    from collections.abc import Sequence

    from decoy_engine.execution._row_errors import RowErrorRecord
    from decoy_engine.validators._types import ValidationReport


@dataclasses.dataclass(frozen=True)
class _WorklistItem:
    """One normalized (table, row_index, trigger, reason) entry, D9."""

    table: str
    row_index: int
    trigger: str
    reason: str


def _normalize_worklist(
    report: ValidationReport | None,
    row_errors: tuple[RowErrorRecord, ...],
    triggers: list[str],
) -> list[_WorklistItem]:
    """Build one normalized worklist from validator findings (tagged
    "validation_fail") and row-error records (tagged with their own
    trigger). Only entries whose trigger is in `triggers` are included.
    Validator findings are ordered first so a row failing both a validator
    and a row-error trigger keeps the validator's reason as "first wins"
    (matches the pre-D9 dedup behavior exactly for validation_fail-only
    runs -- the regression pin)."""
    items: list[_WorklistItem] = []
    if report is not None and "validation_fail" in triggers:
        for finding in report.findings:
            for row_idx in finding.failing_row_indices:
                items.append(
                    _WorklistItem(
                        table=finding.table,
                        row_index=row_idx,
                        trigger="validation_fail",
                        reason=finding.detail,
                    )
                )
    for record in row_errors:
        if record.trigger not in triggers:
            continue
        items.append(
            _WorklistItem(
                table=record.table,
                row_index=record.row_index,
                trigger=record.trigger,
                reason=record.reason,
            )
        )
    return items


def compute_quarantine(
    outputs: dict[str, pa.Table],
    report: ValidationReport | None,
    quarantine_config: dict[str, Any],
    *,
    row_errors: tuple[RowErrorRecord, ...] = (),
) -> tuple[dict[str, pa.Table], list[dict[str, Any]], dict[str, int], int]:
    """Pure compute+filter core of quarantine (S2 sequential-path extraction).

    Builds the normalized worklist (see ``_normalize_worklist``), produces the
    quarantine entry dicts and per-trigger counts, and returns the outputs
    with bad rows removed. Does NO file I/O (unlike ``apply_quarantine``, its
    only caller pre-extraction), so a caller that needs to defer the JSONL
    write (e.g. ``run_sequential``, which quarantines one table at a time but
    writes exactly one JSONL file across the whole run to avoid the
    truncating ``_write_jsonl`` clobbering earlier tables) can compose it per
    call and write once at the end.

    This function does not mutate any input ``pa.Table``; it builds new tables
    via ``pa.Table.filter`` which returns a new Arrow object.

    Args:
        outputs: Pipeline output tables keyed by table name.
        report: Frozen ValidationReport from the validator framework, or
            None when no validators are configured (row-errors-only calls).
        quarantine_config: The ``quarantine:`` config dict (validated dump).
        row_errors: Table-attributed per-row strategy errors (D7/D8).
            Default empty tuple: existing callers are unaffected.

    Returns:
        Tuple of (filtered_outputs, entries, counts_by_trigger, total).
        ``total`` is the count of distinct (table, row_index) pairs removed;
        ``counts_by_trigger`` may sum higher when a row fails multiple triggers.
    """
    triggers: list[str] = quarantine_config.get("triggers") or []

    worklist = _normalize_worklist(report, row_errors, triggers)

    # Collect one quarantine entry per DISTINCT (table, row_index) pair.
    # A row failing multiple triggers appears once in the output file (first
    # entry wins for _quarantine_trigger and _quarantine_reason).
    # counts_by_trigger tallies per entry and may sum higher than
    # total_quarantined when a row fails multiple triggers.
    quarantine_entries: list[dict[str, Any]] = []
    counts_by_trigger: dict[str, int] = defaultdict(int)
    seen_rows: set[tuple[str, int]] = set()
    rows_to_remove: dict[str, set[int]] = defaultdict(set)

    table_col_cache: dict[str, dict[str, list[Any]]] = {}
    for item in worklist:
        tbl = outputs.get(item.table)
        if tbl is None:
            continue
        counts_by_trigger[item.trigger] += 1
        rows_to_remove[item.table].add(item.row_index)
        key = (item.table, item.row_index)
        if key in seen_rows:
            continue  # already written; dedup per distinct row
        seen_rows.add(key)
        col_pylist = table_col_cache.get(item.table)
        if col_pylist is None:
            col_pylist = {col: tbl.column(col).to_pylist() for col in tbl.schema.names}
            table_col_cache[item.table] = col_pylist
        row_data: dict[str, Any] = {
            col: col_pylist[col][item.row_index] for col in tbl.schema.names
        }
        row_data["_quarantine_trigger"] = item.trigger
        row_data["_quarantine_reason"] = item.reason
        row_data["_source_table"] = item.table
        quarantine_entries.append(row_data)

    # total_quarantined = distinct rows removed from main (not sum of per-trigger counts).
    total = len(seen_rows)

    # Filter the main output tables (remove quarantined rows).
    filtered_outputs: dict[str, pa.Table] = {}
    for table_name, table in outputs.items():
        bad_rows = rows_to_remove.get(table_name)
        if not bad_rows:
            filtered_outputs[table_name] = table
            continue
        # Build a boolean keep-mask: True for rows that stay in main output.
        n = table.num_rows
        keep_mask = pa.array([i not in bad_rows for i in range(n)], type=pa.bool_())
        filtered_outputs[table_name] = table.filter(keep_mask)

    return filtered_outputs, quarantine_entries, dict(counts_by_trigger), total


def apply_quarantine(
    outputs: dict[str, pa.Table],
    report: ValidationReport | None,
    quarantine_config: dict[str, Any],
    *,
    row_errors: tuple[RowErrorRecord, ...] = (),
) -> tuple[dict[str, pa.Table], QuarantineSummary]:
    """Route failing rows to the quarantine output; return filtered main outputs.

    Sprint 2 honesty pack (D9): builds one normalized worklist of
    ``(table, row_index, trigger, reason)`` from ``report.findings`` (each
    tagged ``"validation_fail"``) and ``row_errors`` (each tagged with its
    own trigger), then runs the existing dedup/write/filter machinery once
    over the worklist (now factored into ``compute_quarantine``). Callers
    that only pass ``report`` (``row_errors=()``, the default) get
    byte-identical behavior to the pre-D9 implementation -- the existing
    quarantine test suite pins this.

    If no rows are quarantined, the quarantine file is NOT written and the
    outputs are returned unchanged.

    This function does not mutate any input ``pa.Table``; it builds new tables
    via ``pa.Table.filter`` which returns a new Arrow object.

    Args:
        outputs: Pipeline output tables keyed by table name.
        report: Frozen ValidationReport from the validator framework, or
            None when no validators are configured (row-errors-only calls).
        quarantine_config: The ``quarantine:`` config dict (validated dump).
        row_errors: Table-attributed per-row strategy errors (D7/D8).
            Default empty tuple: existing callers are unaffected.

    Returns:
        Tuple of (filtered outputs dict, QuarantineSummary).
    """
    output_path: str = quarantine_config.get("output_path") or ""

    filtered_outputs, quarantine_entries, counts_by_trigger, total = compute_quarantine(
        outputs, report, quarantine_config, row_errors=row_errors
    )

    # Fail-closed backstop: if rows need to be written but output_path is
    # absent, raise rather than silently dropping rows (data loss). Pydantic
    # config validation (QuarantineConfig._fail_closed_when_enabled) catches
    # this earlier for callers who go through PipelineConfig.model_validate;
    # this guard covers raw-dict callers that bypass Pydantic.
    if quarantine_entries and not (output_path or "").strip():
        raise ValueError(
            f"apply_quarantine: {len(quarantine_entries)} row(s) must be quarantined "
            "but output_path is empty. Set quarantine.output_path to a non-empty path "
            "to avoid silent data loss."
        )

    # Write the quarantine JSONL file only when rows were quarantined.
    if quarantine_entries and output_path:
        _write_jsonl(output_path, quarantine_entries)

    summary = QuarantineSummary(
        enabled=True,
        output_path=output_path,
        counts_by_trigger=counts_by_trigger,
        total_quarantined=total,
    )
    return filtered_outputs, summary


def _write_jsonl(path: str, records: list[dict[str, Any]]) -> None:
    """Write ``records`` to a JSON-lines file at ``path``.

    Each record is one line of JSON. The parent directory is created if it
    does not exist. Values that are not JSON-serialisable (e.g. ``pyarrow``
    scalars, ``None``) are converted via the ``_json_default`` fallback.

    Args:
        path: Absolute or relative path for the output file.
        records: List of dicts to serialise.
    """
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, default=_json_default) + "\n")


def write_jsonl_staged(final_path: str, records: list[dict[str, Any]]) -> Path:
    """Write ``records`` as JSONL to a private staging file beside ``final_path``,
    returning the staging path. Never touches ``final_path`` itself.

    DE-08 (HIGH data-safety finding): a transactional caller (``run_sequential``
    with a ``TransactionalSink``) must not publish the quarantine sidecar to its
    final path until the sink's own commit has succeeded -- otherwise a sink
    abort (table staging discarded) leaves the raw-row quarantine JSONL
    published anyway. This mirrors ``ParquetTransactionalSink``'s staging
    discipline (`execution/_transactional_sink.py`): the staging file is
    created in ``final_path``'s PARENT directory (same filesystem as the
    final path) via ``tempfile.mkstemp``, so the later publish step is a
    single same-filesystem ``os.replace`` (POSIX rename), never a copy.

    The caller owns the staged file's fate: call ``publish_staged_jsonl`` on
    success, or ``discard_staged_jsonl`` on any failure (including a sink
    commit failure). If this function itself raises -- partway through the
    write, or from the terminal close after every record has been written --
    cleanup is best-effort: a cleanup failure (closing the file, or unlinking
    the partial temp file) is swallowed rather than propagated, so it can
    never mask the real error -- the caller always sees whichever error
    actually occurred (the write/serialize/fdopen error, or, if writing
    succeeded but the terminal close itself failed, e.g. a buffered flush
    hitting ENOSPC, that close error), never a `raise`-in-`except` shadow.

    Args:
        final_path: The path the caller intends to publish to eventually.
            Only used to resolve the staging directory (same parent); never
            written to by this function.
        records: List of dicts to serialise, one JSON object per line.

    Returns:
        Path to the private staging file.
    """
    out_path = Path(final_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix="_decoy_quarantine_stage_", suffix=".jsonl", dir=out_path.parent
    )
    staged = Path(tmp_name)
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        # fdopen wraps the raw mkstemp fd in a file object; if it raises
        # before doing so, the bare fd has no destructor and would leak.
        # Close/unlink are best-effort so a cleanup failure cannot mask the
        # real fdopen error via the bare raise.
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        for record in records:
            fh.write(json.dumps(record, default=_json_default) + "\n")
    except BaseException:
        # Mirrors ParquetTransactionalSink.write_batches: close/unlink are
        # best-effort cleanup for a stage that must never be left behind
        # with a raw record in it, and neither may mask the original
        # write/serialize error propagating via the bare raise.
        try:
            fh.close()
        except Exception:
            pass
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    try:
        fh.close()
    except BaseException:
        # The write loop above finished, but the terminal close (a buffered
        # flush to disk) can still fail, e.g. ENOSPC. Left unguarded, this
        # raise would skip `return staged` entirely -- the caller never gets
        # a staged path to pass to `discard_staged_jsonl`, so the raw-PII
        # temp file would leak. Best-effort unlink mirrors the write-loop
        # cleanup above.
        try:
            staged.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return staged


def publish_staged_jsonl(staged_path: Path, final_path: str) -> None:
    """Exclusively publish a staged quarantine JSONL to ``final_path``.

    Uses ``os.link`` (creates a hardlink; atomic and, since
    ``write_jsonl_staged`` stages beside ``final_path``, same-filesystem) then
    unlinks the stage -- instead of ``os.replace`` -- so the publish itself is
    fail-CLOSED: an already-existing ``final_path`` is refused rather than
    silently overwritten. Call ONLY after the sink's own commit has already
    succeeded (DE-08): this is the "publish" half of the stage-then-publish
    pair that gives the quarantine sidecar the same commit-or-discard fate as
    the transactional sink.

    Why exclusive-create, not name-based aliasing checks (dennis re-gate
    HIGH): ``guard_quarantine_not_aliasing_committed_table`` only fires for
    sinks that expose a ``committed_table_path`` method
    (``ParquetTransactionalSink`` does). A duck-typed ``TransactionalSink``
    -- write/commit/abort present, but no ``committed_table_path`` -- routed
    through this same staged-publish path with that guard silently no-op'ing
    (``getattr(sink, "committed_table_path", None)`` returns ``None``), so an
    unconditional ``os.replace`` would overwrite the sink's just-committed
    masked table with the raw-PII quarantine JSONL while the run still
    reported SUCCESS. In transactional mode the sidecar always publishes into
    a fresh all-or-nothing location, so ``final_path`` must not pre-exist --
    exclusive-create closes the raw-PII-overwrite path uniformly for Parquet,
    duck-typed, and arbitrary custom sinks alike, without depending on the
    sink advertising its committed paths at all. It also shrinks the
    check-then-publish TOCTOU the name-based guard had: there is no window
    between "check the name" and "replace the file" for the path to have come
    into existence in between -- the filesystem's own exclusive-link
    semantics make the check-and-act a single atomic operation.

    Raises:
        ValueError: ``final_path`` already exists. Nothing is published (the
            pre-existing file at ``final_path`` is left completely
            untouched); the staged temp file is best-effort discarded here so
            no raw-PII stage is left behind (the caller's own cleanup, e.g.
            ``finalize_committed_quarantine``, discards it again for defense
            in depth -- ``discard_staged_jsonl`` is idempotent).
        OSError: The link fails for any other reason (e.g. a genuine
            cross-device error -- should not happen, since the stage is
            created beside ``final_path`` -- or permission denied). Nothing
            is published; ``staged_path`` is left for the caller to discard.
            Never falls back to a plain overwrite.
    """
    final = Path(final_path)
    try:
        os.link(staged_path, final)
    except FileExistsError:
        try:
            staged_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(
            f"refusing to publish quarantine: a file already exists at "
            f"quarantine.output_path {final_path!r}; transactional publish "
            "will not overwrite it"
        ) from None
    os.unlink(staged_path)


def guard_quarantine_not_aliasing_committed_table(
    sink: object, written_tables: Sequence[str], output_path: str
) -> None:
    """Raise if ``output_path`` resolves to a table artifact ``sink`` just
    durably committed (DE-08 re-gate HIGH #1).

    The commit-first reorder (module docstring of ``execution/_sequential.py``)
    fixed the ordering hazard between the quarantine sidecar and the sink's
    own commit, but ``publish_staged_jsonl``'s unconditional ``os.replace``
    still has no idea ``output_path`` might alias a table this same run just
    published, e.g. ``quarantine.output_path: out/parent.parquet`` against a
    ``ParquetTransactionalSink(Path("out"))``. On ``origin/main`` (pre-reorder)
    that misconfiguration raised loudly (``ENOTEMPTY``, see
    ``TestNestedLayoutSinkAndQuarantineShareParentDirectory``); left unguarded
    post-reorder it silently overwrites the masked table with raw JSONL.

    Only checked for sinks that expose a ``committed_table_path(table) ->
    Path`` method (``ParquetTransactionalSink`` does, see
    ``execution/_transactional_sink.py``). An arbitrary custom or duck-typed
    ``TransactionalSink`` that does not expose its committed paths is
    unaffected by THIS particular guard -- and against ``origin/main`` (pre
    the commit-first reorder this module's docstring describes) that omission
    WAS a new gap the reorder introduced: ``publish_staged_jsonl`` used an
    unconditional ``os.replace``, so a duck-typed sink with no
    ``committed_table_path`` had no line of defense at all and would silently
    overwrite a just-committed masked table with raw-PII JSONL while the run
    still reported SUCCESS (dennis re-gate HIGH). ``publish_staged_jsonl`` is
    now itself fail-closed (exclusive create via ``os.link``; see its
    docstring) for every sink shape -- Parquet, duck-typed, and arbitrary
    custom alike -- so this function is now only a friendlier, earlier,
    name-based error for the one sink type that can support it; it is no
    longer the only line of defense. The natural layout, the quarantine JSONL
    living INSIDE the sink's own target directory under its own name
    (``out/quarantine.jsonl``), is never equal to any table's committed path,
    so it is unaffected too.

    Raises:
        ValueError: ``output_path`` is exactly a table this run just
            committed. Nothing has been staged or published for the
            quarantine sidecar yet when this raises, and the already-
            committed table is untouched.
    """
    resolver = getattr(sink, "committed_table_path", None)
    if not callable(resolver):
        return
    target = Path(output_path).resolve()
    for table in written_tables:
        committed = Path(resolver(table)).resolve()
        if committed == target:
            raise ValueError(
                f"quarantine.output_path {output_path!r} aliases the output "
                f"artifact this run just committed for table {table!r} "
                f"({committed}); refusing to overwrite a masked table with "
                "the raw-PII quarantine sidecar. Point quarantine.output_path "
                "at a different file."
            )


def finalize_committed_quarantine(
    *,
    sink: object,
    is_genuine_transactional_sink: bool,
    written_tables: Sequence[str],
    output_path: str,
    entries: list[dict[str, Any]],
    counts_by_trigger: dict[str, int],
) -> dict[str, Any] | None:
    """Publish the quarantine sidecar; return its evidence-manifest entry.

    Called by ``run_sequential`` strictly AFTER ``_tsink.commit()`` has
    already returned successfully (or with no sink / a non-transactional
    Callable sink, where there was nothing to wait for) -- see
    ``execution/_sequential.py``'s module docstring for the full
    commit-or-discard contract this preserves. Returns ``None`` (nothing to
    record) when ``entries`` is empty.

    For a genuine ``TransactionalSink``, stages then atomically publishes
    (guarded by ``guard_quarantine_not_aliasing_committed_table`` first); for
    no sink or a plain Callable sink, writes straight to ``output_path`` as
    before DE-08 (never staged, so a special/non-directory path like
    ``/dev/null`` cannot crash attempting to stage a temp file there).
    """
    if not entries:
        return None
    if is_genuine_transactional_sink:
        guard_quarantine_not_aliasing_committed_table(sink, written_tables, output_path)
        staged = write_jsonl_staged(output_path, entries)
        try:
            publish_staged_jsonl(staged, output_path)
        except BaseException:
            # Tables are already committed; this is a bare sidecar-publish
            # failure, not a run failure -- discard the orphaned staged file
            # and surface the error.
            discard_staged_jsonl(staged)
            raise
    else:
        _write_jsonl(output_path, entries)
    return quarantine_manifest(
        QuarantineSummary(
            enabled=True,
            output_path=output_path,
            counts_by_trigger=counts_by_trigger,
            total_quarantined=len(entries),
        )
    )


def discard_staged_jsonl(staged_path: Path | None) -> None:
    """Best-effort delete of a staged quarantine file that must never be
    published (mirrors ``TransactionalSink.abort``'s best-effort staging
    cleanup: errors are swallowed so a cleanup failure cannot mask the
    original run exception). A no-op when ``staged_path`` is None (nothing
    was ever staged for this run)."""
    if staged_path is None:
        return
    try:
        staged_path.unlink(missing_ok=True)
    except OSError:
        pass


def _json_default(obj: Any) -> Any:
    """Fallback serialiser for json.dumps."""
    if obj is None:
        return None
    return str(obj)


def quarantine_manifest(summary: QuarantineSummary) -> dict[str, Any]:
    """Serialise a QuarantineSummary to a plain dict for the evidence manifest.

    Args:
        summary: Frozen QuarantineSummary from ``apply_quarantine``.

    Returns:
        Plain dict suitable for storing in ``quality_metrics["quarantine"]``.
    """
    return dataclasses.asdict(summary)
