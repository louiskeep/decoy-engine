"""The value-free search result and the released table.

``SearchResult`` holds counts, measures, the chosen node, the lattice's
column names and heights, and the privacy-model descriptors (column names and
thresholds). Its constructor checks the exact type of every field, so a cell
value, a label or a numpy scalar cannot be stored in it.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd
import pyarrow as pa

from decoy_engine.quality.anonymity import (
    DistinctLDiversity,
    EntropyLDiversity,
    EqualTCloseness,
    KAnonymity,
    OrderedTCloseness,
    PrivacyModel,
    RecursiveCLDiversity,
)

METRIC_TOKENS = frozenset({"discernibility", "loss", "non_uniform_entropy"})

# Exact types only: a subclass could add a field holding a cell value, and
# the result and every error built from a model would then carry it.
BUILT_IN_MODELS: tuple[type[PrivacyModel], ...] = (
    KAnonymity,
    DistinctLDiversity,
    EntropyLDiversity,
    RecursiveCLDiversity,
    EqualTCloseness,
    OrderedTCloseness,
)

# The exact types each scalar field may hold.
_FIELD_TYPES: dict[str, tuple[type, ...]] = {
    "found": (bool,),
    "lattice_size": (int,),
    "nodes_evaluated": (int,),
    "rows": (int,),
    "suppression_limit": (float,),
    "allowed_suppressed": (int,),
    "rows_suppressed": (int, type(None)),
    "loss": (float, type(None)),
    "discernibility": (int, type(None)),
    "loss_metric": (float, type(None)),
    "non_uniform_entropy": (float, type(None)),
    "class_count": (int, type(None)),
    "k_min": (int, type(None)),
}


def _int_tuple(value: object) -> bool:
    return type(value) is tuple and all(type(x) is int for x in value)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """What the search chose and why, with no cell value.

    ``node`` is None (and ``found`` False) when no node meets the targets
    within the suppression limit; the loss and class fields are then None.
    ``loss`` is the objective metric's value; all three metrics are given.
    ``class_count`` and ``k_min`` describe the classes left after suppression.
    """

    columns: tuple[str, ...]
    heights: tuple[int, ...]
    models: tuple[PrivacyModel, ...]
    node: tuple[int, ...] | None
    found: bool
    metric: str
    lattice_size: int
    nodes_evaluated: int
    rows: int
    suppression_limit: float
    allowed_suppressed: int
    rows_suppressed: int | None
    loss: float | None
    discernibility: int | None
    loss_metric: float | None
    non_uniform_entropy: float | None
    class_count: int | None
    k_min: int | None

    def __post_init__(self) -> None:
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if not _allowed(field.name, value):
                raise TypeError(
                    f"SearchResult.{field.name} must be a builtin count, measure or token, "
                    f"not {type(value).__name__}"
                )


def _allowed(name: str, value: Any) -> bool:
    if name == "columns":
        return type(value) is tuple and all(type(x) is str for x in value)
    if name == "heights":
        return _int_tuple(value)
    if name == "models":
        return type(value) is tuple and all(type(x) in BUILT_IN_MODELS for x in value)
    if name == "node":
        return value is None or _int_tuple(value)
    if name == "metric":
        return type(value) is str and value in METRIC_TOKENS
    return type(value) in _FIELD_TYPES.get(name, ())


@dataclass(frozen=True, eq=False)
class Released:
    """The table generalized to the chosen node.

    ``table`` has the suppressed rows removed (record suppression);
    ``suppressed`` flags them by input position. The all-rows form is kept only
    under the explicit name ``generalized_including_suppressed``: its
    suppressed rows are not protected by the targets.
    """

    table: pd.DataFrame | pa.Table
    suppressed: npt.NDArray[np.bool_]
    generalized_including_suppressed: pd.DataFrame | pa.Table
