"""``retention-report/v1``: the single value-free outcome shape of preview and execution (F26).

Platform API, host executor, audit and the local CLI all serialize through ``to_dict`` here. Every
field is typed and bounded and the dict is built from an explicit allow-list, so adding a field is
a reviewed change (``ALLOWED_REPORT_KEYS`` / ``ALLOWED_ITEM_KEYS`` are asserted by a sentry). The
report carries opaque ids, closed codes, counts and the confirmation digest; never a name, path,
locator, content hash or exception message.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from decoy_engine.retention._canonical import (
    canonical_json,
    check_count,
    check_error_type,
    check_opaque_id,
    check_policy_id,
    sha256_hex,
)
from decoy_engine.retention._vocab import (
    ArtifactClass,
    Decision,
    ExitCode,
    ReasonCode,
    ReportKind,
    RunOutcome,
    exit_code_for,
)

REPORT_SCHEMA = "retention-report/v1"
DIGEST_DOMAIN = "decoy.retention.inventory/v1"

ALLOWED_REPORT_KEYS = frozenset(
    {
        "schema",
        "kind",
        "run_id",
        "policy_id",
        "scope",
        "complete",
        "limits",
        "counts",
        "reason_counts",
        "unregistered_count",
        "outcome",
        "refusal",
        "digest",
        "items",
    }
)
ALLOWED_ITEM_KEYS = frozenset(
    {"artifact_id", "artifact_class", "decision", "reason", "size_bytes", "result", "error_type"}
)
ALLOWED_LIMIT_KEYS = frozenset({"max_items", "max_bytes", "max_backend_calls", "max_seconds"})
ALLOWED_COUNT_KEYS = frozenset(
    {
        "total",
        "eligible",
        "retained",
        "approved",
        "deleted",
        "failed",
        "bytes_eligible",
        "bytes_deleted",
    }
)


@dataclass(frozen=True)
class Limits:
    max_items: int
    max_bytes: int
    max_backend_calls: int
    max_seconds: int

    def __post_init__(self) -> None:
        for name in ALLOWED_LIMIT_KEYS:
            check_count(name, getattr(self, name))

    def to_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in sorted(ALLOWED_LIMIT_KEYS)}


@dataclass(frozen=True)
class Counts:
    total: int = 0
    eligible: int = 0
    retained: int = 0
    approved: int = 0
    deleted: int = 0
    failed: int = 0
    bytes_eligible: int = 0
    bytes_deleted: int = 0

    def __post_init__(self) -> None:
        for name in ALLOWED_COUNT_KEYS:
            check_count(name, getattr(self, name))

    def to_dict(self) -> dict[str, int]:
        return {k: getattr(self, k) for k in sorted(ALLOWED_COUNT_KEYS)}


@dataclass(frozen=True)
class ReportItem:
    artifact_id: str
    artifact_class: ArtifactClass
    decision: Decision
    reason: ReasonCode
    size_bytes: int | None = None
    result: ReasonCode | None = None
    error_type: str | None = None

    def __post_init__(self) -> None:
        check_opaque_id("artifact_id", self.artifact_id)
        if self.size_bytes is not None:
            check_count("size_bytes", self.size_bytes)
        check_error_type(self.error_type)
        # An eligible item carries the eligible code; a retained item never does.
        if (self.decision is Decision.ELIGIBLE) != (self.reason is ReasonCode.ELIGIBLE):
            raise ValueError("decision and reason disagree")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_class": self.artifact_class.value,
            "decision": self.decision.value,
            "reason": self.reason.value,
            "size_bytes": self.size_bytes,
            "result": None if self.result is None else self.result.value,
            "error_type": self.error_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReportItem:
        if set(data) - ALLOWED_ITEM_KEYS:
            raise ValueError("unknown item field")
        decision = Decision.parse(data.get("decision"))
        reason = ReasonCode.parse(data.get("reason"))
        if decision is Decision.UNKNOWN or (decision is Decision.ELIGIBLE) != (
            reason is ReasonCode.ELIGIBLE
        ):
            # Unrecognized or contradictory: can only retain (unknown-and-retained).
            decision, reason = (
                Decision.RETAINED,
                (reason if reason.family == "retain" else ReasonCode.RETAIN_UNKNOWN),
            )
        result = data.get("result")
        return cls(
            artifact_id=data["artifact_id"],
            artifact_class=ArtifactClass.parse(data.get("artifact_class")),
            decision=decision,
            reason=reason,
            size_bytes=data.get("size_bytes"),
            result=None if result is None else ReasonCode.parse(result),
            error_type=data.get("error_type"),
        )


def derive_outcome(
    kind: ReportKind, counts: Counts, *, complete: bool, refusal: ReasonCode | None
) -> RunOutcome:
    """One mapping from counts to the shared outcome, so platform and CLI cannot disagree."""
    if refusal is not None:
        return RunOutcome.REFUSED
    if kind is ReportKind.PREVIEW:
        if not complete:
            return RunOutcome.PARTIAL
        if counts.total == 0:
            return RunOutcome.NOOP
        return RunOutcome.SUCCESS if counts.eligible else RunOutcome.RETAINED
    if counts.approved == 0:
        return RunOutcome.NOOP
    if counts.deleted == counts.approved:
        return RunOutcome.SUCCESS
    if counts.deleted == 0 and counts.failed == 0:
        return RunOutcome.RETAINED
    return RunOutcome.PARTIAL


@dataclass(frozen=True)
class RetentionReport:
    kind: ReportKind
    run_id: str
    policy_id: str
    scope: tuple[ArtifactClass, ...]
    complete: bool
    limits: Limits
    counts: Counts
    digest: str
    items: tuple[ReportItem, ...] = field(default=())
    unregistered_count: int | None = None
    refusal: ReasonCode | None = None

    def __post_init__(self) -> None:
        check_opaque_id("run_id", self.run_id)
        check_policy_id(self.policy_id)
        if self.refusal is not None and self.refusal.family != "refused":
            raise ValueError("refusal must be a refused_* code")
        if self.unregistered_count is not None:
            check_count("unregistered_count", self.unregistered_count)
        if (
            not isinstance(self.digest, str)
            or len(self.digest) != 64
            or not all(c in "0123456789abcdef" for c in self.digest)
        ):
            raise ValueError("digest must be 64 lowercase hex characters")

    @property
    def outcome(self) -> RunOutcome:
        return derive_outcome(self.kind, self.counts, complete=self.complete, refusal=self.refusal)

    @property
    def exit_code(self) -> ExitCode:
        return exit_code_for(self.outcome)

    def reason_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for it in self.items:
            out[it.reason.value] = out.get(it.reason.value, 0) + 1
        return dict(sorted(out.items()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "policy_id": self.policy_id,
            "scope": sorted(c.value for c in self.scope),
            "complete": self.complete,
            "limits": self.limits.to_dict(),
            "counts": self.counts.to_dict(),
            "reason_counts": self.reason_counts(),
            "unregistered_count": self.unregistered_count,
            "outcome": self.outcome.value,
            "refusal": None if self.refusal is None else self.refusal.value,
            "digest": self.digest,
            "items": [i.to_dict() for i in self.items],
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RetentionReport:
        if data.get("schema") != REPORT_SCHEMA:
            raise ValueError("unsupported report schema")
        if set(data) - ALLOWED_REPORT_KEYS:
            raise ValueError("unknown report field")
        refusal = data.get("refusal")
        return cls(
            kind=ReportKind.parse(data.get("kind")),
            run_id=data["run_id"],
            policy_id=data["policy_id"],
            scope=tuple(ArtifactClass.parse(c) for c in data.get("scope", ())),
            complete=bool(data["complete"]),
            limits=Limits(**data["limits"]),
            counts=Counts(**data["counts"]),
            digest=data["digest"],
            items=tuple(ReportItem.from_dict(i) for i in data.get("items", ())),
            unregistered_count=data.get("unregistered_count"),
            refusal=None if refusal is None else ReasonCode.parse(refusal),
        )


def counts_from_items(
    items: Iterable[ReportItem], *, approved: int = 0, bytes_deleted: int = 0
) -> Counts:
    items = tuple(items)
    eligible = [i for i in items if i.decision is Decision.ELIGIBLE]
    deleted = sum(
        1
        for i in items
        if i.result in (ReasonCode.RESULT_DELETED, ReasonCode.RESULT_ABSENT_CONFIRMED)
    )
    failed = sum(
        1
        for i in items
        if i.result
        in (
            ReasonCode.RESULT_RETRYABLE,
            ReasonCode.RESULT_RECONCILE_PENDING,
            ReasonCode.RESULT_BACKEND_REFUSED,
        )
    )
    return Counts(
        total=len(items),
        eligible=len(eligible),
        retained=len(items) - len(eligible),
        approved=approved,
        deleted=deleted,
        failed=failed,
        bytes_eligible=sum(i.size_bytes or 0 for i in eligible),
        bytes_deleted=bytes_deleted,
    )


def inventory_digest(
    *,
    policy_id: str,
    scope: Iterable[ArtifactClass],
    limits: Limits,
    expires_at: int,
    eligible: Sequence[tuple[str, str]],
) -> str:
    """The confirmation digest an operator echoes back to authorize exactly this set.

    ``eligible`` is ``(artifact_id, identity_digest)`` pairs. The identity digests bind the digest
    to each object's registered identity but are inputs only: they are never serialized, so the
    report stays free of content hashes. Pairs are sorted, so ordering cannot change the digest;
    widening scope, adding a target, swapping an identity or changing a limit does."""
    for aid, ident in eligible:
        check_opaque_id("artifact_id", aid)
        if not isinstance(ident, str) or not ident:
            raise ValueError("identity digest required")
    body = {
        "domain": DIGEST_DOMAIN,
        "policy_id": check_policy_id(policy_id),
        "scope": sorted(c.value for c in scope),
        "limits": limits.to_dict(),
        "expires_at": check_count("expires_at", expires_at),
        "eligible": sorted([aid, ident] for aid, ident in eligible),
    }
    return sha256_hex(canonical_json(body))


def render_text(report: RetentionReport) -> str:
    """Human output: counts and opaque ids only, from fixed strings. Safe to print or log."""
    c = report.counts
    lines = [
        f"{REPORT_SCHEMA} {report.kind.value} run {report.run_id}",
        f"outcome: {report.outcome.value}"
        + (f" ({report.refusal.value})" if report.refusal else ""),
        f"complete: {'yes' if report.complete else 'no'}",
        f"total={c.total} eligible={c.eligible} retained={c.retained} "
        f"approved={c.approved} deleted={c.deleted} failed={c.failed}",
        f"bytes_eligible={c.bytes_eligible} bytes_deleted={c.bytes_deleted}",
    ]
    for code, n in report.reason_counts().items():
        lines.append(f"  {code}: {n}")
    lines.append(f"digest: {report.digest}")
    return "\n".join(lines)
