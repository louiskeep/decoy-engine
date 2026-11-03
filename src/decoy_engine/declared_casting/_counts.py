"""Issue ledger and count algebra for declared casting.

Every parse/format failure and every substitution is recorded exactly once and reduced to bounded
integers keyed by closed enums: no value, token, exception text or path ever enters this module.

Algebra (checked by the sentries and B6):
  total_substitutions = sentinel_to_null + parse_null + output_null
  total_failures      = parse_null + parse_quarantine + parse_fail
                        + output_null + output_quarantine + output_fail
  quarantined rows    = the distinct (table, row) set; a source null is never a substitution.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from decoy_engine.reasons import Phase, ReasonContext, ReasonRecord, approve_name, approve_ref

SCHEMA_VERSION = 1
COMPLETE, PARTIAL, UNAVAILABLE = "complete", "partial_before_abort", "unavailable"
_TRACK_LIMIT = 1_000_000  # duplicate detection is an invariant check; beyond this it only counts
_STAGES = ("parse", "format")
_DISPOSITIONS = ("fail", "null", "quarantine")
_SUBSTITUTING = {"parse_null", "output_null"}


@dataclass(frozen=True)
class CastIssue:
    table: str
    column: str
    row: int
    stage: str
    reason: Any
    disposition: str


def _record(issue: CastIssue) -> ReasonRecord:
    """The F29 occurrence: registry identity and semantics, the stage as phase, the column as ref.

    A parse failure happens before masking (``MASK``); a format failure after it (``FINALIZE``).
    A name F29 does not approve is left out of the context rather than leaked.
    """
    phase = Phase.MASK if issue.stage == "parse" else Phase.FINALIZE
    ref = approve_ref(f"{issue.table}.{issue.column}")
    return ReasonRecord.for_reason(
        issue.reason, phase=phase, context=ReasonContext.build(object_ref=ref)
    )


class CastIssueLedger:
    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._rows: dict[tuple[str, int], list[ReasonRecord]] = {}
        self._seen: set[tuple[str, str, int, str]] = set()
        self._truncated = False

    def add(self, issue: CastIssue) -> None:
        if issue.stage not in _STAGES or issue.disposition not in _DISPOSITIONS:
            raise ValueError("unknown stage or disposition")
        key = (issue.table, issue.column, issue.row, issue.stage)
        if len(self._seen) < _TRACK_LIMIT:
            if key in self._seen:
                raise ValueError("a casting issue was counted twice")
            self._seen.add(key)
        else:
            self._truncated = True
        stage = "parse" if issue.stage == "parse" else "output"
        self._counts[(issue.table, issue.column)][f"{stage}_{issue.disposition}"] += 1
        if issue.disposition == "quarantine":
            self._rows.setdefault((issue.table, issue.row), []).append(_record(issue))

    def add_sentinel(self, table: str, column: str, n: int = 1) -> None:
        self._counts[(table, column)]["sentinel_to_null"] += n

    def is_quarantined(self, table: str, row: int) -> bool:
        return (table, row) in self._rows

    def quarantined_rows(self) -> set[tuple[str, int]]:
        return set(self._rows)

    def row_reasons(self, table: str, row: int) -> tuple[ReasonRecord, ...]:
        return tuple(self._rows.get((table, row), ()))

    def counts(self, *, completeness: str = COMPLETE) -> dict[str, Any]:
        by_table: dict[str, dict[str, dict[str, dict[str, int]]]] = {}
        subs = fails = 0
        for (table, column), cell in sorted(self._counts.items()):
            subs += sum(v for k, v in cell.items() if k in _SUBSTITUTING or k == "sentinel_to_null")
            fails += sum(v for k, v in cell.items() if k != "sentinel_to_null")
            safe_t, safe_c = approve_name(table), approve_name(column)
            if safe_t is None or safe_c is None:
                continue  # an unsafe name is dropped from the map, never from the totals
            by_table.setdefault(safe_t, {"by_column": {}})["by_column"][safe_c] = {
                k: v for k, v in sorted(cell.items()) if v
            }
        return {
            "schema_version": SCHEMA_VERSION,
            "completeness": completeness,
            "total_substitutions": subs,
            "total_failures": fails,
            "quarantined_rows": len(self._rows),
            "by_table": by_table,
        }
