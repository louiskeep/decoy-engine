"""The ``platform`` and ``safe_output`` reason namespaces (F31 E0c).

The identity list is a closed contract: codes become permanent at the release, so the test spells
every code as a literal and compares it with the registered set, no extra and none missing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from packaging.version import Version

from decoy_engine import reasons
from decoy_engine.reasons import Kind, Outcome, Phase, RetryClass, Severity

V = "1.5.0"
PIN = Path(__file__).resolve().parents[2] / "fixtures" / "f29" / "reason-registry-release.json"

JOB_LOG = (
    "job_narrative",
    "job_succeeded",
    "job_failed",
    "job_cancelled",
    "config_rejected",
    "config_too_large",
    "variable_unresolved",
    "v1_config_rejected",
    "pipeline_error",
    "dispatch_aborted",
    "config_changed_after_claim",
    "worker_lost",
    "worker_shutdown",
    "launch_refused",
    "worker_exit_unlanded",
    "lease_recovered",
    "host_safety_rejected",
    "host_safety_deferred",
    "dispatch_mode_unknown",
    "admission_rejected",
    "owner_missing",
    "owner_inactive",
    "owner_refused",
    "legacy_binding_cancelled",
    "iteration_no_pipeline",
    "iteration_failed",
    "iteration_no_values",
    "iteration_child_failed",
    "iteration_child_cancelled",
    "startup_requeued",
    "startup_failed",
    "scan_upload_failed",
    "scan_upload_cancelled",
    "scan_disabled",
    "workflow_restart_interrupted",
    "workflow_step_not_run",
    "quality_hook_failed",
    "quality_hook_skipped",
    "quality_hook_note",
    "synth_hook_failed",
    "synth_hook_skipped",
    "synth_hook_note",
    "post_scan_hook_failed",
    "post_scan_hook_skipped",
    "post_scan_hook_note",
    "iteration_child_target_locked",
)
RESPONSE = (
    "validation_preflight_failed",
    "workflow_step_failed",
    "iteration_preview_failed",
    "database_unavailable",
    "preview_unavailable",
    "retrieval_unavailable",
    "retrieval_failed",
)
NOTIFICATION = ("notify_test_fire", "notify_node_flagged", "notify_flag_gate")
VALIDATION_CODES = (
    "yaml_empty",
    "yaml_unparseable",
    "yaml_not_a_mapping",
    "legacy_graph_mode_retired",
    "not_a_v2_config",
    "mask_secret_ref_not_allowed",
    "fixed_width_layout_ref_missing",
    "fixed_width_layout_ref_invalid",
    "fixed_width_layout_not_found",
    "fixed_width_layout_corrupt",
    "geo_lat_lng_requires_h3",
    "v2_config_invalid",
)
VALIDATION = ("validation_failed", *VALIDATION_CODES)
OWNERS = {
    "PlatformJobLog": JOB_LOG,
    "PlatformResponse": RESPONSE,
    "PlatformNotification": NOTIFICATION,
    "PlatformValidation": VALIDATION,
}
SAFE_OUTPUT = ("invalid", "legacy_withheld")

# Rows the table marks "info" (kind control, severity info) with their outcome, and the warnings.
INFO = {
    "job_narrative": Outcome.EXECUTION,
    "job_succeeded": Outcome.SUCCESS,
    "job_cancelled": Outcome.CANCELLED,
    "iteration_child_cancelled": Outcome.CANCELLED,
    "startup_requeued": Outcome.EXECUTION,
    "scan_upload_cancelled": Outcome.CANCELLED,
    "workflow_step_not_run": Outcome.CANCELLED,
    "quality_hook_note": Outcome.EXECUTION,
    "synth_hook_note": Outcome.EXECUTION,
    "post_scan_hook_note": Outcome.EXECUTION,
    "notify_test_fire": Outcome.SUCCESS,
    "notify_flag_gate": Outcome.EXECUTION,
}
WARNINGS = (
    "quality_hook_skipped",
    "synth_hook_skipped",
    "post_scan_hook_skipped",
    "notify_node_flagged",
)


def _new_specs() -> list[reasons.ReasonSpec]:
    return [s for s in reasons.specs() if s.namespace in ("platform", "safe_output")]


def test_every_platform_code_declared_since_floor() -> None:
    new = _new_specs()
    assert {"platform", "safe_output"} <= set(reasons.namespaces())
    assert len(new) == 71
    assert [s.identity for s in new if s.since != V] == []


def test_identity_set_is_exact() -> None:
    platform = {s.code: s for s in reasons.specs() if s.namespace == "platform"}
    expected = [c for codes in OWNERS.values() for c in codes]
    assert len(expected) == len(set(expected)) == 69
    assert set(platform) == set(expected)
    assert {s.code for s in reasons.specs() if s.namespace == "safe_output"} == set(SAFE_OUTPUT)
    for owner, codes in OWNERS.items():
        assert {c for c, s in platform.items() if s.owner == owner} == set(codes), owner
    for code, spec in platform.items():
        (rule,) = spec.rules
        if code in INFO:
            assert (spec.kind, rule.severity, rule.outcome) == (
                Kind.CONTROL,
                Severity.INFO,
                INFO[code],
            ), code
        elif code in WARNINGS:
            assert (spec.kind, rule.severity) == (Kind.WARNING, Severity.WARNING), code
        else:
            assert (spec.kind, rule.severity) == (Kind.FAILURE, Severity.ERROR), code
        if code == "legacy_binding_cancelled":
            assert rule.outcome is Outcome.CANCELLED


def test_safe_output_profile() -> None:
    invalid = reasons.get("safe_output", "invalid")
    withheld = reasons.get("safe_output", "legacy_withheld")
    for spec, severity in ((invalid, Severity.ERROR), (withheld, Severity.WARNING)):
        (rule,) = spec.rules
        assert spec.kind is Kind.CONTROL and spec.owner == "SafeOutput"
        assert (rule.phase, rule.severity, rule.outcome, rule.retry) == (
            Phase.UNKNOWN,
            severity,
            Outcome.EXECUTION,
            RetryClass.NEVER,
        )
    assert reasons.safe_output.INVALID == "invalid"
    assert reasons.safe_output.LEGACY_WITHHELD == "legacy_withheld"


@pytest.mark.parametrize("code", VALIDATION_CODES)
def test_validation_codes_match_platform(code: str) -> None:
    spec = reasons.find("platform", code)
    assert spec is not None and spec.owner == "PlatformValidation"
    (rule,) = spec.rules
    assert (spec.kind, rule.phase, rule.severity, rule.outcome, rule.retry) == (
        Kind.FAILURE,
        Phase.CONFIGURATION,
        Severity.ERROR,
        Outcome.CONFIGURATION,
        RetryClass.CONFIGURATION_REPAIR,
    )


def test_summaries_are_value_free() -> None:
    new = _new_specs()
    assert new
    forbidden = ("{", "}", "%", "\\", "://", "@")
    for spec in new:
        summary = spec.summary
        assert summary and summary.isascii() and len(summary) <= 400, spec.identity
        assert [f for f in forbidden if f in summary] == [], spec.identity


def test_registry_version_is_at_least_the_floor() -> None:
    # A later registry bump must not turn this red: only the floor and the pin ordering are fixed.
    # A release re-captures the pin at the live version, so equal is allowed.
    pin = json.loads(PIN.read_text())
    current = Version(reasons.REGISTRY_VERSION)
    assert current >= Version(V)
    assert current >= Version(pin["registry_version"])
