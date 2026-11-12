"""The one blocking decision of a post-scan report (stub: filled in by the next commit)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, NamedTuple

from decoy_engine.errors import DecoyError
from decoy_engine.reasons import ReasonRef

__all__ = ["PostScanBlockedError", "PostScanVerdict", "post_scan_verdict", "severity_counts"]


class SeverityCounts(NamedTuple):
    passed: int
    warning: int
    fail: int
    error: int


@dataclass(frozen=True)
class PostScanVerdict:
    blocking: bool
    codes: tuple[ReasonRef, ...]
    warnings: tuple[ReasonRef, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"blocking": self.blocking, "codes": [], "warnings": []}


def post_scan_verdict(report: Mapping[str, Any], *, ner_blocked: bool = False) -> PostScanVerdict:
    return PostScanVerdict(False, (), ())


def severity_counts(severities: Iterable[str]) -> SeverityCounts:
    return SeverityCounts(0, 0, 0, 0)


class PostScanBlockedError(DecoyError):
    def __init__(self, report: Mapping[str, Any], verdict: PostScanVerdict | None = None) -> None:
        self.report = dict(report)
        self.verdict = verdict or post_scan_verdict(report)
        self.reason = ""
        super().__init__("")
