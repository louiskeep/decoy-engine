"""Declared relationship keys, checked on a run's output against the same facts over its inputs.

Primary keys are each distinct parent end; foreign keys are each distinct child end, checked once
against the union of every parent that declares it. A key blocks only on what the run introduced:
`preserve` and `warn` keep source orphans, and an inherited duplicate is not the run's fault.
Results hold counts, table names, column names and reason codes, never a key value.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from decoy_engine import reasons
from decoy_engine.config import RelationshipConfig
from decoy_engine.profiling import KeyBatches, key_facts, passes_through
from decoy_engine.progress import safe_error_class
from decoy_engine.reasons import ReasonCode
from decoy_engine.verification.postmask._stream_input import (
    TableBatches,
    key_batches,
    missing_columns,
)

Kind = Literal["primary", "foreign"]
Status = Literal["checked", "key_type_mismatch", "key_column_missing", "error"]
_End = tuple[str, tuple[str, ...]]

__all__ = ["KeyCounts", "KeyResult", "check_keys", "keys_report"]


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
    source: KeyCounts
    output: KeyCounts
    error_class: str | None = None
    baseline: str = "measured"
    baseline_error_class: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "table": self.table,
            "columns": list(self.columns),
            "kind": self.kind,
            "parent": list(self.parent),
            "status": self.status,
            "bounded": self.bounded,
            "codes": [asdict(c.spec.ref) for c in self.codes],
            "source": self.source.as_dict(self.kind),
            "output": self.output.as_dict(self.kind),
        }
        if self.error_class is not None:
            out["error_class"] = self.error_class
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
        failed: str | None = None
        try:
            status, bounded, out = self._measure("output", end, parents)
            base = KeyCounts()
            if status == "checked":
                base = self._measure("source", end, parents)[2]
        except Exception as exc:
            if passes_through(exc):
                raise
            # The class name only: a DuckDB or reader message can quote key values.
            failed = safe_error_class(type(exc).__name__) or "Exception"
        table, columns = end
        if failed is not None:
            return KeyResult(
                table=table,
                columns=columns,
                kind=kind,
                parent=names,
                status="error",
                bounded=True,
                codes=(reasons.post_scan.CHECK_ERROR,),
                source=KeyCounts(),
                output=KeyCounts(),
                error_class=failed,
            )
        return KeyResult(
            table=table,
            columns=columns,
            kind=kind,
            parent=names,
            status=status,
            bounded=bounded,
            codes=_codes(status, kind, base, out),
            source=base,
            output=out,
        )

    def _keys(self, side: str, end: _End) -> KeyBatches | None:
        table = self._sides[side].get(end[0])
        if table is None or missing_columns(table, end[1]):
            return None
        return key_batches(table, end[1])

    def _measure(
        self, side: str, end: _End, parents: tuple[_End, ...]
    ) -> tuple[Status, bool, KeyCounts]:
        own = self._keys(side, end)
        found = [self._keys(side, p) for p in parents]
        if own is None or any(k is None for k in found):
            return ("key_column_missing" if side == "output" else "checked"), True, KeyCounts()
        if not parents:
            facts = key_facts(own, (), scratch=self._scratch, memory_limit=self._limit)
            counts = KeyCounts(
                rows=facts.parent_rows,
                null_key_rows=facts.parent_null_key_rows,
                duplicate_excess=facts.parent_duplicate_excess,
            )
            return facts.status, facts.bounded, counts
        union = _union([k for k in found if k is not None])
        # Every extra parent rides along as a child so key_facts compares its types with the
        # first parent's; its own counts are dropped.
        extras = [k for k in found[1:] if k is not None]
        facts = key_facts(union, [*extras, own], scratch=self._scratch, memory_limit=self._limit)
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


def _union(parts: Sequence[KeyBatches]) -> KeyBatches:
    if len(parts) == 1:
        return parts[0]

    def batches() -> Iterator[Any]:
        for part in parts:
            yield from part.batches()

    return KeyBatches(parts[0].schema, batches, all(p.bounded for p in parts))


def _codes(status: Status, kind: Kind, base: KeyCounts, out: KeyCounts) -> tuple[ReasonCode, ...]:
    p = reasons.post_scan
    if status == "key_column_missing":
        return (p.KEY_COLUMN_MISSING,)
    if status == "key_type_mismatch":
        return (p.KEY_TYPE_MISMATCH,)
    found: list[ReasonCode] = []
    if kind == "primary":
        if out.null_key_rows > base.null_key_rows:
            found.append(p.PRIMARY_KEY_NULLS_INTRODUCED)
        if out.duplicate_excess > base.duplicate_excess:
            found.append(p.PRIMARY_KEY_DUPLICATES_INTRODUCED)
    elif out.orphan_rows > base.orphan_rows:
        found.append(p.FOREIGN_KEY_ORPHANS_INTRODUCED)
    return tuple(found)
