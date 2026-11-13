"""Model findings for the Post-scan residual-PII check: the classifier and NER passes, the model
severity table, dismissals and the `ner_findings: fail` promotion. Whether a model row withholds
the outputs is decided by the one verdict (`_blocked.post_scan_verdict`), not here.

Pattern: a finding names the detector that produced it, its source and the model version that
ran, with a rate or a band, never the value (Google Cloud DLP findings carry an info type, a
likelihood and the detector; AWS Macie findings the same). Detectors of three sources report
through one list, as Microsoft Presidio's recognizer registry does for pattern, NER and ML
recognizers. The NER rate is the share of scanned cells holding a span of a detector id
(`ner_rates`, the one NER scoring entry, with the free-text gate inside it; AWS Glue's
`ThresholdFraction` has the same shape), and a classifier label carries the band of the strictly
loaded pack (`classify_frame`, the one classifier inference).

Model findings never pass through `residual_pii._classify` or `_escalate`: those read a regex
detector confidence and a positional identity rate, which a model finding does not have. Each
column with at least one hit gets one row per detector id; severity comes from `_ner_severity`
and `_classifier_severity`. Order (owner decision Q6): warn-mode severity, then dismissals (a
matching `warning` becomes `info` with `dismissed`), then, under `ner_findings: fail`, each NER
`warning` still standing becomes `fail` with `promoted`. A dismissal never lowers a `fail`, an
`error` or a source-identity finding.

Each model pass runs in its own `try`: a coded engine error propagates unchanged
(`passes_through`, the rule the regex checks and pre-scan apply), and any other exception adds one
`error` row naming the source, the model and the exception class, and the regex rows stay. Rows,
log lines and errors carry codes, counts, buckets and bands only.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

import pandas as pd

from decoy_engine.config import post_scan_integrity_requested
from decoy_engine.detection._model_status import log_detector_models, log_detector_run
from decoy_engine.detection._ner_rate import ner_rates
from decoy_engine.detection.ner import DEFAULT_NER_MODEL, pipeline_version
from decoy_engine.detection.types import DetectorSource, ModelRef
from decoy_engine.profiling import passes_through
from decoy_engine.profiling.model_pack import DetectorSet, classify_frame, load_detectors
from decoy_engine.verification.postmask.residual_pii import (
    _NO_OP_BY_DESIGN,
    _PRODUCES_PII_LIKE_VALUES,
    _REUSES_SOURCE_VALUES,
    configured_columns,
    is_source_identity_finding,
)
from decoy_engine.verification.postmask.types import ResidualPIIFinding, Severity

# Strategies that keep the value or emit a realistic one by design: a model hit there is expected.
_EXPECTED_HIT = _NO_OP_BY_DESIGN | _PRODUCES_PII_LIKE_VALUES | _REUSES_SOURCE_VALUES
_NER_TEXT_STRATEGIES = frozenset({"text_redact", "text_mask"})
_CLASSIFIER_BANDS = frozenset({"high", "review"})

__all__ = [
    "model_findings",
    "ner_findings_mode",
    "post_scan_detectors",
    "review",
]


def ner_findings_mode(config: Mapping[str, Any]) -> str:
    """The run's `post_scan.ner_findings` mode, `warn` when unset."""
    block = config.get("post_scan")
    return str(block.get("ner_findings") or "warn") if isinstance(block, Mapping) else "warn"


def post_scan_detectors(
    config: Mapping[str, Any], *, file_access: Any = None
) -> DetectorSet | None:
    """The run's post-scan detector set, strictly loaded; None when the run has no post-scan.

    Called at run start, so a missing model refuses the run before any row is masked.
    """
    if not post_scan_integrity_requested(config):
        return None
    block = config["post_scan"]
    sources = tuple(block.get("detectors") or ("regex",))
    ner_model = block.get("ner_model") or DEFAULT_NER_MODEL
    return load_detectors(sources, ner_model=ner_model, file_access=file_access)


@dataclass
class _Pass:
    """One model pass: its findings so far and the table it was reading."""

    source: DetectorSource
    model: ModelRef | None
    table: str = ""
    findings: list[ResidualPIIFinding] = field(default_factory=list)


def _strategy(entry: Mapping[str, Any] | None) -> str | None:
    return entry.get("strategy") if entry is not None else None


def _ner_on(entry: Mapping[str, Any] | None) -> bool:
    options = entry.get("provider_config") if entry is not None else None
    return isinstance(options, Mapping) and bool(options.get("ner"))


def _ner_severity(entry: Mapping[str, Any] | None, hit: bool) -> Severity:
    if not hit or _strategy(entry) in _EXPECTED_HIT:
        return "info"
    return "warning"


def _classifier_severity(entry: Mapping[str, Any] | None) -> Severity:
    strategy = _strategy(entry)
    if strategy in _EXPECTED_HIT or (strategy in _NER_TEXT_STRATEGIES and _ner_on(entry)):
        return "info"
    return "warning"


def _is_text(series: pd.Series) -> bool:
    return pd.api.types.is_object_dtype(series) or pd.api.types.is_string_dtype(series)


def _ner_pass(
    run: _Pass,
    output_frames: Mapping[str, pd.DataFrame],
    columns: Mapping[tuple[str, str], Mapping[str, Any]],
    ref: ModelRef,
) -> None:
    version: str | None = None
    for table, frame in output_frames.items():
        run.table = str(table)
        for column in frame.columns:
            if not _is_text(frame[column]):
                continue
            for detector_id, rate in sorted(ner_rates(frame[column], model=ref.id).items()):
                if rate.hit_cells == 0:
                    continue
                # The version of the pipeline that scored, not the one the run start saw.
                version = version or pipeline_version(ref.id)
                entry = columns.get((run.table, str(column)))
                run.findings.append(
                    ResidualPIIFinding(
                        table=run.table,
                        column=str(column),
                        detector_id=detector_id,
                        match_rate=rate.rate,
                        severity=_ner_severity(entry, rate.hit),
                        configured_strategy=_strategy(entry),
                        sample_match_count=rate.hit_cells,
                        message=f"ner found {detector_id} in {rate.bucket} of the scanned cells.",
                        source="ner",
                        model=ModelRef(ref.id, version),
                    )
                )


