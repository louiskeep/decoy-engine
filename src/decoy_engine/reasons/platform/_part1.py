"""Catalog of the ``platform`` namespace. Added at registry 1.5.0."""

from __future__ import annotations

from decoy_engine.reasons import _catalog
from decoy_engine.reasons._types import Kind, Outcome, Severity

__all__ = [
    "ADMISSION_REJECTED",
    "CONFIG_CHANGED_AFTER_CLAIM",
    "CONFIG_REJECTED",
    "CONFIG_TOO_LARGE",
    "DATABASE_UNAVAILABLE",
    "DISPATCH_ABORTED",
    "DISPATCH_MODE_UNKNOWN",
    "FIXED_WIDTH_LAYOUT_CORRUPT",
    "FIXED_WIDTH_LAYOUT_NOT_FOUND",
    "FIXED_WIDTH_LAYOUT_REF_INVALID",
    "FIXED_WIDTH_LAYOUT_REF_MISSING",
    "GEO_LAT_LNG_REQUIRES_H3",
    "HOST_SAFETY_DEFERRED",
    "HOST_SAFETY_REJECTED",
    "ITERATION_CHILD_CANCELLED",
    "ITERATION_CHILD_FAILED",
    "ITERATION_CHILD_TARGET_LOCKED",
    "ITERATION_FAILED",
    "ITERATION_NO_PIPELINE",
    "ITERATION_NO_VALUES",
    "ITERATION_PREVIEW_FAILED",
    "JOB_CANCELLED",
    "JOB_FAILED",
    "JOB_NARRATIVE",
    "JOB_SUCCEEDED",
    "LAUNCH_REFUSED",
    "LEASE_RECOVERED",
    "LEGACY_BINDING_CANCELLED",
    "LEGACY_GRAPH_MODE_RETIRED",
    "MASK_SECRET_REF_NOT_ALLOWED",
    "NOTIFY_FLAG_GATE",
    "NOTIFY_NODE_FLAGGED",
    "NOTIFY_TEST_FIRE",
    "NOT_A_V2_CONFIG",
    "OWNER_INACTIVE",
    "OWNER_MISSING",
    "OWNER_REFUSED",
    "PIPELINE_ERROR",
    "POST_SCAN_HOOK_FAILED",
    "POST_SCAN_HOOK_NOTE",
    "POST_SCAN_HOOK_SKIPPED",
    "PREVIEW_UNAVAILABLE",
    "QUALITY_HOOK_FAILED",
    "QUALITY_HOOK_NOTE",
    "QUALITY_HOOK_SKIPPED",
    "RETRIEVAL_FAILED",
    "RETRIEVAL_UNAVAILABLE",
    "SCAN_DISABLED",
    "SCAN_UPLOAD_CANCELLED",
    "SCAN_UPLOAD_FAILED",
    "STARTUP_FAILED",
    "STARTUP_REQUEUED",
    "SYNTH_HOOK_FAILED",
    "SYNTH_HOOK_NOTE",
    "SYNTH_HOOK_SKIPPED",
    "V1_CONFIG_REJECTED",
    "V2_CONFIG_INVALID",
    "VALIDATION_FAILED",
    "VALIDATION_PREFLIGHT_FAILED",
    "VARIABLE_UNRESOLVED",
    "WORKER_EXIT_UNLANDED",
    "WORKER_LOST",
    "WORKER_SHUTDOWN",
    "WORKFLOW_RESTART_INTERRUPTED",
    "WORKFLOW_STEP_FAILED",
    "WORKFLOW_STEP_NOT_RUN",
    "YAML_EMPTY",
    "YAML_NOT_A_MAPPING",
    "YAML_UNPARSEABLE",
]

_d = _catalog.platform("PlatformJobLog")

