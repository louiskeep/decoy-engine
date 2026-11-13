"""The one blocking decision of a post-scan report, its fixed code order and its exception.

A key decides by its codes and its `baseline`, never by `status == "checked"`: a mismatch over a
mismatched source, or a failure counting the source, only warns. A key row whose status, baseline
and codes are not a combination `_keys` can write is unreadable and blocks with `check_error`.
A residual or policy `fail` gives `finding_blocked`; an `error` finding or a set
`pass_failed_with` gives `check_error`. An `fk_preservation` `fail` adds the orphans code (a set,
so a key's own copy stays one); an `error` adds `check_error` only when no key blocks.
A model `residual_pii` row (`source` `ner` or `classifier`) skips that rule and blocks only
through the NER predicate (SCAN-ML 4.5, AT-M4f): under `ner_findings: fail` an NER row at `fail`
or `error` adds `ner_blocked`; a classifier row never blocks; a model row whose severity is
outside the known set still adds `check_error`. Nothing here carries a data value.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple, cast, get_args

from decoy_engine import reasons
from decoy_engine.errors import DecoyError
from decoy_engine.reasons import MAX_NAME_LEN, ReasonRef, reason_ref_payload
from decoy_engine.verification.postmask import _keys
from decoy_engine.verification.postmask.types import SCHEMA_VERSION

__all__ = [
    "PostScanBlockedError",
    "PostScanVerdict",
    "block_location",
    "blocked_error",
    "post_scan_verdict",
    "raise_if_ner_blocked",
    "severity_counts",
]

_P = reasons.post_scan
_NER_BLOCKED = _P.NER_BLOCKED.spec.ref
# Inability to check first, then structure, then data.
_ORDER: tuple[ReasonRef, ...] = (
    *(
        c.spec.ref
        for c in (
            _P.CHECK_ERROR,
            _P.KEY_COLUMN_MISSING,
            _P.KEY_TYPE_MISMATCH,
            _P.PRIMARY_KEY_NULLS_INTRODUCED,
            _P.PRIMARY_KEY_DUPLICATES_INTRODUCED,
            _P.FOREIGN_KEY_ORPHANS_INTRODUCED,
            _P.FINDING_BLOCKED,
        )
    ),
    _NER_BLOCKED,
)
_CHECK_ERROR, _KEY_TYPE_MISMATCH, _FINDING_BLOCKED = (
    _P.CHECK_ERROR.spec.ref,
    _P.KEY_TYPE_MISMATCH.spec.ref,
    _P.FINDING_BLOCKED.spec.ref,
)
_ORPHANS = _P.FOREIGN_KEY_ORPHANS_INTRODUCED.spec.ref
_FALLBACK_SUMMARY = "The post-scan integrity check blocked the run."
_MAX_SPOTS = 3
# Read from `_keys` so a new status or baseline cannot be missed here.
_STATUSES = frozenset(get_args(_keys.Status))
_BASELINES = frozenset(get_args(_keys.Baseline))
_SEVERITIES = frozenset({"info", "warning", "fail", "error"})
_MODEL_SOURCES = frozenset({"ner", "classifier"})
_NER_MODES = ("warn", "fail")
# A report that cannot be read is a check that could not run, so it blocks.
_UNREADABLE = object()


class SeverityCounts(NamedTuple):
    passed: int
    warning: int
    fail: int
    error: int


def severity_counts(severities: Iterable[str]) -> SeverityCounts:
    """The one tally of finding severities; `info` counts as passed."""
    tally = Counter(severities)
    return SeverityCounts(tally["info"], tally["warning"], tally["fail"], tally["error"])


@dataclass(frozen=True)
class PostScanVerdict:
    """`codes` are the blocking reasons in the documented order, `warnings` the key codes that
    stay on a key without blocking. `blocking` is true exactly when `codes` is not empty."""

    blocking: bool
    codes: tuple[ReasonRef, ...]
    warnings: tuple[ReasonRef, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocking": self.blocking,
            "codes": [reason_ref_payload(c) for c in self.codes],
            "warnings": [reason_ref_payload(c) for c in self.warnings],
        }


def _rows(report: Mapping[str, Any], section: str) -> Iterator[Mapping[str, Any] | object]:
    """The rows of one section; a present section that is not a list, or a row that is not a
    mapping, comes out as `_UNREADABLE` so the caller can block on it."""
    if section not in report:
        return
    rows = report[section]
    if not isinstance(rows, (list, tuple)):
        yield _UNREADABLE
        return
    for row in rows:
        yield row if isinstance(row, Mapping) else _UNREADABLE


def _post_scan_ref(payload: object) -> ReasonRef | None:
    """The ref of one code mapping, only when it names a registered `post_scan` reason. The
    caller never sees a raw string from the report."""
    if not isinstance(payload, Mapping) or payload.get("namespace") != "post_scan":
        return None
    code = payload.get("code")
    if isinstance(code, str) and reasons.find("post_scan", code) is not None:
        return ReasonRef("post_scan", code)
    return None


def _reachable_key_shape(
    status: _keys.Status,
    baseline: _keys.Baseline,
    kind: _keys.Kind,
    refs: tuple[ReasonRef, ...],
) -> bool:
    """Whether `_keys.check_keys` can write this row. The code lists come from `_keys.key_codes`
    itself, run over each count pattern, so there is no second copy of that table."""
    if status == "error":
        return baseline == "not_run" and refs == (_CHECK_ERROR,)
    if status == "key_column_missing":
        expected = _keys.key_codes(status, kind, baseline, _keys.KeyCounts(), _keys.KeyCounts())
        return baseline == "not_run" and refs == tuple(code.spec.ref for code in expected)
    if status == "key_type_mismatch":
        expected = _keys.key_codes(status, kind, baseline, _keys.KeyCounts(), _keys.KeyCounts())
        return (
            kind == "foreign"
            and baseline not in {"measured", "error"}
            and refs == tuple(code.spec.ref for code in expected)
        )
    if baseline == "not_run" or (kind == "primary" and baseline == "key_type_mismatch"):
        return False
    outputs = (
        (
            _keys.KeyCounts(),
            _keys.KeyCounts(null_key_rows=1),
            _keys.KeyCounts(duplicate_excess=1),
            _keys.KeyCounts(null_key_rows=1, duplicate_excess=1),
        )
        if kind == "primary"
        else (_keys.KeyCounts(), _keys.KeyCounts(orphan_rows=1))
    )
    return refs in {
        tuple(
            code.spec.ref
            for code in _keys.key_codes(status, kind, baseline, _keys.KeyCounts(), out)
        )
        for out in outputs
    }


def _key_refs(key: Mapping[str, Any]) -> Iterator[tuple[ReasonRef, bool]]:
    """Each code of one key result and whether it blocks (4.2.2). A row the engine cannot have
    written blocks with `check_error`. On a readable row a `check_error` blocks unless it is the
    source side's, and a `key_type_mismatch` blocks unless the source types did not compare
    either."""
    baseline, status, kind, payloads = (
        key.get("baseline"),
        key.get("status"),
        key.get("kind"),
        key.get("codes"),
    )
    if (
        not isinstance(payloads, (list, tuple))
        or not isinstance(status, str)
        or status not in _STATUSES
        or not isinstance(baseline, str)
        or baseline not in _BASELINES
        or not isinstance(kind, str)
        or kind not in get_args(_keys.Kind)
    ):
        yield _CHECK_ERROR, True
        return
    refs = tuple(_post_scan_ref(payload) for payload in payloads)
    if any(ref is None for ref in refs) or not _reachable_key_shape(
        cast(_keys.Status, status),
        cast(_keys.Baseline, baseline),
        cast(_keys.Kind, kind),
        cast(tuple[ReasonRef, ...], refs),
    ):
        yield _CHECK_ERROR, True
        return
    for ref in cast(tuple[ReasonRef, ...], refs):
        if ref == _CHECK_ERROR:
            yield ref, baseline != "error"
        elif ref == _KEY_TYPE_MISMATCH:
            yield ref, baseline != "key_type_mismatch"
        else:
            yield ref, True


def _ner_blocks(finding: Mapping[str, Any], ner_findings: str) -> bool:
    """The NER predicate: under `fail`, an NER row at `fail` or `error` withholds the outputs."""
    return (
        ner_findings == "fail"
        and finding.get("source") == "ner"
        and finding.get("severity") in ("fail", "error")
    )


def post_scan_verdict(report: Mapping[str, Any], *, ner_findings: str = "warn") -> PostScanVerdict:
    """Whether `report` blocks, and why. `ner_findings` is the run's `post_scan.ner_findings`
    mode; the report does not carry it, and the default `warn` lets no model row block."""
    if ner_findings not in _NER_MODES:
        raise ValueError("ner_findings must be 'warn' or 'fail'")
    blocked: set[ReasonRef] = set()
    warned: set[ReasonRef] = set()
    for key in _rows(report, "keys"):
        if not isinstance(key, Mapping):
            blocked.add(_CHECK_ERROR)
            continue
        for ref, blocks in _key_refs(key):
            (blocked if blocks else warned).add(ref)
    keyed = bool(blocked)
    version = report.get("schema_version")
    if not isinstance(version, str) or version.split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        blocked.add(_CHECK_ERROR)
    for section in ("residual_pii", "policy_validation"):
        for finding in _rows(report, section):
            severity = finding.get("severity") if isinstance(finding, Mapping) else None
            if (
                section == "residual_pii"
                and isinstance(finding, Mapping)
                and finding.get("source") in _MODEL_SOURCES
                and severity in _SEVERITIES
            ):
                if _ner_blocks(finding, ner_findings):
                    blocked.add(_NER_BLOCKED)
            elif severity == "fail":
                blocked.add(_FINDING_BLOCKED)
            elif severity not in _SEVERITIES or severity == "error":
                blocked.add(_CHECK_ERROR)
    for finding in _rows(report, "fk_preservation"):
        severity = finding.get("severity") if isinstance(finding, Mapping) else None
        if severity not in _SEVERITIES:
            blocked.add(_CHECK_ERROR)
        elif severity == "fail":
            blocked.add(_ORPHANS)
        elif severity == "error" and not keyed:
            blocked.add(_CHECK_ERROR)
    if report.get("pass_failed_with"):
        blocked.add(_CHECK_ERROR)
    rank = {ref: n for n, ref in enumerate(_ORDER)}

    def ordered(refs: set[ReasonRef]) -> tuple[ReasonRef, ...]:
        return tuple(sorted(refs, key=lambda r: (rank.get(r, len(rank)), r.namespace, r.code)))

    codes = ordered(blocked)
    return PostScanVerdict(bool(codes), codes, ordered(warned))


def block_location(report: Mapping[str, Any]) -> str:
    """Table, column and detector id of the first failing findings, and nothing else. A policy
    finding has no detector, so it carries the fixed id `policy_validation`. A model row never
    gives `finding_blocked`, so it is not named here."""
    spots = []
    for section, fixed in (("residual_pii", None), ("policy_validation", "policy_validation")):
        for f in _rows(report, section):
            if not (isinstance(f, Mapping) and f.get("severity") == "fail"):
                continue
            if section == "residual_pii" and f.get("source") in _MODEL_SOURCES:
                continue
            table, column, detector = f.get("table"), f.get("column"), fixed or f.get("detector_id")
            if all(
                isinstance(n, str) and 0 < len(n) <= MAX_NAME_LEN for n in (table, column, detector)
            ):
                spots.append(f"{table}.{column} ({detector})")
    return ", ".join(spots[:_MAX_SPOTS])


class PostScanBlockedError(DecoyError):
    """A post-scan report blocked the run; the outputs are withheld.

    `report` is the value-free report dict, `verdict` the decision on it. `str(error)` and
    `message` are the first blocking code's registry summary plus the safe block location."""

    def __init__(self, report: Mapping[str, Any], verdict: PostScanVerdict | None = None) -> None:
        self.report = dict(report)
        self.verdict = post_scan_verdict(report) if verdict is None else verdict
        first = self.verdict.codes[0] if self.verdict.codes else None
        self.reason = f"{first.namespace}.{first.code}" if first else ""
        # The identity of a ref the verdict produced; the adapter trusts it only when registered.
        registered = first is not None and reasons.find("post_scan", first.code) is not None
        self.code = first.code if first and registered and first.namespace == "post_scan" else None
        summary = (
            reasons.get(first.namespace, first.code).summary
            if first and reasons.is_registered(first.code, (first.namespace,))
            else _FALLBACK_SUMMARY
        )
        where = block_location(report) if _FINDING_BLOCKED in self.verdict.codes else ""
        self.message = f"{summary} At {where}." if where else summary
        super().__init__(self.message)

    def __reduce__(self) -> tuple[Any, ...]:
        return (type(self), (self.report, self.verdict))


def blocked_error(
    report: Mapping[str, Any], verdict: PostScanVerdict
) -> PostScanBlockedError | None:
    """The error that withholds the outputs when `verdict` blocks, else None. Callers outside this
    module raise through here, so the error is always built with the verdict that decided it."""
    return PostScanBlockedError(report, verdict) if verdict.blocking else None


def raise_if_ner_blocked(report: Mapping[str, Any], *, ner_findings: str) -> None:
    """The run's one enforcement (SCAN-ML Q1): raise `PostScanBlockedError`, with an empty
    exception chain, when the NER predicate blocks. Any other blocking code rides along in
    `codes` (and leads by `_ORDER`) but alone stays report-only on the run path."""
    verdict = post_scan_verdict(report, ner_findings=ner_findings)
    error = blocked_error(report, verdict) if _NER_BLOCKED in verdict.codes else None
    if error is None:
        return
    # Lazy: the helper sits in the secret-provider package, which this module need not load.
    from decoy_engine.secret_providers._errors import raise_bare

    raise_bare(error)
