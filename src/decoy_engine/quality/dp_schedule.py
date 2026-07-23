"""The fixed, public DP query schedule shape (DPS Scope B, guide section
4.3.1).

Split out of `quality/dp_budget.py` on a size-cap crossing (CLAUDE.md's
~600 LOC orchestration cap): these are plain, frozen data shapes with no
OpenDP dependency of their own -- the schedule a fit commits to before any
value is examined, not the session that enforces and certifies it. Guide
section 4.3.5 mitigation 1 ("`OpenDpReleaseSession` is the sole
construction and invocation site for OpenDP measurements... pin this with
an import-shape test") is about where `opendp` is imported, not about
where these schedule dataclasses live; `quality/dp_budget.py` remains the
only module importing `opendp` (`tests/unit/quality/
test_opendp_dependency.py::test_quality_dp_budget_is_the_only_module_
that_imports_opendp_anywhere`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NumericQuerySpec:
    """One scheduled numeric-marginal query (guide section 4.4).

    `interior_edges` are the `numeric_bins - 1` public interior cut points
    derived from the column's declared domain (never from data); the
    SAME edges are used both to certify the allocation search and to
    build the real release chain, so the certified loss and the actual
    release are provably the same measurement shape.
    """

    name: str
    interior_edges: tuple[float, ...]

    @property
    def numeric_bins(self) -> int:
        return len(self.interior_edges) + 1


@dataclass(frozen=True)
class CategoricalQuerySpec:
    """The pair of scheduled queries one categorical column contributes
    (guide section 4.5): the thresholded grouped count and the noised
    non-null total."""

    grouped_name: str
    total_name: str


@dataclass(frozen=True)
class Schedule:
    """The fixed, public, deterministic query schedule for one fit (guide
    section 4.3.1). Built from public declarations only, before any value
    is examined."""

    row_count_name: str
    numeric: tuple[NumericQuerySpec, ...]
    categorical: tuple[CategoricalQuerySpec, ...]

    @property
    def query_count(self) -> int:
        return 1 + len(self.numeric) + 2 * len(self.categorical)

    @property
    def query_names(self) -> tuple[str, ...]:
        names = [self.row_count_name]
        names += [q.name for q in self.numeric]
        for q in self.categorical:
            names += [q.grouped_name, q.total_name]
        return tuple(names)

    def delta_per_categorical(self, delta: float) -> float:
        if not self.categorical:
            return 0.0
        return (delta / 2.0) / len(self.categorical)
