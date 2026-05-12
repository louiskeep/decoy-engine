"""FORECAST recommender — `recommend(profile: StormProfile) -> ForecastReport`.

Pure function. Reads only the StormProfile (a JSON-serializable summary),
never raw data. The signature is enforced by `tests/unit/test_forecast_security_boundary.py`.

What it produces:
  - Ranked Disguise recommendations (top one is the "Apply Disguise" CTA)
  - Per-field Mask recommendations for any PII-flagged column
  - Risk flags lifted from STORM's sentinels (with FORECAST-suggested fixes)
  - A draft pipeline YAML the user can copy/edit/run as-is
"""

from __future__ import annotations

from typing import Any, Optional

import yaml

from decoy_engine.context import ExecutionContext, emit_lineage, emit_step
from decoy_engine.disguises import Disguise, load_disguises
from decoy_engine.disguises.schema import FieldRule
from decoy_engine.forecast.transform_metadata import best_transform_for
from decoy_engine.forecast.types import (
    DisguiseRecommendation,
    FieldRecommendation,
    ForecastReport,
    RiskFlag,
)
from decoy_engine.storm.types import FieldStats, SentinelFlag, StormProfile

# Loaded once at import time — small, deterministic, fine to module-cache.
_DISGUISES: list[Disguise] = load_disguises()


# ── public API ────────────────────────────────────────────────────────────────

def recommend(
    profile: StormProfile,
    ctx: Optional[ExecutionContext] = None,
) -> ForecastReport:
    """Generate a ForecastReport for the given StormProfile.

    The signature deliberately accepts only the profile + an optional
    side-channel context — see the module docstring + the matching
    security-boundary test for the FORECAST-never-sees-raw-data rationale.

    ``ctx`` (Item 71) routes structured events through the caller's
    JobLogger so a standalone FORECAST run shows up in the bottom-pane
    SSE stream + step timeline alongside masking jobs. None preserves
    the pure-function behavior for CLI / test callers.
    """
    logger = ctx.logger if ctx is not None else None
    emit_lineage(logger, "source", profile.source_label, "storm_profile")
    emit_step(logger, "forecast.recommend", status="start")
    try:
        field_recs = _per_field_recommendations(profile.fields)
        disguise_recs = _rank_disguises(profile, field_recs)
        risk_flags = _surface_risk_flags(profile.fields)
        yaml_text = _draft_pipeline_yaml(profile, disguise_recs, field_recs)

        report = ForecastReport(
            profile_source=profile.source_label,
            disguise_recommendations=disguise_recs,
            field_recommendations=field_recs,
            risk_flags=risk_flags,
            proposed_pipeline_yaml=yaml_text,
        )
    except Exception as exc:  # noqa: BLE001 — re-raised below
        emit_step(
            logger, "forecast.recommend", status="error",
            error_class=type(exc).__name__, error_msg=str(exc),
        )
        raise
    emit_step(
        logger, "forecast.recommend", status="finish",
        rows_in=len(profile.fields), rows_out=len(disguise_recs),
    )
    return report


# ── per-field recommendations ─────────────────────────────────────────────────

def _per_field_recommendations(fields: list[FieldStats]) -> list[FieldRecommendation]:
    """For each field with a detector hit above the cutoff, recommend a Mask."""
    out: list[FieldRecommendation] = []
    for f in fields:
        if not f.detector_matches:
            continue
        top = f.detector_matches[0]
        choice = best_transform_for(top.detector_id)
        if choice is None:
            continue
        mask, params, why = choice
        out.append(FieldRecommendation(
            field_name=f.name,
            recommended_mask=mask,
            mask_params=params,
            confidence=top.match_rate,
            why=why,
            matched_detector=top.detector_id,
        ))
    return out


# ── Disguise ranking ──────────────────────────────────────────────────────────

def _detector_set(profile: StormProfile) -> set[str]:
    """All detector ids that fired anywhere in the profile."""
    return {m.detector_id for f in profile.fields for m in f.detector_matches}


