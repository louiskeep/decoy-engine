"""Shared builders for the C1 (deterministic categorical on the chunked route) tests.

The oracle leg is the same table run through `run_mask_chunked` with a forced-oracle
column beside the categorical one (`force_oracle`, a numeric-category categorical that is never
native-admitted), so the schema rule
applies to both legs and the comparison is native-chunked vs oracle-chunked, byte for byte.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from tests.native._b8_support import FORCE, Run, assert_same_as_oracle, run_pair, with_force
from tests.native._chunked_entry_support import (
    make_config,
    passthrough,
)

__all__ = [
    "CATEGORIES",
    "FORCE",
    "WEIGHTS",
    "Run",
    "assert_same_as_oracle",
    "cat_col",
    "make_config",
    "passthrough",
    "run_pair",
    "source",
    "with_force",
]

CATEGORIES = ["alpha", "beta", "gamma", "delta"]
WEIGHTS = [0.55, 0.25, 0.15, 0.05]


def cat_col(
    name: str = "c",
    *,
    weighted: bool = False,
    mode: str | None = "deterministic",
    categories: list[Any] | None = None,
    namespace: str | None = "ns_c",
    **extra: Any,
) -> dict[str, Any]:
    """A categorical column. `mode` is "deterministic", "allow_collisions" or None."""
    cfg: dict[str, Any] = {"categories": list(CATEGORIES if categories is None else categories)}
    if weighted:
        cfg["weights"] = list(WEIGHTS)
    col: dict[str, Any] = {"name": name, "strategy": "categorical", "provider_config": cfg}
    if namespace is not None:
        col["namespace"] = namespace
    if mode == "deterministic":
        col["deterministic"] = True
    elif mode == "allow_collisions":
        col["allow_collisions"] = True
    col.update(extra)
    return col


def source(values: list[str | None], *, typ: pa.DataType | None = None) -> pa.Table:
    """A table with the categorical source `c` and an integer passthrough `p`."""
    return pa.table(
        {
            "c": pa.array(values, typ or pa.string()),
            "p": pa.array(list(range(len(values))), pa.int64()),
        }
    )
