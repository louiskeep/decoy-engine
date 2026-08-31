"""Invocation-scoped diagnostic aggregation for the native C1 route (Task 3.4).

Covers the HIGH 3 warning/error aggregation contract: a `PoolCache` is shared
across the whole engine process (`get_default_pool_cache()`), and its
`.warnings()` (S5 NF5) ACCUMULATE across every invocation that ever put() a
pool into it -- a shared cache carries prior invocations' warnings forever,
by design (nothing ever prunes it). Without an isolation mechanism, a second
job sharing the default cache would silently inherit the first job's
`pool_dominates_cache` warnings as if they were its own. `RouteDiagnostics`
is that isolation mechanism: one instance per invocation of
`run_native_or_oracle_chunked`, constructed at invocation START, snapshotting
the shared cache's warning count as a baseline so only warnings appended
AFTER that point ever surface as this invocation's evidence.

PRECONDITION: invocations sharing one cache must be SEQUENTIAL. The baseline is
a global length prefix, so two `RouteDiagnostics` constructed at overlapping
times would each slice `[baseline:]` and surface the OTHER invocation's
concurrently-appended warnings (then fan-out mis-attributes them by provider).
The documented coordinator is per-table sequential, so the shipped contract
holds; a concurrent shared-cache path would first need isolation keyed on
something stronger than a global length prefix (e.g. per-invocation tagging of
each warning at emission), NOT this length slice.

This module is standalone (Task 3.4 scope): it is not wired into
`_dispatch.py`'s `run_native_or_oracle_chunked`, mirroring `_pool_quality.py`'s
own standalone contract (Task 3.2) -- there is no engine end-of-stream hook
for the chunked native/oracle routes (both are lazy generators), so a future
coordinator constructs one `RouteDiagnostics` per invocation, feeds it the
`PoolCache` it shares with the route, and calls `evidence()` /
`raise_if_row_errors()` once the chunk stream is drained.

Ordering + dedup mirrors `execution/_chunked.py`'s `aggregate_chunk_warnings`
exactly (the oracle's own per-chunk warning union): equality-based dedup
(QualityWarning carries a dict `detail`, unhashable) preserving first-emission
order, because the same warning re-emitted (e.g. a pool evicted then
byte-identically rebuilt under LRU pressure -- PoolCache.put()'s own docstring:
"an evicted-then-rebuilt pool is byte-identical") collapses to one, exactly
what a non-evicting run would have emitted once.

Row errors have no native producer today (the native kernels and the pool
sampler are pure and do not raise per-row `RowError`s), but the contract must
not silently drop one if a future kernel starts producing them (established
methodology: fail-closed per-row-error is the Sprint 2 honesty pack policy,
`execution/_chunked.py`'s own `if result.row_errors: raise
RowErrorsFailedError(...)` at the first chunk that has any). `record_row_error`
+ `raise_if_row_errors` mirrors that: a caller records errors observed while
masking the CURRENT chunk and calls `raise_if_row_errors()` after each chunk,
so this collector's row-error state never holds more than one chunk's worth
before the job aborts -- the same bound the oracle route already has, not an
arbitrary size cap invented here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypeVar

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution._row_errors import RowError, RowErrorRecord
from decoy_engine.generation.pool._cache import PoolCache
from decoy_engine.generation.pool._events import QualityWarning


@dataclass(frozen=True)
class PoolOwner:
    """One (table, column) that used `provider` to build/select a pool.

    Supplied by the caller at evidence-collection time (not tracked
    internally): `QualityWarning` itself carries only `provider` (see
    `PoolCache.put`'s one construction site), never table/column, so
    attributing a warning to a column requires the caller's own knowledge of
    which columns used which provider this invocation. When two columns
    share a provider under distinct namespaces (e.g. C1's LAST and MAIDEN,
    both `person_last_name`), a warning on that provider cannot be
    disambiguated further from the warning object alone -- both owners are
    attached (fan-out), which is honest given the warning's own shape rather
    than a guess.
    """

    table: str
    column: str
    provider: str


@dataclass(frozen=True)
class AttributedWarning:
    """One deduplicated `QualityWarning` plus every owner that could have
    produced it (see `PoolOwner`'s fan-out note)."""

    warning: QualityWarning
    owners: tuple[PoolOwner, ...]


@dataclass(frozen=True)
class RouteDiagnosticsEvidence:
    """The frozen per-invocation diagnostic snapshot for job evidence."""

    pool_warnings: tuple[AttributedWarning, ...]
    row_errors: tuple[RowErrorRecord, ...]


_T = TypeVar("_T")


def _dedup_ordered(items: Sequence[_T]) -> tuple[_T, ...]:
    """Order-stable equality dedup, mirroring `aggregate_chunk_warnings`.

    Equality-based (not a `set()`) because `QualityWarning`/`RowErrorRecord`
    both carry fields (`detail`, no `__hash__` override needed here since we
    never hash) that are cheap to compare directly; warning/error counts per
    invocation are small (bounded by column count, not row or chunk count),
    so O(n^2) is irrelevant, exactly the oracle aggregator's own reasoning.
    """
    seen: list[_T] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return tuple(seen)


class RouteDiagnostics:
    """Invocation-scoped collector for the native C1 route's diagnostics.

    Construct ONE instance per `run_native_or_oracle_chunked` invocation,
    sharing the SAME `PoolCache` the route uses, before any chunk is masked.
    """

    def __init__(self, pool_cache: PoolCache) -> None:
        self._pool_cache = pool_cache
        # Isolation baseline (the core Task 3.4 contract): `PoolCache.warnings()`
        # only ever appends (S5 NF5), so a length-prefix snapshot at
        # construction time is a stable boundary -- everything at index
        # `< self._baseline_len` predates this invocation and is excluded by
        # construction, regardless of how many prior invocations shared this
        # cache or how many warnings they left behind.
        self._baseline_len = len(pool_cache.warnings())
        self._row_errors: list[RowErrorRecord] = []

    def pool_warnings(self, *, owners: Sequence[PoolOwner] = ()) -> tuple[AttributedWarning, ...]:
        """This invocation's NEW pool warnings only, deduped + ordered.

        `owners` attributes each returned warning to every (table, column)
        the caller says used that warning's provider this invocation (see
        `PoolOwner`); omitted, every warning carries an empty owner tuple
        (still isolated and deduped, just unattributed).
        """
        new = self._pool_cache.warnings()[self._baseline_len :]
        deduped = _dedup_ordered(new)
        by_provider: dict[str, list[PoolOwner]] = {}
        for owner in owners:
            by_provider.setdefault(owner.provider, []).append(owner)
        return tuple(
            AttributedWarning(warning=warning, owners=tuple(by_provider.get(warning.provider, ())))
            for warning in deduped
        )

    def record_row_error(self, error: RowError, *, table: str) -> None:
        """Record one row error observed while masking the CURRENT chunk.

        Callers should call `raise_if_row_errors()` after each chunk
        (mirroring `execution/_chunked.py`'s per-chunk fail-closed gate) so
        this sink never accumulates more than one chunk's worth of errors
        before the job aborts.
        """
        self._row_errors.append(
            RowErrorRecord(
                table=table,
                column=error.column,
                row_index=error.row_index,
                trigger=error.trigger,
                reason=error.reason,
            )
        )

    def row_errors(self) -> tuple[RowErrorRecord, ...]:
        """This invocation's recorded row errors, deduped + ordered."""
        return _dedup_ordered(self._row_errors)

    def evidence(self, *, owners: Sequence[PoolOwner] = ()) -> RouteDiagnosticsEvidence:
        """The frozen snapshot for job evidence: isolated pool warnings plus
        recorded row errors, both deduped and deterministically ordered."""
        return RouteDiagnosticsEvidence(
            pool_warnings=self.pool_warnings(owners=owners),
            row_errors=self.row_errors(),
        )

    def raise_if_row_errors(self) -> None:
        """Fail closed the moment any row error has been recorded.

        Mirrors `execution/_chunked.py`'s `if result.row_errors: raise
        RowErrorsFailedError(...)`: the native route has no quarantine
        machinery either, so a per-row strategy error here is exactly as
        unsafe to silently drop as it is on the oracle chunked path.
        """
        errors = self.row_errors()
        if errors:
            raise RowErrorsFailedError(errors)


__all__ = [
    "AttributedWarning",
    "PoolOwner",
    "RouteDiagnostics",
    "RouteDiagnosticsEvidence",
]