JOB_NARRATIVE = _d(
    "job_narrative",
    "A job step logged a message. Its text is kept in the server log.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.EXECUTION,
    since="1.5.0",
)
JOB_SUCCEEDED = _d(
    "job_succeeded",
    "Job complete.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.SUCCESS,
    since="1.5.0",
)
JOB_FAILED = _d(
    "job_failed", "Job complete: failed. The server log has the details.", since="1.5.0"
)
JOB_CANCELLED = _d(
    "job_cancelled",
    "Job complete: cancelled.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.CANCELLED,
    since="1.5.0",
)
CONFIG_REJECTED = _d(
    "config_rejected",
    "The job configuration was rejected. The server log has the details.",
    since="1.5.0",
)
CONFIG_TOO_LARGE = _d(
    "config_too_large", "The job configuration exceeds the stored-config size limit.", since="1.5.0"
)
VARIABLE_UNRESOLVED = _d(
    "variable_unresolved",
    "A pipeline variable could not be resolved. The server log has the details.",
    since="1.5.0",
)
V1_CONFIG_REJECTED = _d(
    "v1_config_rejected",
    "This pipeline uses the retired v1 format. Re-author it in the graph builder.",
    since="1.5.0",
)
PIPELINE_ERROR = _d(
    "pipeline_error",
    "The pipeline stopped with an error. The server log has the details.",
    since="1.5.0",
)
DISPATCH_ABORTED = _d(
    "dispatch_aborted",
    "The worker stopped the job before it ran. The server log has the details.",
    since="1.5.0",
)
CONFIG_CHANGED_AFTER_CLAIM = _d(
    "config_changed_after_claim",
    "The stored job configuration changed between claim and run.",
    since="1.5.0",
)
WORKER_LOST = _d(
    "worker_lost",
    "The worker running the job stopped responding, so the job was failed.",
    since="1.5.0",
)
WORKER_SHUTDOWN = _d(
    "worker_shutdown",
    "The worker shut down while the job was running, so the job was failed.",
    since="1.5.0",
)
LAUNCH_REFUSED = _d("launch_refused", "The supervisor refused to launch the job.", since="1.5.0")
WORKER_EXIT_UNLANDED = _d(
    "worker_exit_unlanded", "The worker exited without recording a final job state.", since="1.5.0"
)
LEASE_RECOVERED = _d(
    "lease_recovered",
    "The job was failed during crash recovery after its host lease was released.",
    since="1.5.0",
)
HOST_SAFETY_REJECTED = _d(
    "host_safety_rejected",
    "A host-safety check rejected the job before any data was read.",
    since="1.5.0",
)
HOST_SAFETY_DEFERRED = _d(
    "host_safety_deferred",
    "The job waited too long for host capacity and was not run.",
    since="1.5.0",
)
DISPATCH_MODE_UNKNOWN = _d(
    "dispatch_mode_unknown", "The job mode has no registered dispatcher.", since="1.5.0"
)
ADMISSION_REJECTED = _d(
    "admission_rejected",
    "The job estimate exceeds the hard ceiling, so it was rejected before any data was read.",
    since="1.5.0",
)
OWNER_MISSING = _d(
    "owner_missing", "The job's owner no longer exists, so the job cannot run.", since="1.5.0"
)
OWNER_INACTIVE = _d(
    "owner_inactive", "The job's owner is inactive, so the job cannot run.", since="1.5.0"
)
OWNER_REFUSED = _d("owner_refused", "The job's owner cannot run jobs.", since="1.5.0")
LEGACY_BINDING_CANCELLED = _d(
    "legacy_binding_cancelled",
    "The job uses a retired cloud binding and was cancelled.",
    outcome=Outcome.CANCELLED,
    since="1.5.0",
)
ITERATION_NO_PIPELINE = _d(
    "iteration_no_pipeline", "The iteration parent job has no pipeline.", since="1.5.0"
)
ITERATION_FAILED = _d(
    "iteration_failed",
    "The iteration could not run. The server log has the details.",
    since="1.5.0",
)
ITERATION_NO_VALUES = _d(
    "iteration_no_values", "The iteration produced no values, so nothing ran.", since="1.5.0"
)
ITERATION_CHILD_FAILED = _d(
    "iteration_child_failed",
    "An iteration child job failed. The server log has the details.",
    since="1.5.0",
)
ITERATION_CHILD_CANCELLED = _d(
    "iteration_child_cancelled",
    "Cancelled: the parent job reached a final state.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.CANCELLED,
    since="1.5.0",
)
STARTUP_REQUEUED = _d(
    "startup_requeued",
    "The platform restarted while the job ran. The job was queued to run again.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.EXECUTION,
    since="1.5.0",
)
STARTUP_FAILED = _d(
    "startup_failed",
    "The platform restarted while the job ran, so the job was failed.",
    since="1.5.0",
)
SCAN_UPLOAD_FAILED = _d("scan_upload_failed", "The scan upload failed.", since="1.5.0")
SCAN_UPLOAD_CANCELLED = _d(
    "scan_upload_cancelled",
    "The scan upload was cancelled. Upload again to retry.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.CANCELLED,
    since="1.5.0",
)
SCAN_DISABLED = _d("scan_disabled", "Scanning is disabled in this release.", since="1.5.0")
WORKFLOW_RESTART_INTERRUPTED = _d(
    "workflow_restart_interrupted",
    "The platform restarted while this workflow step ran. Progress was not kept; run it again when ready.",
    since="1.5.0",
)
WORKFLOW_STEP_NOT_RUN = _d(
    "workflow_step_not_run",
    "Cancelled: the workflow run ended before this step ran.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.CANCELLED,
    since="1.5.0",
)
QUALITY_HOOK_FAILED = _d(
    "quality_hook_failed",
    "The quality check after the run failed. The server log has the details.",
    since="1.5.0",
)
QUALITY_HOOK_SKIPPED = _d(
    "quality_hook_skipped",
    "The quality check after the run was skipped.",
    kind=Kind.WARNING,
    severity=Severity.WARNING,
    since="1.5.0",
)
QUALITY_HOOK_NOTE = _d(
    "quality_hook_note",
    "The quality check after the run logged a note.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.EXECUTION,
    since="1.5.0",
)
SYNTH_HOOK_FAILED = _d(
    "synth_hook_failed",
    "The synthesis step after the run failed. The server log has the details.",
    since="1.5.0",
)
SYNTH_HOOK_SKIPPED = _d(
    "synth_hook_skipped",
    "The synthesis step after the run was skipped.",
    kind=Kind.WARNING,
    severity=Severity.WARNING,
    since="1.5.0",
)
SYNTH_HOOK_NOTE = _d(
    "synth_hook_note",
    "The synthesis step after the run logged a note.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.EXECUTION,
    since="1.5.0",
)
POST_SCAN_HOOK_FAILED = _d(
    "post_scan_hook_failed",
    "The scan after masking failed. The server log has the details.",
    since="1.5.0",
)
POST_SCAN_HOOK_SKIPPED = _d(
    "post_scan_hook_skipped",
    "The scan after masking was skipped.",
    kind=Kind.WARNING,
    severity=Severity.WARNING,
    since="1.5.0",
)
POST_SCAN_HOOK_NOTE = _d(
    "post_scan_hook_note",
    "The scan after masking logged a note.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.EXECUTION,
    since="1.5.0",
)
ITERATION_CHILD_TARGET_LOCKED = _d(
    "iteration_child_target_locked",
    "Another job holds this output target, so the iteration child did not run.",
    since="1.5.0",
)

