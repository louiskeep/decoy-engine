"""Closed vocabularies of the retention contract (F26): classes, backends, reason and exit codes.

Every enum here is append-only. A consumer that meets a value it does not know (a newer producer)
parses it to the ``UNKNOWN`` member via ``parse`` and treats it as retained: eligibility is never
inferred from an unrecognized value. Nothing here imports a locator, a path or a backend SDK.
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import TypeVar

_E = TypeVar("_E", bound=Enum)


class _Closed(str, Enum):
    @classmethod
    def parse(cls: type[_E], value: object) -> _E:
        """The member for ``value``, else the ``UNKNOWN`` member (never raises)."""
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            return cls("unknown")


class ArtifactClass(_Closed):
    RAW_UPLOAD = "raw_upload"
    SNAPSHOT = "snapshot"
    PUBLISHED_OUTPUT = "published_output"
    STAGING = "staging"
    CHECKPOINT = "checkpoint"
    # Explains why value-free evidence survives a purge; never a deletion target.
    EVIDENCE = "evidence"
    UNKNOWN = "unknown"


TARGET_CLASSES: tuple[ArtifactClass, ...] = (
    ArtifactClass.RAW_UPLOAD,
    ArtifactClass.SNAPSHOT,
    ArtifactClass.PUBLISHED_OUTPUT,
    ArtifactClass.STAGING,
    ArtifactClass.CHECKPOINT,
)


class DataClass(_Closed):
    RAW = "raw"
    PROTECTED = "protected"
    DIAGNOSTIC = "diagnostic"
    EVIDENCE = "evidence"
    UNKNOWN = "unknown"


class Backend(_Closed):
    LOCAL = "local"
    S3 = "s3"
    GCS = "gcs"
    UNKNOWN = "unknown"


class Decision(_Closed):
    ELIGIBLE = "eligible"
    RETAINED = "retained"
    UNKNOWN = "unknown"


class ReportKind(_Closed):
    PREVIEW = "preview"
    EXECUTE = "execute"
    UNKNOWN = "unknown"


class RunOutcome(_Closed):
    SUCCESS = "success"
    NOOP = "noop"
    RETAINED = "retained"
    PARTIAL = "partial"
    REFUSED = "refused"
    UNKNOWN = "unknown"


class ExitCode(IntEnum):
    """Retention OUTCOME exit codes shared by ``decoy-admin retention-sweep`` and ``decoy local-cleanup``.

    Only outcomes live here. They occupy 10-14 on purpose: a consumer keeps its own usage and
    runtime-error codes (the CLI's 1/2/3/4/5, argparse's 2) and these can never collide with them.
    """

    SUCCESS = 0
    NOOP = 10
    RETAINED = 11
    PARTIAL = 12
    REFUSED = 13
    # An outcome this build cannot read (a newer producer). Never 0: unknown is not success.
    INDETERMINATE = 14


_OUTCOME_EXIT = {
    RunOutcome.SUCCESS: ExitCode.SUCCESS,
    RunOutcome.NOOP: ExitCode.NOOP,
    RunOutcome.RETAINED: ExitCode.RETAINED,
    RunOutcome.PARTIAL: ExitCode.PARTIAL,
    RunOutcome.REFUSED: ExitCode.REFUSED,
}


def exit_code_for(outcome: RunOutcome) -> ExitCode:
    """Unknown outcomes map to INDETERMINATE: never report success for a value that cannot be read."""
    return _OUTCOME_EXIT.get(outcome, ExitCode.INDETERMINATE)


class ReasonCode(_Closed):
    """One precedence-ordered decision, item result or run refusal. Values are wire strings."""

    ELIGIBLE = "eligible"

    # Ledger / identity (precedence step 1 and 6)
    RETAIN_SCHEMA_UNSUPPORTED = "retain_schema_unsupported"
    RETAIN_IDENTITY_UNREADABLE = "retain_identity_unreadable"
    RETAIN_LOCATOR_UNREADABLE = "retain_locator_unreadable"
    RETAIN_IDENTITY_MISMATCH = "retain_identity_mismatch"
    RETAIN_UNSUPPORTED_BACKEND = "retain_unsupported_backend"
    RETAIN_PRODUCER_MISSING = "retain_producer_missing"
    RETAIN_UNREGISTERED = "retain_unregistered"
    # Policy (step 2)
    RETAIN_POLICY_MISSING = "retain_policy_missing"
    RETAIN_POLICY_INVALID = "retain_policy_invalid"
    RETAIN_KEEP_ALL = "retain_keep_all"
    RETAIN_NOT_EXPIRED = "retain_not_expired"
    RETAIN_CLOCK_MISSING = "retain_clock_missing"
    # Holds (step 3)
    RETAIN_HOLD = "retain_hold"
    RETAIN_BACKEND_HOLD = "retain_backend_hold"
    RETAIN_HOLD_UNREADABLE = "retain_hold_unreadable"
    # Quiescence (step 4)
    RETAIN_PRODUCER_ACTIVE = "retain_producer_active"
    RETAIN_LEASE = "retain_lease"
    RETAIN_TARGET_LOCK = "retain_target_lock"
    RETAIN_RESERVATION = "retain_reservation"
    RETAIN_RESUMABLE = "retain_resumable"
    # Dependencies (step 5)
    RETAIN_DEP_PIPELINE = "retain_dep_pipeline"
    RETAIN_DEP_JOB_YAML = "retain_dep_job_yaml"
    RETAIN_DEP_WORKFLOW = "retain_dep_workflow"
    RETAIN_DEP_STORM = "retain_dep_storm"
    RETAIN_DEP_OUTPUT_GENERATION = "retain_dep_output_generation"
    RETAIN_DEP_CHECKPOINT = "retain_dep_checkpoint"
    RETAIN_DEP_ACTIVE_ATTEMPT = "retain_dep_active_attempt"
    RETAIN_YAML_UNREADABLE = "retain_yaml_unreadable"
    RETAIN_EVIDENCE_INCOMPLETE = "retain_evidence_incomplete"
    # Recovery (step 7) and execution-time drift
    RETAIN_RECOVERY_UNPROVEN = "retain_recovery_unproven"
    RETAIN_STATE_CHANGED = "retain_state_changed"
    RETAIN_PRODUCER_REQUIRED = "retain_producer_required"
    RETAIN_UNKNOWN = "retain_unknown"

    # Item results of an execution
    RESULT_DELETED = "result_deleted"
    RESULT_ABSENT_CONFIRMED = "result_absent_confirmed"
    RESULT_RETRYABLE = "result_retryable"
    RESULT_BACKEND_REFUSED = "result_backend_refused"
    RESULT_RECONCILE_PENDING = "result_reconcile_pending"

    # Run-level refusals (nothing was deleted)
    REFUSED_GATE_OFF = "refused_gate_off"
    REFUSED_RUN_STALE = "refused_run_stale"
    REFUSED_RUN_INCOMPLETE = "refused_run_incomplete"
    REFUSED_RUN_CONSUMED = "refused_run_consumed"
    REFUSED_DIGEST_MISMATCH = "refused_digest_mismatch"
    REFUSED_SCOPE_MISMATCH = "refused_scope_mismatch"
    REFUSED_RECOVERY_MISSING = "refused_recovery_missing"
    REFUSED_RECOVERY_UNPROVEN = "refused_recovery_unproven"
    REFUSED_AUTHORIZATION = "refused_authorization"
    REFUSED_AUDIT_UNAVAILABLE = "refused_audit_unavailable"
    REFUSED_UNSAFE_PLATFORM = "refused_unsafe_platform"
    REFUSED_MANIFEST_INVALID = "refused_manifest_invalid"
    REFUSED_CONFIRMATION = "refused_confirmation"

    UNKNOWN = "unknown"

    @property
    def family(self) -> str:
        v = self.value
        if v == "eligible":
            return "eligible"
        if v.startswith("retain_"):
            return "retain"
        if v.startswith("result_"):
            return "result"
        if v.startswith("refused_"):
            return "refused"
        return "unknown"

    @classmethod
    def parse(cls, value: object) -> ReasonCode:
        """Unknown values parse to ``RETAIN_UNKNOWN`` so a newer producer's code can only retain."""
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError:
            return cls.RETAIN_UNKNOWN


