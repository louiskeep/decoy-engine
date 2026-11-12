"""The public canary scan: ``scan_canary_artifacts`` (F34 E3, invariants I2, I5, I7).

One call validates its inputs, builds its patterns, walks the artifacts in ordinal order and
returns a ``CanarySpanReport``. Invalid input gives a ``REJECTED`` report, never an exception.
The call holds no state outside its own frame: patterns, totals and the budget flag are locals
(defect class 3), so concurrent calls cannot see each other.

- Ordinals ``MAX_SCANNED_ARTIFACTS`` and up are ``resource_limited`` and never opened.
- A consumer's ``preclassified`` outcome is counted as given, also after the budget is spent.
- The row and text budgets are checked before each batch (L5), so a batch may overshoot them by
  its own size. The artifact being read is then ``resource_limited`` and every later artifact
  that would need opening is ``unreported``.
- Only a ``CanaryArtifactReadFailure`` of exactly that type sets an outcome; a subclass or any
  other exception propagates (class 6). The batch iterator is closed on every exit (I7).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass

import pyarrow as pa

from decoy_engine.verification.canary._aggregate import Totals, build_report
from decoy_engine.verification.canary._canonical import (
    CellState,
    SchemaSupport,
    canonical_cells,
    classify_schema,
)
from decoy_engine.verification.canary._match import (
    CanaryPatterns,
    build_patterns,
    screen_canaries,
    tally_cells,
)
from decoy_engine.verification.canary._normalize import WHITE_SPACE
from decoy_engine.verification.canary._types import (
    CanaryArtifactInput,
    CanaryArtifactOutcome,
    CanaryArtifactReadFailure,
    CanaryBindingSource,
    CanaryConsumerReason,
    CanaryPolicyInputError,
    CanarySpanPolicy,
    CanarySpanReport,
)

MAX_SCANNED_ARTIFACTS = 64
_MAX_CANARIES = 4096
_MAX_CANARY_BYTES = 4096
_MAX_TOTAL_CANARY_BYTES = 1024 * 1024
_MAX_COLUMNS = 1024
_SPAN_MIN = range(4, 129)
_MAX_ROWS = range(1, 5_000_001)
_MAX_TEXT = range(1, 2 * 1024**3 + 1)
_OUTCOME = CanaryArtifactOutcome


def _canaries_valid(canaries: object) -> bool:
    if not isinstance(canaries, Sequence) or isinstance(canaries, str | bytes | bytearray):
        return False
    if not 1 <= len(canaries) <= _MAX_CANARIES:
        return False
    total = 0
    for canary in canaries:
        if type(canary) is not str:
            return False
        try:
            size = len(canary.encode("utf-8"))  # strict: a lone surrogate raises
        except UnicodeEncodeError:
            return False
        total += size
        blank = all(ord(ch) in WHITE_SPACE for ch in canary)
        if not 1 <= size <= _MAX_CANARY_BYTES or blank or total > _MAX_TOTAL_CANARY_BYTES:
            return False
    return screen_canaries(canaries)


def _policy_valid(policy: object) -> bool:
    return (
        type(policy) is CanarySpanPolicy
        and policy.span_min_scalars in _SPAN_MIN
        and policy.max_rows in _MAX_ROWS
        and policy.max_text_bytes in _MAX_TEXT
    )


def _artifacts_valid(artifacts: object) -> bool:
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, str | bytes | bytearray):
        return False
    if not all(type(a) is CanaryArtifactInput for a in artifacts):
        return False
    return sorted(a.artifact_ordinal for a in artifacts) == list(range(len(artifacts)))


def _consumer_valid(reasons: object) -> bool:
    return type(reasons) is frozenset and all(type(r) is CanaryConsumerReason for r in reasons)


@dataclass(slots=True)
class _Run:
    """State of one scan call."""

    patterns: CanaryPatterns
    policy: CanarySpanPolicy
    totals: Totals
    exhausted: bool = False

    def over_budget(self) -> bool:
        rows = self.totals.count("scanned_row_count")
        text = self.totals.count("scanned_text_byte_count")
        return rows >= self.policy.max_rows or text >= self.policy.max_text_bytes


def _exact_outcome(failure: CanaryArtifactReadFailure) -> CanaryArtifactOutcome:
    if type(failure) is not CanaryArtifactReadFailure:
        raise failure
    return failure.outcome


def _close(iterator: Iterator[pa.RecordBatch]) -> None:
    close = getattr(iterator, "close", None)  # plain iterators have none; generators do
    if callable(close):
        close()


def _scan_batch(run: _Run, ordinal: int, batch: pa.RecordBatch) -> None:
    run.totals.rows(batch.num_rows)
    for column in range(batch.num_columns):
        cells = canonical_cells(batch.column(column))
        texts = [c for c in cells if type(c) is str]
        refused = sum(c is CellState.UNCOMPARED for c in cells)
        tally = tally_cells(run.patterns, texts)
        size = sum(len(t.encode("utf-8")) for t in texts)
        run.totals.cells(ordinal, column, tally, uncompared=refused, text_bytes=size)


def _consume(run: _Run, ordinal: int, batches: Iterator[pa.RecordBatch]) -> CanaryArtifactOutcome:
    columns = 0
    for batch in batches:
        if type(batch) is not pa.RecordBatch:
            raise CanaryPolicyInputError
        if run.over_budget():
            run.exhausted = True
            return _OUTCOME.RESOURCE_LIMITED
        if batch.num_columns > _MAX_COLUMNS:
            return _OUTCOME.RESOURCE_LIMITED
        if classify_schema(batch.schema) is SchemaSupport.UNSUPPORTED:
            return _OUTCOME.UNSUPPORTED
        if batch.num_columns > columns:
            run.totals.columns(batch.num_columns - columns)
            columns = batch.num_columns
        _scan_batch(run, ordinal, batch)
    return _OUTCOME.SCANNED


def _read(run: _Run, artifact: CanaryArtifactInput) -> CanaryArtifactOutcome:
    opener = artifact.open_batches
    if opener is None:  # the input type guarantees one of opener and preclassified
        raise CanaryPolicyInputError
    try:
        batches = opener()
    except CanaryArtifactReadFailure as failure:
        return _exact_outcome(failure)
    try:
        return _consume(run, artifact.artifact_ordinal, batches)
    except CanaryArtifactReadFailure as failure:
        return _exact_outcome(failure)
    finally:
        _close(batches)


def _outcome(run: _Run, artifact: CanaryArtifactInput) -> CanaryArtifactOutcome:
    if artifact.artifact_ordinal >= MAX_SCANNED_ARTIFACTS:
        return _OUTCOME.RESOURCE_LIMITED
    if artifact.preclassified is not None:
        return artifact.preclassified
    if run.exhausted:
        return _OUTCOME.UNREPORTED
    return _read(run, artifact)


def scan_canary_artifacts(
    canaries: Sequence[str],
    policy: CanarySpanPolicy,
    *,
    binding_source: CanaryBindingSource,
    artifacts: Sequence[CanaryArtifactInput],
    consumer_reasons: frozenset[CanaryConsumerReason],
) -> CanarySpanReport:
    """Scan the artifacts for the canaries and report counts, never values."""
    binding = binding_source if type(binding_source) is CanaryBindingSource else None
    valid = binding is not None and _consumer_valid(consumer_reasons)
    valid = valid and _canaries_valid(canaries) and _policy_valid(policy)
    valid = valid and _artifacts_valid(artifacts)
    given = len(canaries) if isinstance(canaries, Sequence) and not isinstance(canaries, str) else 0
    if not valid:
        return build_report(
            binding_source=binding or CanaryBindingSource.NONE,
            input_canary_count=given,
            accepted_canary_count=0,
            patterns=None,
            requested_artifact_count=0,
            totals=Totals(),
            rejected=True,
        )
    unique = tuple(dict.fromkeys(canaries))
    patterns = build_patterns(unique, policy.span_min_scalars)
    run = _Run(patterns, policy, Totals())
    for artifact in sorted(artifacts, key=lambda a: a.artifact_ordinal):
        run.totals.outcome(_outcome(run, artifact))
    return build_report(
        binding_source=binding_source,
        input_canary_count=given,
        accepted_canary_count=len(unique),
        patterns=patterns.counts,
        requested_artifact_count=len(artifacts),
        totals=run.totals,
        publication_inactive=CanaryConsumerReason.PUBLICATION_INACTIVE in consumer_reasons,
    )
