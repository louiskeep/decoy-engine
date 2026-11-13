"""Declared relationship keys, checked on a run's output against the same facts over its inputs.

Primary keys are each distinct parent end; foreign keys are each distinct child end, checked once
against the union of every parent that declares it. A key blocks only on what the run introduced:
`preserve` and `warn` keep source orphans, and an inherited duplicate is not the run's fault.
Results hold counts, table names, column names and reason codes, never a key value.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from decoy_engine import reasons
from decoy_engine.config import RelationshipConfig
from decoy_engine.profiling import KeyBatches, key_facts, keys_comparable, passes_through
from decoy_engine.progress import safe_error_class
from decoy_engine.reasons import ReasonCode, reason_ref_payload
from decoy_engine.verification.postmask._stream_input import (
    TableBatches,
    key_batches,
    missing_columns,
)

Kind = Literal["primary", "foreign"]
Status = Literal["checked", "key_type_mismatch", "key_column_missing", "error"]
# How the source side was counted (4.2.2). Only a measured or no_source baseline has counts.
Baseline = Literal[
    "measured", "no_source", "key_column_missing", "key_type_mismatch", "error", "not_run"
]
_End = tuple[str, tuple[str, ...]]

__all__ = ["Baseline", "KeyCounts", "KeyResult", "Status", "check_keys", "key_codes", "keys_report"]


@dataclass(frozen=True)
class KeyCounts:
    rows: int = 0
    null_key_rows: int = 0
    duplicate_excess: int = 0
    orphan_rows: int = 0
    orphan_distinct_keys: int = 0

    def as_dict(self, kind: Kind) -> dict[str, int]:
        names = (
            ("rows", "null_key_rows", "duplicate_excess")
            if kind == "primary"
            else ("rows", "null_key_rows", "orphan_rows", "orphan_distinct_keys")
        )
        return {n: getattr(self, n) for n in names}


@dataclass(frozen=True)
class KeyResult:
    table: str
    columns: tuple[str, ...]
    kind: Kind
    parent: tuple[str, ...]  # the parent tables a foreign key is checked against
    status: Status
    bounded: bool
    codes: tuple[ReasonCode, ...]
    # None unless the baseline was counted, so no reader can take an unmeasured zero as measured.
    source: KeyCounts | None
    output: KeyCounts
    baseline: Baseline
    error_class: str | None = None
    baseline_error_class: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "table": self.table,
            "columns": list(self.columns),
            "kind": self.kind,
            "parent": list(self.parent),
            "status": self.status,
            "bounded": self.bounded,
            "baseline": self.baseline,
            "codes": [reason_ref_payload(c.spec.ref) for c in self.codes],
            "source": None if self.source is None else self.source.as_dict(self.kind),
            "output": self.output.as_dict(self.kind),
        }
        if self.error_class is not None:
            out["error_class"] = self.error_class
        if self.baseline_error_class is not None:
            out["baseline_error_class"] = self.baseline_error_class
        return out


def keys_report(results: Sequence[KeyResult]) -> list[dict[str, Any]]:
    return [r.as_dict() for r in results]


def check_keys(
    relationships: Sequence[RelationshipConfig],
    source: Mapping[str, TableBatches],
    output: Mapping[str, TableBatches],
    *,
    scratch: Path,
    memory_limit: str,
) -> tuple[KeyResult, ...]:
    """One result per declared key. A table absent from `source` has a baseline of zero (a
    generated table). `scratch` is caller-owned."""
    primaries: dict[_End, None] = {}
    parents_of: dict[_End, dict[_End, None]] = {}
    for rel in relationships:
        parent = (rel.parent.table, tuple(rel.parent.columns))
        primaries[parent] = None
        for child in rel.children:
            parents_of.setdefault((child.table, tuple(child.columns)), {})[parent] = None
    todo = [(end, ()) for end in primaries] + [(c, tuple(p)) for c, p in parents_of.items()]
    results = []
    for n, (end, parents) in enumerate(todo):
        run = _Run(source, output, scratch / f"k{n}", memory_limit)
        results.append(run.check(end, parents))
    return tuple(results)


class _Run:
    def __init__(
        self,
        source: Mapping[str, TableBatches],
        output: Mapping[str, TableBatches],
        scratch: Path,
        memory_limit: str,
    ) -> None:
        self._sides = {"source": source, "output": output}
        self._scratch = scratch
        self._limit = memory_limit

    def check(self, end: _End, parents: tuple[_End, ...]) -> KeyResult:
        kind: Kind = "foreign" if parents else "primary"
        names = tuple(dict.fromkeys(t for t, _ in parents))
        table, columns = end
        try:
            status, bounded, out = self._measure("output", end, parents)
        except Exception as exc:
            if passes_through(exc):
                raise
            return KeyResult(
                table=table,
                columns=columns,
                kind=kind,
                parent=names,
                status="error",
                bounded=True,
                codes=(reasons.post_scan.CHECK_ERROR,),
                source=None,
                output=KeyCounts(),
                baseline="not_run",
                error_class=_class_of(exc),
            )
        baseline, base, failed = self._baseline(end, parents, status)
        return KeyResult(
            table=table,
            columns=columns,
            kind=kind,
            parent=names,
            status=status,
            bounded=bounded,
            codes=key_codes(status, kind, baseline, base, out),
            source=base,
            output=out,
            baseline=baseline,
            baseline_error_class=failed,
        )

    def _baseline(
        self, end: _End, parents: tuple[_End, ...], out_status: Status
    ) -> tuple[Baseline, KeyCounts | None, str | None]:
        if out_status == "key_column_missing":
            return "not_run", None, None
        got = self._resolve("source", end, parents)
        if got == "no_source":
            try:
                inherited = self._inherited(end, parents)
            except Exception as exc:
                if passes_through(exc):
                    raise
                return "error", None, _class_of(exc)
            if inherited is not None:
                return "measured", inherited, None
            return "no_source", KeyCounts(), None
        if isinstance(got, str):
            return "key_column_missing", None, None
        own, found = got
        if parents and not keys_comparable(found[0], [*found[1:], own]):
            return "key_type_mismatch", None, None
        if out_status == "key_type_mismatch":
            return "not_run", None, None
        try:
            counts = self._measure("source", end, parents)[2]
        except Exception as exc:
            if passes_through(exc):
                raise
            return "error", None, _class_of(exc)
        return "measured", counts, None

    def _inherited(self, end: _End, parents: tuple[_End, ...]) -> KeyCounts | None:
        """A child that is in the inputs but whose parent is generated: the input child keys that
        match no output parent key. The generated parent has no input side, so these orphans came
        with the data (`preserve` and `warn` keep them) and are not the run's doing. `None` when
        the child is itself generated, or a side cannot be read or compared."""
        own = self._lookup("source", end)
        found = [self._lookup("output", p) for p in parents]
        if not parents or not isinstance(own, KeyBatches):
            return None
        if not all(isinstance(f, KeyBatches) for f in found):
            return None
        keys = [f for f in found if isinstance(f, KeyBatches)]
        if not keys_comparable(keys[0], [*keys[1:], own]):
            return None
        facts = key_facts(
            _union(keys), [*keys[1:], own], scratch=self._scratch, memory_limit=self._limit
        )
        if facts.status != "checked":
            return None
        c = facts.children[-1]
        return KeyCounts(
            rows=c.rows,
            null_key_rows=c.null_key_rows,
            orphan_rows=c.orphan_rows,
            orphan_distinct_keys=c.orphan_distinct_keys,
        )

    def _lookup(self, side: str, end: _End) -> KeyBatches | str:
        table = self._sides[side].get(end[0])
        if table is None:
            return "no_source"
        if missing_columns(table, end[1]):
            return "key_column_missing"
        return key_batches(table, end[1])

    def _resolve(
        self, side: str, end: _End, parents: tuple[_End, ...]
    ) -> tuple[KeyBatches, list[KeyBatches]] | str:
        """The key batches of the key and of each parent, or why one cannot be read: a table that
        is not there (`no_source`, a generated table) before a column that is not."""
        got = [self._lookup(side, e) for e in (end, *parents)]
        for reason in ("no_source", "key_column_missing"):
            if reason in got:
                return reason
        keys = [k for k in got if isinstance(k, KeyBatches)]
        return keys[0], keys[1:]

    def _measure(
        self, side: str, end: _End, parents: tuple[_End, ...]
    ) -> tuple[Status, bool, KeyCounts]:
        got = self._resolve(side, end, parents)
        if isinstance(got, str):
            return "key_column_missing", True, KeyCounts()
        own, found = got
        if not parents:
            facts = key_facts(own, (), scratch=self._scratch, memory_limit=self._limit)
            counts = KeyCounts(
                rows=facts.parent_rows,
                null_key_rows=facts.parent_null_key_rows,
                duplicate_excess=facts.parent_duplicate_excess,
            )
            return facts.status, facts.bounded, counts
        # Every extra parent rides along as a child so key_facts compares its types with the
        # first parent's; its own counts are dropped.
        facts = key_facts(
            _union(found), [*found[1:], own], scratch=self._scratch, memory_limit=self._limit
        )
        if facts.status != "checked":
            return facts.status, facts.bounded, KeyCounts()
        c = facts.children[-1]
        counts = KeyCounts(
            rows=c.rows,
            null_key_rows=c.null_key_rows,
            orphan_rows=c.orphan_rows,
            orphan_distinct_keys=c.orphan_distinct_keys,
        )
        return "checked", facts.bounded, counts


def _class_of(exc: Exception) -> str:
    # The class name only: a DuckDB or reader message can quote key values.
    return safe_error_class(type(exc).__name__) or "Exception"


def _union(parts: Sequence[KeyBatches]) -> KeyBatches:
    if len(parts) == 1:
        return parts[0]

    def batches() -> Iterator[Any]:
        for part in parts:
            yield from part.batches()

    return KeyBatches(parts[0].schema, batches, all(p.bounded for p in parts))


def key_codes(
    status: Status, kind: Kind, baseline: Baseline, base: KeyCounts | None, out: KeyCounts
) -> tuple[ReasonCode, ...]:
    p = reasons.post_scan
    if status == "key_column_missing":
        return (p.KEY_COLUMN_MISSING,)
    if status == "key_type_mismatch":
        return (p.KEY_TYPE_MISMATCH,)
    # A source side that could not be counted rides on the key as a code the verdict only warns
    # on; output counts are then compared with zero, so any output count above it still blocks.
    if base is None:
        base = KeyCounts()
    found: list[ReasonCode] = []
    if baseline == "error":
        found.append(p.CHECK_ERROR)
    elif baseline == "key_type_mismatch":
        found.append(p.KEY_TYPE_MISMATCH)
    if kind == "primary":
        if out.null_key_rows > base.null_key_rows:
            found.append(p.PRIMARY_KEY_NULLS_INTRODUCED)
        if out.duplicate_excess > base.duplicate_excess:
            found.append(p.PRIMARY_KEY_DUPLICATES_INTRODUCED)
    elif out.orphan_rows > base.orphan_rows:
        found.append(p.FOREIGN_KEY_ORPHANS_INTRODUCED)
    return tuple(found)
