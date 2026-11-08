"""Closed report and input types of the residual canary check (``canary-span-report/v1``).

Every public field is an exact ``int`` count, a ``bool``, a closed enum or a tuple of those, so
no canary, cell value, column name, path or exception text is representable in a report (F34
invariant I1). The pattern is the F29 reason registry's: closed identities instead of free text,
validated once at construction. Frozen ``slots`` dataclasses (PEP 557, PEP 681 for typing) make
a built report immutable; ``__post_init__`` checks exact types because ``bool`` is an ``int``
and an ``int`` subclass may carry arbitrary state. Field order, enum order and reason order are
frozen under v1 (I8); a breaking edit needs ``canary-span-report/v2``.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Iterator
from dataclasses import dataclass, fields
from itertools import pairwise
from typing import TYPE_CHECKING

from decoy_engine import reasons
from decoy_engine.errors import DecoyError

if TYPE_CHECKING:
    import pyarrow as pa

MAX_COUNT = 2**53 - 1
MAX_FINDINGS = 4096


class CanarySpanReportSchema(enum.Enum):
    V1 = "canary-span-report/v1"


class CanaryNormalizationSchema(enum.Enum):
    V1 = "canary-span-normalization/v1"


class CanaryStatus(enum.Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    REJECTED = "rejected"


class CanaryReason(enum.Enum):
    """Declaration order is the canonical ``reasons`` order (frozen); values are F29 codes."""

    MATCH = reasons.verification.CANARY_MATCH
    SCAN_INCOMPLETE = reasons.verification.CANARY_SCAN_INCOMPLETE
    INPUT_REJECTED = reasons.verification.CANARY_INPUT_REJECTED
    BINDING_ABSENT = reasons.verification.CANARY_BINDING_ABSENT
    PUBLICATION_INACTIVE = reasons.verification.CANARY_PUBLICATION_INACTIVE
    TARGET_UNAVAILABLE = reasons.verification.CANARY_TARGET_UNAVAILABLE
    PUBLICATION_MISMATCH = reasons.verification.CANARY_PUBLICATION_MISMATCH
    ARTIFACT_UNREADABLE = reasons.verification.CANARY_ARTIFACT_UNREADABLE
    ARTIFACT_UNSUPPORTED = reasons.verification.CANARY_ARTIFACT_UNSUPPORTED
    RESOURCE_LIMIT = reasons.verification.CANARY_RESOURCE_LIMIT
    CELL_UNCOMPARED = reasons.verification.CANARY_CELL_UNCOMPARED
    SPAN_SHORT_EXCLUDED = reasons.verification.CANARY_SPAN_SHORT_EXCLUDED


class CanaryObservedReason(enum.Enum):
    """The only reasons a policy input may carry; the five derived ones are never accepted."""

    PUBLICATION_INACTIVE = reasons.verification.CANARY_PUBLICATION_INACTIVE
    TARGET_UNAVAILABLE = reasons.verification.CANARY_TARGET_UNAVAILABLE
    PUBLICATION_MISMATCH = reasons.verification.CANARY_PUBLICATION_MISMATCH
    ARTIFACT_UNREADABLE = reasons.verification.CANARY_ARTIFACT_UNREADABLE
    ARTIFACT_UNSUPPORTED = reasons.verification.CANARY_ARTIFACT_UNSUPPORTED
    RESOURCE_LIMIT = reasons.verification.CANARY_RESOURCE_LIMIT
    CELL_UNCOMPARED = reasons.verification.CANARY_CELL_UNCOMPARED


class CanaryConsumerReason(enum.Enum):
    """The one reason only the consumer knows; the engine derives every other from counts."""

    PUBLICATION_INACTIVE = reasons.verification.CANARY_PUBLICATION_INACTIVE


class CanaryMatchMode(enum.Enum):
    """Strongest first; a cell counts once, under the first mode that matches."""

    EXACT_CELL = "exact_cell"
    EXACT_SPAN = "exact_span"
    NFC_WHITESPACE_SPAN = "nfc_whitespace_span"


class CanaryBindingSource(enum.Enum):
    PUBLICATION_RECORD = "publication_record"
    NONE = "none"


class CanaryArtifactOutcome(enum.Enum):
    SCANNED = "scanned"
    DUPLICATE = "duplicate"
    UNSUPPORTED = "unsupported"
    UNREADABLE = "unreadable"
    RESOURCE_LIMITED = "resource_limited"
    MISMATCHED = "mismatched"
    UNREPORTED = "unreported"


class CanaryCompletionClass(enum.Enum):
    CLEAR = "clear"
    FINDINGS = "findings"
    INCOMPLETE = "incomplete"
    REJECTED = "rejected"


class CanarySpanReportInvalid(DecoyError):  # noqa: N818 (F34 plan name, plus "Span")
    """A report or finding that breaks its invariants. Fixed: no message, no args."""

    def __init__(self) -> None:
        super().__init__()


class CanaryPolicyInputError(DecoyError):
    """A policy or input value of the wrong type or kind. Fixed: no message, no args."""

    def __init__(self) -> None:
        super().__init__()


_READ_FAILURE_OUTCOMES = frozenset(
    {
        CanaryArtifactOutcome.UNREADABLE,
        CanaryArtifactOutcome.UNSUPPORTED,
        CanaryArtifactOutcome.RESOURCE_LIMITED,
    }
)


class CanaryArtifactReadFailure(DecoyError):  # noqa: N818 (name frozen by the F34 plan)
    """A consumer's batch reader could not read one artifact. Its only arg is the outcome."""

    def __init__(self, outcome: CanaryArtifactOutcome) -> None:
        if type(outcome) is not CanaryArtifactOutcome or outcome not in _READ_FAILURE_OUTCOMES:
            raise CanaryPolicyInputError
        super().__init__(outcome)
        self.outcome = outcome