# Fixed safe summaries for human output. Never formatted from data.
REASON_SUMMARY: dict[ReasonCode, str] = {
    ReasonCode.ELIGIBLE: "Expired, unheld and unreferenced under the policy snapshot.",
    ReasonCode.RETAIN_SCHEMA_UNSUPPORTED: "The lifecycle record uses a schema this build cannot read.",
    ReasonCode.RETAIN_IDENTITY_UNREADABLE: "The registered object identity could not be read.",
    ReasonCode.RETAIN_LOCATOR_UNREADABLE: "The protected locator could not be decrypted.",
    ReasonCode.RETAIN_IDENTITY_MISMATCH: "The object no longer matches its registered identity.",
    ReasonCode.RETAIN_UNSUPPORTED_BACKEND: "The storage backend is not supported for deletion.",
    ReasonCode.RETAIN_PRODUCER_MISSING: "No producer is recorded for this artifact.",
    ReasonCode.RETAIN_UNREGISTERED: "The object was never registered; it is report-only.",
    ReasonCode.RETAIN_POLICY_MISSING: "No retention policy applies.",
    ReasonCode.RETAIN_POLICY_INVALID: "The retention policy snapshot is invalid.",
    ReasonCode.RETAIN_KEEP_ALL: "The policy keeps this class indefinitely.",
    ReasonCode.RETAIN_NOT_EXPIRED: "The retention window has not elapsed.",
    ReasonCode.RETAIN_CLOCK_MISSING: "No trustworthy clock reading was available.",
    ReasonCode.RETAIN_HOLD: "An active hold protects this artifact.",
    ReasonCode.RETAIN_BACKEND_HOLD: "The storage backend reports a hold or retention lock.",
    ReasonCode.RETAIN_HOLD_UNREADABLE: "Hold state could not be read.",
    ReasonCode.RETAIN_PRODUCER_ACTIVE: "The producer is not yet terminal and quiescent.",
    ReasonCode.RETAIN_LEASE: "A scheduler lease protects this artifact.",
    ReasonCode.RETAIN_TARGET_LOCK: "A target lock protects this artifact.",
    ReasonCode.RETAIN_RESERVATION: "A scheduler reservation protects this artifact.",
    ReasonCode.RETAIN_RESUMABLE: "A resumable owner protects this artifact.",
    ReasonCode.RETAIN_DEP_PIPELINE: "A live pipeline or version references this artifact.",
    ReasonCode.RETAIN_DEP_JOB_YAML: "A job snapshot references this artifact.",
    ReasonCode.RETAIN_DEP_WORKFLOW: "A workflow step references this artifact.",
    ReasonCode.RETAIN_DEP_STORM: "A scan references this artifact.",
    ReasonCode.RETAIN_DEP_OUTPUT_GENERATION: "An active output generation references this artifact.",
    ReasonCode.RETAIN_DEP_CHECKPOINT: "A checkpoint resume reference protects this artifact.",
    ReasonCode.RETAIN_DEP_ACTIVE_ATTEMPT: "An active attempt references this artifact.",
    ReasonCode.RETAIN_YAML_UNREADABLE: "A referencing definition could not be parsed.",
    ReasonCode.RETAIN_EVIDENCE_INCOMPLETE: "Dependency evidence was truncated or unavailable.",
    ReasonCode.RETAIN_RECOVERY_UNPROVEN: "No tested recovery path covers this artifact.",
    ReasonCode.RETAIN_STATE_CHANGED: "State changed after the preview; retained.",
    ReasonCode.RETAIN_PRODUCER_REQUIRED: "Artifact facts are not yet materialized in a finalizer.",
    ReasonCode.RETAIN_UNKNOWN: "Unrecognized reason; retained.",
    ReasonCode.RESULT_DELETED: "Deleted and exact absence confirmed.",
    ReasonCode.RESULT_ABSENT_CONFIRMED: "Already absent; exact absence confirmed.",
    ReasonCode.RESULT_RETRYABLE: "The backend outcome was transient or ambiguous; retryable.",
    ReasonCode.RESULT_BACKEND_REFUSED: "The backend refused the conditional delete; retained.",
    ReasonCode.RESULT_RECONCILE_PENDING: "Backend outcome known; finalization pending reconciliation.",
    ReasonCode.REFUSED_GATE_OFF: "The live-purge feature gate is off.",
    ReasonCode.REFUSED_RUN_STALE: "The run has expired.",
    ReasonCode.REFUSED_RUN_INCOMPLETE: "The run's inventory was incomplete.",
    ReasonCode.REFUSED_RUN_CONSUMED: "The run was already executed.",
    ReasonCode.REFUSED_DIGEST_MISMATCH: "The confirmation digest does not match the run.",
    ReasonCode.REFUSED_SCOPE_MISMATCH: "The requested scope differs from the run.",
    ReasonCode.REFUSED_RECOVERY_MISSING: "No recovery evidence was supplied.",
    ReasonCode.REFUSED_RECOVERY_UNPROVEN: "Recovery evidence is stale, failed or covers a different method.",
    ReasonCode.REFUSED_AUTHORIZATION: "Fresh owner authorization is missing or invalid.",
    ReasonCode.REFUSED_AUDIT_UNAVAILABLE: "The blocking audit record could not be written.",
    ReasonCode.REFUSED_UNSAFE_PLATFORM: "This platform cannot execute deletion safely.",
    ReasonCode.REFUSED_MANIFEST_INVALID: "The manifest is missing, malformed or unverifiable.",
    ReasonCode.REFUSED_CONFIRMATION: "Interactive confirmation was not given.",
    ReasonCode.UNKNOWN: "Unrecognized code.",
}
