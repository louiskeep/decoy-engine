"""Quarantine-row support for SP-05 (P5.B.quarantine_rows).

When a row triggers any configured quarantine trigger, ``apply_quarantine``
routes it to a JSONL file at ``config["quarantine"]["output_path"]`` instead
of the main pipeline output. The job continues and completes successfully.

Wired triggers (SP-05):
  - ``validation_fail``: row failed a job-level validator (ValidationReport
    finding with row indices).

Reserved for future wiring (not active in SP-05):
  - ``format_error``: placeholder - not wired. Rejected at config validation.
  - ``mask_error``: placeholder - not wired. Rejected at config validation.

The quarantine output is JSON-lines (one JSON object per distinct quarantined
row). Each record carries the original column values plus extra fields:

  _quarantine_trigger
      Name of the first trigger that fired for this row.
  _quarantine_reason
      Human-readable explanation from the first ValidatorFinding for this row.

Deduplication: a row failing multiple validators appears ONCE in the quarantine
output (the first finding wins). ``total_quarantined`` equals the number of
distinct rows removed from main; ``counts_by_trigger`` tallies per finding and
may sum higher than ``total_quarantined`` when a row fails multiple validators.

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
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.validators._types import QuarantineSummary

if TYPE_CHECKING:
    from decoy_engine.validators._types import ValidationReport


def apply_quarantine(
    outputs: dict[str, pa.Table],
    report: ValidationReport,
    quarantine_config: dict[str, Any],
) -> tuple[dict[str, pa.Table], QuarantineSummary]:
    """Route failing rows to the quarantine output; return filtered main outputs.

    Reads the ``validation_fail`` trigger's row indices from ``report.findings``,
    removes those rows from the corresponding output tables, writes the removed
    rows to the JSONL file at ``quarantine_config["output_path"]``, and returns
    the trimmed outputs together with a ``QuarantineSummary``.

    If no rows are quarantined (the report has findings but the trigger does not
    match, or the row sets are empty), the quarantine file is NOT written and
    the outputs are returned unchanged.

    This function does not mutate any input ``pa.Table``; it builds new tables
    via ``pa.Table.filter`` + ``pyarrow.concat_tables`` which return new
    Arrow objects.

    Args:
        outputs: Pipeline output tables keyed by table name.
        report: Frozen ValidationReport from the validator framework.
        quarantine_config: The ``quarantine:`` config dict (validated dump).

    Returns:
        Tuple of (filtered outputs dict, QuarantineSummary).
    """
    output_path: str = quarantine_config.get("output_path") or ""
    triggers: list[str] = quarantine_config.get("triggers") or []

    # Collect one quarantine entry per DISTINCT (table, row_index) pair.
    # A row failing two validators appears once in the output file (first
    # finding wins for _quarantine_trigger and _quarantine_reason).
    # counts_by_trigger tallies per finding and may sum higher than
    # total_quarantined when a row fails multiple validators.
    quarantine_entries: list[dict[str, Any]] = []
    counts_by_trigger: dict[str, int] = defaultdict(int)
    seen_rows: set[tuple[str, int]] = set()

    if "validation_fail" in triggers:
        for finding in report.findings:
            tbl = outputs.get(finding.table)
            if tbl is None:
                continue
            col_pylist: dict[str, list[Any]] = {
                col: tbl.column(col).to_pylist() for col in tbl.schema.names
            }
            for row_idx in finding.failing_row_indices:
                counts_by_trigger["validation_fail"] += 1
                key = (finding.table, row_idx)
                if key in seen_rows:
                    continue  # already written; dedup per distinct row
                seen_rows.add(key)
                row_data: dict[str, Any] = {
                    col: col_pylist[col][row_idx] for col in tbl.schema.names
                }
                row_data["_quarantine_trigger"] = "validation_fail"
                row_data["_quarantine_reason"] = finding.detail
                row_data["_source_table"] = finding.table
                quarantine_entries.append(row_data)

    # total_quarantined = distinct rows removed from main (not sum of per-trigger counts).
    total = len(seen_rows)

    # Build per-table sets of row indices to remove from the main output.
    rows_to_remove: dict[str, set[int]] = defaultdict(set)
    if "validation_fail" in triggers:
        for finding in report.findings:
            if finding.table in outputs:
                rows_to_remove[finding.table].update(finding.failing_row_indices)

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

    # Fail-closed backstop: if rows need to be written but output_path is
    # absent, raise rather than silently dropping rows (data loss). Pydantic
    # config validation (QuarantineConfig._fail_closed_when_enabled) catches
    # this earlier for callers who go through PipelineConfig.model_validate;
    # this guard covers raw-dict callers that bypass Pydantic.
    if quarantine_entries and not output_path:
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
        counts_by_trigger=dict(counts_by_trigger),
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
