"""FK-aware subsetting engine core (Sprint G, SS1-SS5).

Public surface ONLY: `run_subset_preflight`, `plan_subset`, `run_subset`,
`relationships_from_config`, `subset_inputs_from_config`, the public dataclass
types, and the error classes. Everything else under `decoy_engine.subset.*` is
private. Not re-exported from the top-level `decoy_engine` package this sprint
(SS6's CLI imports `decoy_engine.subset` directly) -- keeps the frozen public
surface small until the CLI/platform wiring lands.
"""

from __future__ import annotations

from decoy_engine.subset._api import (
    plan_subset,
    run_subset,
    subset_inputs_from_config,
)
from decoy_engine.subset._edges import relationships_from_config
from decoy_engine.subset._errors import (
    SubsetBudgetExceededError,
    SubsetConfigError,
    SubsetError,
    SubsetInternalError,
    SubsetPreflightError,
)
from decoy_engine.subset._preflight import run_subset_preflight
from decoy_engine.subset._types import (
    ClosureResult,
    EdgeDirection,
    EdgeStats,
    FanOutBudget,
    FanOutPolicy,
    FkPreflightEdgeReport,
    FkPreflightReport,
    Predicate,
    PredicateOp,
    PreflightFailure,
    RoundTrace,
    SeedMode,
    SeedSpec,
    SubsetEdge,
    SubsetManifest,
    SubsetPlan,
    SubsetResult,
    SubsetSource,
    TableEstimate,
)

__all__ = [
    "ClosureResult",
    "EdgeDirection",
    "EdgeStats",
    "FanOutBudget",
    "FanOutPolicy",
    "FkPreflightEdgeReport",
    "FkPreflightReport",
    "Predicate",
    "PredicateOp",
    "PreflightFailure",
    "RoundTrace",
    "SeedMode",
    "SeedSpec",
    "SubsetBudgetExceededError",
    "SubsetConfigError",
    "SubsetEdge",
    "SubsetError",
    "SubsetInternalError",
    "SubsetManifest",
    "SubsetPlan",
    "SubsetPreflightError",
    "SubsetResult",
    "SubsetSource",
    "TableEstimate",
    "plan_subset",
    "relationships_from_config",
    "run_subset",
    "run_subset_preflight",
    "subset_inputs_from_config",
]