def _classifier_pass(
    run: _Pass,
    output_frames: Mapping[str, pd.DataFrame],
    columns: Mapping[tuple[str, str], Mapping[str, Any]],
    pack: dict[str, Any],
) -> None:
    for table, frame in output_frames.items():
        run.table = str(table)
        for column, hit in classify_frame(pack, frame, subsample=True).items():
            if hit["label"] is None or hit["band"] not in _CLASSIFIER_BANDS:
                continue
            entry = columns.get((run.table, column))
            run.findings.append(
                ResidualPIIFinding(
                    table=run.table,
                    column=column,
                    detector_id=str(hit["label"]),
                    match_rate=None,
                    severity=_classifier_severity(entry),
                    configured_strategy=_strategy(entry),
                    message=f"classifier labelled the column {hit['label']} ({hit['band']} band).",
                    source="classifier",
                    model=ModelRef(str(hit["model_pack_id"]), str(hit["model_pack_version"])),
                    band=str(hit["band"]),
                )
            )


def model_findings(
    output_frames: Mapping[str, pd.DataFrame], config: dict[str, Any], detectors: DetectorSet
) -> list[ResidualPIIFinding]:
    """The classifier and NER findings over every output table, at warn-mode severity."""
    columns = configured_columns(config)
    pack, ner = detectors.classifier_pack, detectors.ner
    out: list[ResidualPIIFinding] = []
    for source in detectors.sources:
        if source == "classifier" and pack is not None:
            run = _Pass("classifier", detectors.classifier)
        elif source == "ner" and ner is not None:
            run = _Pass("ner", ner)
        else:
            continue
        failed: str | None = None
        try:
            if pack is not None and run.source == "classifier":
                _classifier_pass(run, output_frames, columns, pack)
            elif ner is not None:
                _ner_pass(run, output_frames, columns, ner)
        except Exception as exc:
            if passes_through(exc):
                raise
            failed = type(exc).__name__
        out.extend(run.findings)
        if failed is not None:
            out.append(
                ResidualPIIFinding(
                    table=run.table,
                    column="",
                    detector_id="",
                    match_rate=None if run.source == "classifier" else 0.0,
                    severity="error",
                    message=f"{run.source} pass raised {failed} (see job log for details)",
                    source=run.source,
                    model=run.model,
                )
            )
    return out


def _model_id(finding: ResidualPIIFinding) -> tuple[str | None, str | None]:
    return (finding.model.id, finding.model.version) if finding.model else (None, None)


def _matches(dismissal: Mapping[str, Any], finding: ResidualPIIFinding) -> bool:
    return (
        dismissal.get("table") == finding.table
        and dismissal.get("column") == finding.column
        and dismissal.get("detector_id") == finding.detector_id
        and dismissal.get("source") == finding.source
        and (dismissal.get("model_id"), dismissal.get("model_version")) == _model_id(finding)
        and dismissal.get("strategy") == finding.configured_strategy
    )


def review(
    findings: list[ResidualPIIFinding], config: Mapping[str, Any]
) -> tuple[list[ResidualPIIFinding], Counter[str]]:
    """Apply dismissals, then the `ner_findings: fail` promotion; also count stale dismissals.

    Returns the findings and the dismissals that matched nothing, counted by their table.
    """
    block = config.get("post_scan")
    block = block if isinstance(block, Mapping) else {}
    dismissals = [d for d in block.get("dismissed") or () if isinstance(d, Mapping)]
    promote = ner_findings_mode(config) == "fail"
    used: set[int] = set()
    out: list[ResidualPIIFinding] = []
    for finding in findings:
        if finding.severity == "warning" and not is_source_identity_finding(finding):
            hits = [i for i, d in enumerate(dismissals) if _matches(d, finding)]
            if hits:
                used.update(hits)
                finding = replace(finding, severity="info", dismissed=True)
        if promote and finding.source == "ner" and finding.severity == "warning":
            finding = replace(finding, severity="fail", promoted=True)
        out.append(finding)
    stale = Counter(str(d.get("table")) for i, d in enumerate(dismissals) if i not in used)
    return out, stale


def log_post_scan(
    output_frames: Mapping[str, pd.DataFrame],
    config: Mapping[str, Any],
    detectors: DetectorSet,
    findings: list[ResidualPIIFinding],
    stale: Counter[str],
) -> None:
    """The `post_scan.detectors` line, then one `post_scan.models` line per checked table."""
    mode = ner_findings_mode(config)
    columns = sum(len(frame.columns) for frame in output_frames.values())
    counts: dict[str, int | str] = {
        "tables": len(output_frames),
        "columns": columns,
        "ner_findings": str(mode),
    }
    log_detector_run("post_scan", detectors.runs(), counts)
    tables = [str(t) for t in output_frames]
    tables += [t for t in stale if t not in tables]
    for position, table in enumerate(tables, start=1):
        rows = [f for f in findings if f.table == table]
        log_detector_models(
            "post_scan",
            table,
            position,
            {
                "ner_findings": sum(f.source == "ner" for f in rows),
                "classifier_findings": sum(f.source == "classifier" for f in rows),
                "dismissed": sum(f.dismissed for f in rows),
                "dismissed_stale": stale.get(table, 0),
            },
        )
