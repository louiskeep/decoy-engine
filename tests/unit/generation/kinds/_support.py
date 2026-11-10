"""Builders shared by the GEN-TYPES E1 acceptance tests (test-side only)."""

from __future__ import annotations

from typing import Any

import pyarrow as pa

from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile

SEED = 7

# One valid column per family: the parameters the plan's table gives, plus a name and a type.
FAMILY_COLS: dict[str, dict[str, Any]] = {
    "uniform": {"family": "uniform", "low": 2, "high": 9.5},
}
# Families the registry refuses until E1b brings a portable sampler (plan: Deferred to E1b).
DEFERRED_FAMILIES = ("normal", "exponential", "lognormal", "beta", "poisson", "zipf")
# Keys those families would have taken; uniform must refuse every one as foreign.
FOREIGN_KEYS = ("mean", "std", "rate", "scale", "sigma", "alpha", "beta", "s", "n")


def dist_col(family: str, name: str = "x", **extra: Any) -> dict[str, Any]:
    return {"name": name, "type": "distribution", **FAMILY_COLS[family], **extra}


def cfg(tables: list[dict[str, Any]], seed: int = SEED) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": seed},
        "sources": {},
        "tables": tables,
        "targets": {t["name"]: {"type": "file", "format": "csv", "path": "o.csv"} for t in tables},
    }


def table(name: str, rows: int, cols: list[dict[str, Any]]) -> dict[str, Any]:
    return {"name": name, "row_count": rows, "generate_columns": cols}


def one(col: dict[str, Any], rows: int = 20, seed: int = SEED) -> dict[str, Any]:
    return cfg([table("t", rows, [col])], seed)


def generate(config: dict[str, Any]) -> dict[str, pa.Table]:
    plan = compile_plan(config, empty_profile(), decoy_engine_version="test")
    return generate_tables(plan)


def values(config: dict[str, Any], column: str = "x") -> list[Any]:
    return generate(config)["t"].column(column).to_pylist()