_d = _catalog.platform("PlatformResponse")

VALIDATION_PREFLIGHT_FAILED = _d(
    "validation_preflight_failed",
    "The pipeline could not be checked. The server log has the details.",
    since="1.5.0",
)
WORKFLOW_STEP_FAILED = _d(
    "workflow_step_failed",
    "The workflow step failed. The server log has the details.",
    since="1.5.0",
)
ITERATION_PREVIEW_FAILED = _d(
    "iteration_preview_failed", "The iteration values could not be previewed.", since="1.5.0"
)
DATABASE_UNAVAILABLE = _d("database_unavailable", "The database is unavailable.", since="1.5.0")
PREVIEW_UNAVAILABLE = _d(
    "preview_unavailable", "Preview is unavailable right now. Try again later.", since="1.5.0"
)
RETRIEVAL_UNAVAILABLE = _d(
    "retrieval_unavailable",
    "Output retrieval is unavailable right now. Try again later.",
    since="1.5.0",
)
RETRIEVAL_FAILED = _d("retrieval_failed", "The output could not be read.", since="1.5.0")

_d = _catalog.platform("PlatformNotification")

NOTIFY_TEST_FIRE = _d(
    "notify_test_fire",
    "This is a test notification.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.SUCCESS,
    since="1.5.0",
)
NOTIFY_NODE_FLAGGED = _d(
    "notify_node_flagged",
    "A flagged node logged a message at or above the flag's severity.",
    kind=Kind.WARNING,
    severity=Severity.WARNING,
    since="1.5.0",
)
NOTIFY_FLAG_GATE = _d(
    "notify_flag_gate",
    "A flag gate paused the job for review.",
    kind=Kind.CONTROL,
    severity=Severity.INFO,
    outcome=Outcome.EXECUTION,
    since="1.5.0",
)

