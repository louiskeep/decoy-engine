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
    commit failure). If this function itself raises partway through the
    write, the partial staging file is removed before the exception
    propagates -- nothing is ever left behind for this call's own failure.

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
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, default=_json_default) + "\n")
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged


def publish_staged_jsonl(staged_path: Path, final_path: str) -> None:
    """Atomically publish a staged quarantine JSONL to ``final_path``.

    A single ``os.replace`` (POSIX rename, same filesystem because
    ``write_jsonl_staged`` stages beside ``final_path``) makes the file visible
    at ``final_path`` all at once. Call ONLY after the sink's own commit has
    already succeeded (DE-08): this is the "publish" half of the
    stage-then-publish pair that gives the quarantine sidecar the same
    commit-or-discard fate as the transactional sink.

    Raises:
        OSError: If the rename fails (e.g. disk full, permission denied).
            Nothing is published on failure; ``staged_path`` is untouched.
    """
    os.replace(staged_path, Path(final_path))


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
