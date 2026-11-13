"""Post-scan integrity runner -- assembles the report (Reframe-A).

Public entry: ``run_post_mask``. Called by the platform's
post_mask_hook AFTER a successful mask job lands its output. Pure
function: takes source + output frames + config dict, returns a
JSON-serializable post-scan-integrity report dict.

Best-effort: each check category runs inside its own try/except so a
broken check doesn't kill the whole pass. A check that raises is
recorded as a single ``error``-severity finding with the exception
type name; the other checks still run + produce their findings.

The catastrophic-failure case (the runner itself can't even start, e.g.
``output_frames`` is the wrong type) raises TypeError. The platform
hook catches that + writes a JobPostScanIntegrityReport with
``pass_failed_with: "TypeError"`` so the FE has something to render.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from decoy_engine import fileaccess
from decoy_engine.profiling import MEMORY_LIMIT, passes_through
from decoy_engine.verification.postmask._blocked import severity_counts
from decoy_engine.verification.postmask._column_facts import ColumnFacts, column_facts
from decoy_engine.verification.postmask._keys import KeyResult
from decoy_engine.verification.postmask._stream_input import TableBatches, from_table
from decoy_engine.verification.postmask.fk_preservation import fk_findings

# The runner keeps its check names: callers and tests patch them here to fail one check.
from decoy_engine.verification.postmask.policy_validation import (
    policy_findings as check_policy_validation,
)
from decoy_engine.verification.postmask.residual_pii import (
    residual_pii_findings as check_residual_pii,
)
from decoy_engine.verification.postmask.types import (
    SCHEMA_VERSION,
    FKPreservationFinding,
    PolicyValidationFinding,
    PostMaskReport,
    ResidualPIIFinding,
)


def run_post_mask(
    source_frames: dict[str, pd.DataFrame],
    output_frames: dict[str, pd.DataFrame],
    *,
    config: dict[str, Any],
    keys: Sequence[KeyResult] | None = None,
) -> dict[str, Any]:
    """Run all three post-mask check categories + assemble the report.

    Args:
        source_frames: ``{table_name: pre-mask DataFrame}``. Required
            for policy_validation and for residual_pii's source
            comparison (escalates output==source leaks to 'fail').
        output_frames: ``{table_name: post-mask DataFrame}``. Required
            for all three checks.
        config: the validated pipeline config dict (from
            ``PipelineConfig.model_validate(yaml).model_dump()``).
            Used to look up configured masks + relationships.
        keys: the declared keys' facts (`check_keys`) when the caller has counted them; the
            FK findings derive from these. None counts them here from the two frame dicts.

    Returns:
        A post-scan-integrity/v2 report dict. JSON-serializable (every
        field is a primitive or list of dataclass-asdict'd findings).
        Caller persists this to JobPostScanIntegrityReport.report_json.

    Raises:
        TypeError: if the frame dicts are not actually dicts of
            DataFrames or config is not a dict. Programming errors;
            the platform hook converts these into a single error-row
            report so the FE has something to show.
    """
    if not isinstance(source_frames, dict) or not isinstance(output_frames, dict):
        raise TypeError(
            "run_post_mask: source_frames and output_frames must be dicts "
            f"of pandas DataFrames, got {type(source_frames).__name__} and "
            f"{type(output_frames).__name__}"
        )
    if not isinstance(config, dict):
        raise TypeError(f"run_post_mask: config must be a dict, got {type(config).__name__}")

    with fileaccess.protected_scratch_dir(prefix="decoy-post-scan-columns-") as scratch:
        return _assemble(
            config,
            lambda: _frame_tables(source_frames, output_frames),
            lambda: fk_findings(config, source_frames, output_frames, keys),
            scratch=scratch,
            memory_limit=MEMORY_LIMIT,
        )


def run_post_mask_tables(
    source_tables: Mapping[str, TableBatches],
    output_tables: Mapping[str, TableBatches],
    *,
    config: Mapping[str, Any],
    keys: Sequence[KeyResult],
    scratch: Path,
    memory_limit: str,
) -> dict[str, Any]:
    """`run_post_mask` over tables the run holds as batches. The caller counted the declared keys
    (`check_keys`) and owns `scratch`; the column suite reads the tables in bounded batches."""
    if _megabytes(memory_limit) > _megabytes(MEMORY_LIMIT):
        raise ValueError("run_post_mask_tables: memory_limit is above the post-scan cap")
    return _assemble(
        dict(config),
        lambda: (dict(source_tables), dict(output_tables), {}),
        lambda: fk_findings(config, {}, {}, keys),
        scratch=scratch,
        memory_limit=memory_limit,
    )


def _megabytes(limit: str) -> int:
    match = re.fullmatch(r"(\d+)MB", limit)
    if match is None:
        raise ValueError("run_post_mask_tables: memory_limit must look like '512MB'")
    return int(match.group(1))


def _arrow_column(series: pd.Series) -> pa.Array:
    """One column as Arrow. A column Arrow will not take (mixed int and str, Decimal with float, a
    set) is carried as the text of each value, which is all the detectors read and which keeps
    equal values equal. Only that column changes; its table and the others convert as before."""
    try:
        return pa.array(series, from_pandas=True)
    except Exception:
        return pa.array([None if _null(v) else str(v) for v in series.tolist()], type=pa.string())


def _null(value: object) -> bool:
    return value is None or (isinstance(value, float) and value != value)


def _frame_table(name: str, frame: pd.DataFrame) -> TableBatches:
    columns = [(str(c), _arrow_column(frame.iloc[:, n])) for n, c in enumerate(frame.columns)]
    return from_table(name, pa.Table.from_arrays([a for _, a in columns], [c for c, _ in columns]))


def _frame_tables(
    source_frames: dict[str, pd.DataFrame], output_frames: dict[str, pd.DataFrame]
) -> tuple[dict[str, TableBatches], dict[str, TableBatches], dict[str, str]]:
    """Both sides as tables. A name whose frame cannot be converted at all is left out of both
    and recorded with the error's class name, so the other tables are still checked."""
    src: dict[str, TableBatches] = {}
    out: dict[str, TableBatches] = {}
    failed: dict[str, str] = {}
    for name, out_frame in output_frames.items():
        try:
            out_table = _frame_table(name, out_frame)
            if name in source_frames:
                src[name] = _frame_table(name, source_frames[name])
            out[name] = out_table
        except Exception as exc:
            if passes_through(exc):
                raise
            src.pop(name, None)
            failed[name] = type(exc).__name__
    return src, out, failed


def _assemble(
    config: Mapping[str, Any],
    get_tables: Callable[
        [], tuple[dict[str, TableBatches], dict[str, TableBatches], dict[str, str]]
    ],
    get_fk: Callable[[], list[FKPreservationFinding]],
    *,
    scratch: Path,
    memory_limit: str,
) -> dict[str, Any]:
    residual_pii: list[ResidualPIIFinding] = []
    fk_preservation: list[FKPreservationFinding] = []
    policy_validation: list[PolicyValidationFinding] = []
    pass_failed_with: str | None = None

    # Each check runs in its own try/except so one bad check doesn't
    # kill the pass. Failures are recorded as error-severity findings.
    # Dennis H3 fix (2026-06-01): the exception message previously
    # included `{exc}` which surfaced raw exception text into the
    # report payload + the FE Post-scan integrity view. An OSError on a missing path
    # could leak `/home/user/.ssh/...`; a KeyError could expose a
    # column name an operator didn't expect to see in a UI-rendered
    # report. The typed name (`{type(exc).__name__}`) is sufficient
    # for the operator to find the underlying error in the job log,
    # where the full exception text already lives at the catch site.
    # A coded engine error (a file-access or lazy-source refusal) is not a check failure: it
    # propagates unchanged through `passes_through`, the rule pre-scan applies too.
    # One facts pass feeds both column checks. A table or column that fails is isolated: its
    # error is recorded by class name and the other tables and columns are still checked.
    source_tables, output_tables, failed = get_tables()
    facts, suite_failed = _suite_facts(
        source_tables, output_tables, scratch=scratch, memory_limit=memory_limit
    )
    failed = {**failed, **suite_failed}

    try:
        residual_pii = check_residual_pii(facts, config, failed)
    except Exception as exc:
        if passes_through(exc):
            raise
        residual_pii = [
            ResidualPIIFinding(
                table="",
                column="",
                detector_id="",
                match_rate=0.0,
                severity="error",
                message=f"residual_pii check raised {type(exc).__name__} (see job log for details)",
            )
        ]

    try:
        fk_preservation = get_fk()
    except Exception as exc:
        if passes_through(exc):
            raise
        fk_preservation = [
            FKPreservationFinding(
                parent_table="",
                parent_column="",
                child_table="",
                child_column="",
                severity="error",
                orphan_count=0,
                total_child_rows=0,
                orphan_rate=0.0,
                message=f"fk_preservation check raised {type(exc).__name__} (see job log for details)",
            )
        ]

    try:
        policy_validation = check_policy_validation(
            config, source_tables, output_tables, facts, failed
        )
    except Exception as exc:
        if passes_through(exc):
            raise
        policy_validation = [
            PolicyValidationFinding(
                table="",
                column="",
                strategy="",
                severity="error",
                message=f"policy_validation check raised {type(exc).__name__} (see job log for details)",
            )
        ]

    # Tally severity counters for the FE summary line.
    pass_count, warning_count, fail_count, error_count = severity_counts(
        getattr(finding, "severity", "info")
        for finding in (*residual_pii, *fk_preservation, *policy_validation)
    )

    # Dennis M23 fix (2026-06-01): stamp generated_at at report
    # construction so the JobPostScanIntegrityReport row column + the engine
    # payload agree. Module-level import would force a stdlib pull at
    # every engine import; local for now.
    from datetime import datetime, timezone

    report = PostMaskReport(
        schema_version=SCHEMA_VERSION,
        residual_pii=tuple(residual_pii),
        fk_preservation=tuple(fk_preservation),
        policy_validation=tuple(policy_validation),
        pass_count=pass_count,
        warning_count=warning_count,
        fail_count=fail_count,
        error_count=error_count,
        pass_failed_with=pass_failed_with,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    return _report_to_dict(report)


def _suite_facts(
    source_tables: Mapping[str, TableBatches],
    output_tables: Mapping[str, TableBatches],
    *,
    scratch: Path,
    memory_limit: str,
) -> tuple[dict[str, dict[str, ColumnFacts]], dict[str, str]]:
    """Facts for every output column, per output table, against its source when it has one, and
    the class name of the error for each table whose facts could not be computed."""
    facts: dict[str, dict[str, ColumnFacts]] = {}
    failed: dict[str, str] = {}
    for name, table in output_tables.items():
        try:
            facts[name] = column_facts(
                source_tables.get(name),
                table,
                table.schema.names,
                scratch=scratch,
                memory_limit=memory_limit,
            )
        except Exception as exc:
            if passes_through(exc):
                raise
            failed[name] = type(exc).__name__
    return facts, failed


def _report_to_dict(report: PostMaskReport) -> dict[str, Any]:
    """Convert the dataclass tree to a JSON-serializable dict.

    asdict() walks frozen dataclasses cleanly. We assemble the top
    level by hand so the field ordering matches the schema and so
    the platform persistence layer can rely on stable key names.
    """
    return {
        "schema_version": report.schema_version,
        "generated_at": report.generated_at,
        "residual_pii": [asdict(f) for f in report.residual_pii],
        "fk_preservation": [asdict(f) for f in report.fk_preservation],
        "policy_validation": [asdict(f) for f in report.policy_validation],
        "pass_count": report.pass_count,
        "warning_count": report.warning_count,
        "fail_count": report.fail_count,
        "error_count": report.error_count,
        "pass_failed_with": report.pass_failed_with,
    }
