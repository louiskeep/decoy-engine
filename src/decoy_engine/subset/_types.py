"""Frozen dataclasses for FK-aware subsetting (Sprint G).

House style follows `decoy_engine.plan._types`: frozen dataclasses, tuples
(never lists) for ordered/immutable collections, `__post_init__` guards for
structural invariants only (no I/O, no polars). This module has no
dependency on polars or any subset submodule; every other `subset/*` module
imports from here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

EdgeDirection = Literal["both", "downward", "upward", "none"]
SeedMode = Literal["sample", "filter", "keys"]
PredicateOp = Literal["eq", "ne", "lt", "le", "gt", "ge", "in", "is_null", "is_not_null"]


@dataclass(frozen=True)
class SubsetSource:
    """One table's input location for subsetting.

    Single Parquet file per table (section 10 risk 5): multi-file / glob
    dataset sources are unverified for row-index stability and are not
    supported by this sprint's `SubsetSource`.
    """

    path: str
    format: str  # must be "parquet"; preflight enforces (reject-with-guidance)


@dataclass(frozen=True)
class Predicate:
    """One structured seed-filter predicate. AND-ed with siblings in a SeedSpec."""

    column: str
    op: PredicateOp
    value: Any = None  # scalar for eq..ge; tuple for "in"; None for is_null/is_not_null


@dataclass(frozen=True)
class SeedSpec:
    """One table's seed selection rule.

    `mode` determines which fields are consumed:
    - "sample": `key_columns` non-empty; exactly one of `fraction`/`count`.
    - "filter": `predicates` non-empty.
    - "keys": `key_columns` and `keys` non-empty; every tuple in `keys` has
      length == len(key_columns). Raw key values NEVER get serialized
      (see `SubsetPlan.seed_specs_public` / `SubsetManifest`).
    """

    table: str
    mode: SeedMode
    key_columns: tuple[str, ...] = ()
    fraction: float | None = None
    count: int | None = None
    predicates: tuple[Predicate, ...] = ()
    keys: tuple[tuple[Any, ...], ...] = ()

    def __post_init__(self) -> None:
        if self.mode == "sample":
            if not self.key_columns:
                raise ValueError(
                    f"SeedSpec(table={self.table!r}, mode='sample'): key_columns must be non-empty."
                )
            has_fraction = self.fraction is not None
            has_count = self.count is not None
            if has_fraction == has_count:
                raise ValueError(
                    f"SeedSpec(table={self.table!r}, mode='sample'): exactly one of "
                    "fraction/count must be set."
                )
            if has_fraction and not (0 < (self.fraction or 0) <= 1):
                raise ValueError(
                    f"SeedSpec(table={self.table!r}, mode='sample'): fraction must satisfy "
                    "0 < fraction <= 1."
                )
            if has_count and (self.count or 0) < 1:
                raise ValueError(
                    f"SeedSpec(table={self.table!r}, mode='sample'): count must be >= 1."
                )
        elif self.mode == "filter":
            if not self.predicates:
                raise ValueError(
                    f"SeedSpec(table={self.table!r}, mode='filter'): predicates must be non-empty."
                )
        elif self.mode == "keys":
            if not self.key_columns or not self.keys:
                raise ValueError(
                    f"SeedSpec(table={self.table!r}, mode='keys'): key_columns and keys must "
                    "both be non-empty."
                )
            width = len(self.key_columns)
            for tup in self.keys:
                if len(tup) != width:
                    raise ValueError(
                        f"SeedSpec(table={self.table!r}, mode='keys'): every key tuple must "
                        f"have length {width} (== len(key_columns)); got {len(tup)}."
                    )


@dataclass(frozen=True)
class FanOutBudget:
    """The both-caps budget (GATE-1 #3). Both None means uncapped."""

    max_total_rows: int | None = None
    max_table_seed_multiple: float | None = None


@dataclass(frozen=True)
class FanOutPolicy:
    """Per-edge traversal direction overrides + the budget + the dangling gate.

    GATE-1 #3: upward parent-completeness is ON by default (default direction
    "both"). Disabling upward traversal on any edge can dangle a child FK;
    `_policy.resolve_edge_directions` refuses that unless `allow_dangling` is
    explicitly True.
    """

    budget: FanOutBudget = field(default_factory=FanOutBudget)
    edge_directions: tuple[tuple[str, EdgeDirection], ...] = ()
    allow_dangling: bool = False


@dataclass(frozen=True)
class SubsetEdge:
    """A normalized (parent, child) FK edge pair, built by `_edges.py`."""

    edge_id: str
    parent_table: str
    parent_columns: tuple[str, ...]
    child_table: str
    child_columns: tuple[str, ...]
    orphan_policy: str
    namespace: str | None


@dataclass(frozen=True)
class EdgeStats:
    """Cumulative rows added by one edge, across the whole closure."""

    edge_id: str
    direction: EdgeDirection
    rows_added_downward: int
    rows_added_upward: int


@dataclass(frozen=True)
class RoundTrace:
    """One round of the closure fixpoint loop."""

    round_index: int
    rows_added: int
    per_table_added: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ClosureResult:
    """The output of `compute_closure`.

    `terminated_by` is always "fixpoint" -- the field exists so tests assert
    the exit PATH (no-growth), not merely that the function returned.
    """

    survivors: Mapping[str, frozenset[int]]
    rounds: int
    terminated_by: Literal["fixpoint"]
    edge_stats: tuple[EdgeStats, ...]
    trace: tuple[RoundTrace, ...]


@dataclass(frozen=True)
class TableEstimate:
    """Per-table row counts: input, seed, survivors, seed-null-excluded."""

    table: str
    input_rows: int
    seed_rows: int
    surviving_rows: int
    seed_null_excluded: int


@dataclass(frozen=True)
class SubsetPlan:
    """The dry-run artifact (SS4). Only exists for a passing budget check."""

    engine_version: str
    seed_specs_public: tuple[Mapping[str, Any], ...]
    tables: tuple[TableEstimate, ...]
    edges: tuple[EdgeStats, ...]
    closure_rounds: int
    budget: FanOutBudget
    budget_outcome: Literal["pass"]
    total_surviving_rows: int
    preflight: FkPreflightReport
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class SubsetManifest:
    """Evidence artifact written by SS5. Counts / identifiers ONLY -- no raw key values."""

    manifest_version: int
    engine_version: str
    seed_specs_public: tuple[Mapping[str, Any], ...]
    tables: tuple[TableEstimate, ...]
    edges: tuple[EdgeStats, ...]
    closure_rounds: int
    budget: FanOutBudget
    budget_outcome: Literal["pass"]
    preflight_summary: tuple[FkPreflightEdgeReport, ...]


@dataclass(frozen=True)
class SubsetResult:
    """The `run_subset` return value: the plan, the manifest, the written paths."""

    plan: SubsetPlan
    manifest: SubsetManifest
    output_paths: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PreflightFailure:
    """One fail-closed preflight condition."""

    code: str
    relationship: str
    message: str


@dataclass(frozen=True)
class FkPreflightEdgeReport:
    """Parity with `validation.post._types.FkValidityReport`, pre-selection flavor."""

    relationship: str
    namespace: str | None
    orphan_policy: str
    child_row_count: int
    non_null_child_key_count: int
    parent_match_count: int
    source_orphan_count: int
    invalid_count: int


@dataclass(frozen=True)
class FkPreflightReport:
    """The SS1 preflight output. Never raised directly; `run_subset_preflight` returns it."""

    passed: bool
    failures: tuple[PreflightFailure, ...]
    warnings: tuple[str, ...]
    edges: tuple[FkPreflightEdgeReport, ...]
