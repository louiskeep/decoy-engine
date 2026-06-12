"""Storm post-mask runner -- assembles the report (Reframe-A).

Public entry: ``run_storm_post_mask``. Called by the platform's
post_mask_hook AFTER a successful mask job lands its output. Pure
function: takes source + output frames + config dict, returns a
JSON-serializable storm-report dict.

Best-effort: each check category runs inside its own try/except so a
broken check doesn't kill the whole pass. A check that raises is
recorded as a single ``error``-severity finding with the exception
type name; the other checks still run + produce their findings.

The catastrophic-failure case (the runner itself can't even start, e.g.
``output_frames`` is the wrong type) raises TypeError. The platform
hook catches that + writes a JobStormReport with
``pass_failed_with: "TypeError"`` so the FE has something to render.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd

from decoy_engine.storm.postmask.fk_preservation import check_fk_preservation
from decoy_engine.storm.postmask.policy_validation import check_policy_validation
from decoy_engine.storm.postmask.residual_pii import check_residual_pii
from decoy_engine.storm.postmask.types import (
    SCHEMA_VERSION,
    FKPreservationFinding,
    PolicyValidationFinding,
    ResidualPIIFinding,
    StormPostMaskReport,
)


def run_storm_post_mask(
    source_frames: dict[str, pd.DataFrame],
    output_frames: dict[str, pd.DataFrame],
    *,
    config: dict[str, Any],
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

    Returns:
        A storm-post-mask/v1 report dict. JSON-serializable (every
        field is a primitive or list of dataclass-asdict'd findings).
        Caller persists this to JobStormReport.report_json.

    Raises:
        TypeError: if the frame dicts are not actually dicts of
            DataFrames or config is not a dict. Programming errors;
            the platform hook converts these into a single error-row
            report so the FE has something to show.
    """
    if not isinstance(source_frames, dict) or not isinstance(output_frames, dict):
        raise TypeError(
            "run_storm_post_mask: source_frames and output_frames must be dicts "
            f"of pandas DataFrames, got {type(source_frames).__name__} and "
            f"{type(output_frames).__name__}"
        )
    if not isinstance(config, dict):
        raise TypeError(f"run_storm_post_mask: config must be a dict, got {type(config).__name__}")

    residual_pii: list[ResidualPIIFinding] = []
    fk_preservation: list[FKPreservationFinding] = []
    policy_validation: list[PolicyValidationFinding] = []
    pass_failed_with: str | None = None

    # Each check runs in its own try/except so one bad check doesn't
    # kill the pass. Failures are recorded as error-severity findings.
    # Dennis H3 fix (2026-06-01): the exception message previously
    # included `{exc}` which surfaced raw exception text into the
    # report payload + the FE Storm tab. An OSError on a missing path
    # could leak `/home/user/.ssh/...`; a KeyError could expose a
    # column name an operator didn't expect to see in a UI-rendered
    # report. The typed name (`{type(exc).__name__}`) is sufficient
    # for the operator to find the underlying error in the job log,
    # where the full exception text already lives at the catch site.
    try:
        residual_pii = check_residual_pii(output_frames, config, source_frames=source_frames)
    except Exception as exc:
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
        fk_preservation = check_fk_preservation(output_frames, config)
    except Exception as exc:
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
        policy_validation = check_policy_validation(source_frames, output_frames, config)
    except Exception as exc:
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
    pass_count = 0
    warning_count = 0
    fail_count = 0
    error_count = 0
    for finding in (*residual_pii, *fk_preservation, *policy_validation):
        sev = getattr(finding, "severity", "info")
        if sev == "info":
            pass_count += 1
        elif sev == "warning":
            warning_count += 1
        elif sev == "fail":
            fail_count += 1
        elif sev == "error":
            error_count += 1

    # Dennis M23 fix (2026-06-01): stamp generated_at at report
    # construction so the JobStormReport row column + the engine
    # payload agree. Module-level import would force a stdlib pull at
    # every engine import; local for now.
    from datetime import datetime, timezone

    report = StormPostMaskReport(
        schema_version=SCHEMA_VERSION,
        residual_pii=residual_pii,
        fk_preservation=fk_preservation,
        policy_validation=policy_validation,
        pass_count=pass_count,
        warning_count=warning_count,
        fail_count=fail_count,
        error_count=error_count,
        pass_failed_with=pass_failed_with,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    return _report_to_dict(report)


def _report_to_dict(report: StormPostMaskReport) -> dict[str, Any]:
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