def _is_count(value: object) -> bool:
    return type(value) is int and 0 <= value <= MAX_COUNT


def _check(ok: bool) -> None:
    if not ok:
        raise CanarySpanReportInvalid


@dataclass(frozen=True, slots=True)
class CanaryColumnFinding:
    artifact_ordinal: int
    column_ordinal: int
    match_mode: CanaryMatchMode
    affected_cell_count: int
    count_capped: bool

    def __post_init__(self) -> None:
        _check(_is_count(self.artifact_ordinal) and _is_count(self.column_ordinal))
        _check(type(self.match_mode) is CanaryMatchMode)
        _check(_is_count(self.affected_cell_count) and type(self.count_capped) is bool)

    def sort_key(self) -> tuple[int, int, int]:
        return (self.artifact_ordinal, self.column_ordinal, _MODES.index(self.match_mode))


_MODES = tuple(CanaryMatchMode)
_REASONS = tuple(CanaryReason)


@dataclass(frozen=True, slots=True)
class CanarySpanReport:
    schema_version: CanarySpanReportSchema
    normalization_policy_version: CanaryNormalizationSchema
    status: CanaryStatus
    complete: bool
    reasons: tuple[CanaryReason, ...]
    binding_source: CanaryBindingSource
    input_canary_count: int
    accepted_canary_count: int
    exact_pattern_count: int
    normalized_pattern_count: int
    normalization_collision_count: int
    excluded_short_canary_count: int
    requested_artifact_count: int
    scanned_artifact_count: int
    duplicate_artifact_count: int
    unsupported_artifact_count: int
    unreadable_artifact_count: int
    resource_limited_artifact_count: int
    mismatched_artifact_count: int
    unreported_artifact_count: int
    scanned_column_count: int
    scanned_row_count: int
    scanned_text_byte_count: int
    oversized_cell_count: int
    uncompared_cell_count: int
    affected_cell_count: int
    finding_count: int
    count_capped: bool
    findings: tuple[CanaryColumnFinding, ...]

    def __post_init__(self) -> None:
        _check(type(self.schema_version) is CanarySpanReportSchema)
        _check(type(self.normalization_policy_version) is CanaryNormalizationSchema)
        _check(type(self.status) is CanaryStatus and type(self.complete) is bool)
        _check(type(self.binding_source) is CanaryBindingSource)
        _check(type(self.count_capped) is bool)
        _check(all(_is_count(getattr(self, n)) for n in _COUNT_FIELDS))
        _check(self.complete == (self.status is CanaryStatus.COMPLETE))
        self._check_reasons()
        self._check_findings()

    def _check_reasons(self) -> None:
        reasons = self.reasons
        _check(type(reasons) is tuple and all(type(r) is CanaryReason for r in reasons))
        order = [_REASONS.index(r) for r in reasons]
        _check(all(a < b for a, b in pairwise(order)))
        incomplete = self.status is CanaryStatus.INCOMPLETE
        _check((CanaryReason.SCAN_INCOMPLETE in reasons) == incomplete)
        rejected_only = reasons == (CanaryReason.INPUT_REJECTED,)
        _check(rejected_only == (self.status is CanaryStatus.REJECTED))

    def _check_findings(self) -> None:
        findings = self.findings
        _check(type(findings) is tuple and len(findings) <= MAX_FINDINGS)
        _check(all(type(f) is CanaryColumnFinding for f in findings))
        keys = [f.sort_key() for f in findings]
        _check(all(a < b for a, b in pairwise(keys)))
        _check(self.finding_count >= len(findings))
        _check(self.finding_count == len(findings) or self.count_capped)


_COUNT_FIELDS = tuple(f.name for f in fields(CanarySpanReport) if f.name.endswith("_count"))


@dataclass(frozen=True, slots=True)
class CanarySpanPolicy:
    """Scan bounds. Ranges are checked by the scan, which reports ``REJECTED``; types here."""

    span_min_scalars: int
    max_rows: int
    max_text_bytes: int

    def __post_init__(self) -> None:
        if not all(type(getattr(self, f.name)) is int for f in fields(self)):
            raise CanaryPolicyInputError

    @classmethod
    def default(cls) -> CanarySpanPolicy:
        return cls(span_min_scalars=8, max_rows=100_000, max_text_bytes=256 * 1024 * 1024)


@dataclass(frozen=True, slots=True)
class CanaryArtifactInput:
    """One artifact: either an outcome the consumer already decided, or a batch opener.

    ``SCANNED`` cannot be preclassified: a scanned count must come from bytes the engine read.
    """

    artifact_ordinal: int
    preclassified: CanaryArtifactOutcome | None
    open_batches: Callable[[], Iterator[pa.RecordBatch]] | None

    def __post_init__(self) -> None:
        pre, opener = self.preclassified, self.open_batches
        ok = _is_count(self.artifact_ordinal) and (pre is None) != (opener is None)
        ok = ok and (pre is None or type(pre) is CanaryArtifactOutcome)
        ok = ok and pre is not CanaryArtifactOutcome.SCANNED
        if not ok or not (opener is None or callable(opener)):
            raise CanaryPolicyInputError