_d = _catalog.platform_config("PlatformValidation")

VALIDATION_FAILED = _d(
    "validation_failed", "The pipeline configuration is not valid.", since="1.5.0"
)
YAML_EMPTY = _d(
    "yaml_empty",
    "Pipeline YAML is empty. Re-open the pipeline in the graph builder, fix any GraphToV2Error shown at save time, then Save and try again.",
    since="1.5.0",
)
YAML_UNPARSEABLE = _d("yaml_unparseable", "The pipeline YAML could not be parsed.", since="1.5.0")
YAML_NOT_A_MAPPING = _d(
    "yaml_not_a_mapping", "The pipeline YAML must be a mapping (an object).", since="1.5.0"
)
LEGACY_GRAPH_MODE_RETIRED = _d(
    "legacy_graph_mode_retired",
    "This pipeline's YAML is graph-mode, which was retired. The V2 engine accepts only `version: 2` PipelineConfig YAML. Re-open the pipeline in the graph builder. The save seam emits the V2 config now. If the pipeline uses any of the retired ops, recreate it with the V2 narrow surface.",
    since="1.5.0",
)
NOT_A_V2_CONFIG = _d(
    "not_a_v2_config",
    "Pipeline YAML is not a v2 PipelineConfig (no `version: 2`). Re-save the pipeline from the graph builder to emit a v2 config.",
    since="1.5.0",
)
MASK_SECRET_REF_NOT_ALLOWED = _d(
    "mask_secret_ref_not_allowed",
    "global_settings.mask_secret_ref is not allowed in a platform job; the platform keys masking from its instance master key.",
    since="1.5.0",
)
FIXED_WIDTH_LAYOUT_REF_MISSING = _d(
    "fixed_width_layout_ref_missing", "A fixed-width layout reference is missing.", since="1.5.0"
)
FIXED_WIDTH_LAYOUT_REF_INVALID = _d(
    "fixed_width_layout_ref_invalid", "A fixed-width layout reference is not valid.", since="1.5.0"
)
FIXED_WIDTH_LAYOUT_NOT_FOUND = _d(
    "fixed_width_layout_not_found", "A referenced fixed-width layout was not found.", since="1.5.0"
)
FIXED_WIDTH_LAYOUT_CORRUPT = _d(
    "fixed_width_layout_corrupt", "A referenced fixed-width layout is corrupt.", since="1.5.0"
)
GEO_LAT_LNG_REQUIRES_H3 = _d(
    "geo_lat_lng_requires_h3",
    "geo_generalize with type='lat_lng' requires the h3 library, which is not installed in this environment. Install it with: pip install 'decoy-engine[geo]' or: uv add 'decoy-engine[geo]'",
    since="1.5.0",
)
V2_CONFIG_INVALID = _d(
    "v2_config_invalid", "The pipeline configuration failed schema validation.", since="1.5.0"
)