def _rank_disguises(
    profile: StormProfile,
    field_recs: list[FieldRecommendation],
) -> list[DisguiseRecommendation]:
    detector_set = _detector_set(profile)
    quasi_id_groups = profile.quasi_identifier_groups

    ranked: list[DisguiseRecommendation] = []
    for d in _DISGUISES:
        eligible, score = _score_disguise(d, profile, detector_set, quasi_id_groups)
        if not eligible or score < d.triggers.min_score:
            continue
        matched_fields, apply_payload = _build_apply_payload(d, profile)
        ranked.append(DisguiseRecommendation(
            disguise_id=d.id,
            name=d.name,
            summary=d.summary,
            regulation=d.regulation,
            match_score=round(min(score, 1.0), 3),
            matched_fields=matched_fields,
            reasoning=_disguise_reasoning(d, detector_set, quasi_id_groups),
            apply_payload=apply_payload,
        ))
    ranked.sort(key=lambda r: r.match_score, reverse=True)
    return ranked


def _score_disguise(
    d: Disguise,
    profile: StormProfile,
    detector_set: set[str],
    quasi_id_groups: list[list[str]],
) -> tuple[bool, float]:
    """Evaluate eligibility + score. Returns (eligible, score)."""
    # Required detectors must all be present.
    if d.triggers.required_detectors and not all(
        det in detector_set for det in d.triggers.required_detectors
    ):
        return False, 0.0
    # If any_detectors is set, at least one must fire.
    if d.triggers.any_detectors and not any(
        det in detector_set for det in d.triggers.any_detectors
    ):
        return False, 0.0

    # Weights tuned so a domain-specific Disguise (HIPAA, PCI, ...) outscores
    # the generic `default` Disguise when its key detectors fire. The math
    # also stays comfortably below 1.0 in normal cases so the cap isn't load-
    # bearing.
    score = 0.4  # eligible-but-undistinguished baseline

    # +0.05 per any_detector hit.
    score += 0.05 * sum(1 for det in d.triggers.any_detectors if det in detector_set)

    # +0.15 per fully-matched co-occurrence group (e.g. HIPAA's quasi-id trio).
    score += 0.15 * sum(
        1 for group in d.triggers.co_occurrence
        if all(det in detector_set for det in group)
    )

    # +0.05 per field rule whose detectors appear in the profile (capped at +0.2).
    field_rule_hits = sum(
        1 for rule in d.field_rules
        if any(det in detector_set for det in rule.detectors)
    )
    score += min(0.2, 0.05 * field_rule_hits)

    # +0.05 if the profile flagged any quasi-identifier group at all.
    if quasi_id_groups:
        score += 0.05

    # +0.05 tiebreaker when this Disguise has a regulation tag — keeps HIPAA /
    # PCI / GDPR from being shadowed by the generic default Disguise on data
    # where both apply.
    if d.regulation:
        score += 0.05

    return True, score


def _disguise_reasoning(
    d: Disguise,
    detector_set: set[str],
    quasi_id_groups: list[list[str]],
) -> str:
    """One-sentence human-facing explanation of why this Disguise was picked."""
    parts: list[str] = []
    triggered_any = [det for det in d.triggers.any_detectors if det in detector_set]
    if triggered_any:
        parts.append(", ".join(triggered_any) + " present")
    triggered_co = [
        group for group in d.triggers.co_occurrence
        if all(det in detector_set for det in group)
    ]
    if triggered_co:
        parts.append("co-occurrence (" + ", ".join(triggered_co[0]) + ")")
    if quasi_id_groups:
        parts.append("re-identification quasi-identifier group flagged")
    if not parts:
        return f"matched {d.name}'s baseline triggers"
    return " + ".join(parts) + f" -> recommends {d.name}"


# ── apply payload builder ─────────────────────────────────────────────────────

def _build_apply_payload(
    d: Disguise,
    profile: StormProfile,
) -> tuple[list[str], dict[str, Any]]:
    """Walk the Disguise's field_rules, match them against profile columns by
    detector hits, and build the `apply_payload` the UI consumes when the user
    clicks "Apply Disguise". Also returns the list of column names covered.
    """
    matched_fields: list[str] = []
    field_masks: list[dict[str, Any]] = []

    for f in profile.fields:
        rule = _first_rule_matching_field(f, d.field_rules)
        if rule is None:
            continue
        matched_fields.append(f.name)
        # Build the masking_rules entry: {column, type, ...params}
        entry: dict[str, Any] = {"column": f.name, "type": rule.mask}
        entry.update(rule.params)
        if rule.why:
            entry["_why"] = rule.why  # Hint for the UI; engine ignores _-prefixed keys.
        field_masks.append(entry)

    return matched_fields, {
        "disguise_id": d.id,
        "field_masks": field_masks,
    }


def _first_rule_matching_field(f: FieldStats, rules: list[FieldRule]) -> FieldRule | None:
    """A field matches a rule if any of the rule's detectors fired on the field."""
    field_detector_ids = {m.detector_id for m in f.detector_matches}
    for rule in rules:
        if any(det in field_detector_ids for det in rule.detectors):
            return rule
    return None


# ── risk flags from sentinels ─────────────────────────────────────────────────

# Suggested fixes per sentinel kind. UI shows these to the user.
_FIX_OPTIONS: dict[str, list[str]] = {
    "date_sentinel": [
        "Replace with NULL (most conservative)",
        "Drop rows containing the sentinel",
        "Replace with a cohort-stratified random date",
    ],
    "date_out_of_range": [
        "Replace with NULL",
        "Drop the row",
        "Clamp to the nearest plausible date",
    ],
    "numeric_sentinel": [
        "Replace with NULL",
        "Drop the row",
        "Replace with the column median",
    ],
    "string_sentinel": [
        "Replace with NULL",
        "Replace with a synthetic placeholder",
        "Drop the row",
    ],
}


def _surface_risk_flags(fields: list[FieldStats]) -> list[RiskFlag]:
    out: list[RiskFlag] = []
    for f in fields:
        for s in f.sentinels:
            out.append(RiskFlag(
                field_name=f.name,
                kind=s.kind,
                value=s.value,
                note=s.note,
                fix_options=list(_FIX_OPTIONS.get(s.kind, [])),
            ))
    return out


# ── proposed pipeline YAML ────────────────────────────────────────────────────

def _draft_pipeline_yaml(
    profile: StormProfile,
    disguise_recs: list[DisguiseRecommendation],
    field_recs: list[FieldRecommendation],
) -> str:
    """Render a ready-to-edit pipeline config string.

    Uses the top-ranked Disguise's masking rules if any Disguise was
    recommended; otherwise falls back to per-field recommendations. Source
    and output sections are stubbed with the profile's source_label so the
    user knows which dataset this pipeline targets, but they need to fill
    in actual paths/connectors.
    """
    if disguise_recs:
        masking_rules = [
            _strip_underscored(entry)
            for entry in disguise_recs[0].apply_payload["field_masks"]
        ]
    else:
        masking_rules = [
            {"column": r.field_name, "type": r.recommended_mask, **r.mask_params}
            for r in field_recs
        ]

    config = {
        "version": "1.0",
        "global_settings": {"seed": 42},
        "input": {
            "type": "csv",
            "path": f"# TODO: path to {profile.source_label}",
            "csv_options": {"delimiter": ",", "encoding": "utf-8", "header": True},
        },
        "output": {
            "type": "csv",
            "path": f"# TODO: path for masked output (was: {profile.source_label})",
            "csv_options": {"delimiter": ",", "encoding": "utf-8"},
        },
        "masking_rules": masking_rules,
    }
    return yaml.safe_dump(config, sort_keys=False, allow_unicode=True)


def _strip_underscored(d: dict[str, Any]) -> dict[str, Any]:
    """Remove `_why`-style hint keys before rendering the user-facing YAML."""
    return {k: v for k, v in d.items() if not k.startswith("_")}
