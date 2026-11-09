"""GEN-UNIQUE AT-8 corpus: configs without `unique` / `unique_together`, and their digests.

`corpus_digests()` returns, per config, the sha256 of the generated cells, of the Plan's embedded
`config_json`, and of every column's seed fingerprint. The pinned values in
`test_gen_unique_old_input_bytes.py` were captured on engine main before this slice touched any
generation code, so a green test says the refactors moved no byte of old-input output.
"""

from __future__ import annotations

import hashlib
from typing import Any

from decoy_engine.config._pipeline import PipelineConfig
from decoy_engine.generation.synthesize import generate_tables
from decoy_engine.generators.derivation import strategy_config_fingerprint, with_table
from decoy_engine.plan import compile_plan
from tests.unit._dps_helpers import empty_profile


def _base(tables: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "version": 2,
        "global_settings": {"seed": 4242},
        "sources": {},
        "tables": tables,
        "targets": {t["name"]: {"type": "file", "format": "csv", "path": "o.csv"} for t in tables},
    }


def _parent(rows: int = 40) -> dict[str, Any]:
    return {
        "name": "parent",
        "row_count": rows,
        "generate_columns": [
            {"name": "id", "type": "sequence", "start": 1, "step": 1},
            {
                "name": "code",
                "type": "faker",
                "faker_type": "bothify",
                "faker_kwargs": {"text": "??##"},
            },
        ],
    }


CORPUS: dict[str, dict[str, Any]] = {
    "all_basic_types": _base(
        [
            {
                "name": "people",
                "row_count": 300,
                "generate_columns": [
                    {
                        "name": "id",
                        "type": "sequence",
                        "start": 1000,
                        "step": 3,
                        "prefix": "P",
                        "pad_length": 6,
                    },
                    {"name": "first", "type": "faker", "faker_type": "first_name"},
                    {
                        "name": "mail",
                        "type": "faker",
                        "faker_type": "email",
                        "null_probability": 0.2,
                    },
                    {"name": "tier", "type": "categorical", "categories": ["A", "B", "C", None]},
                    {
                        "name": "w",
                        "type": "categorical",
                        "categories": [1, 2, 3],
                        "weights": [5, 3, 1],
                    },
                    {
                        "name": "score",
                        "type": "faker",
                        "faker_type": "pyint",
                    },
                    {"name": "f", "type": "formula", "formula": "id + score"},
                ],
            }
        ]
    ),
    "pooled_and_opted_out": _base(
        [
            {
                "name": "big",
                "row_count": 50_000,
                "generate_columns": [
                    {"name": "pooled_name", "type": "faker", "faker_type": "first_name"},
                    {
                        "name": "forced_off",
                        "type": "faker",
                        "faker_type": "first_name",
                        "pooled": False,
                    },
                    {
                        "name": "explicit_on",
                        "type": "faker",
                        "faker_type": "last_name",
                        "pooled": True,
                    },
                ],
            }
        ]
    ),
    "reference_shapes": _base(
        [
            _parent(),
            {
                "name": "child",
                "row_count": 120,
                "generate_columns": [
                    {
                        "name": "rand",
                        "type": "reference",
                        "reference_table": "parent",
                        "reference_column": "id",
                    },
                    {
                        "name": "seq",
                        "type": "reference",
                        "reference_table": "parent",
                        "reference_column": "id",
                        "distribution": "sequential",
                    },
                    {
                        "name": "wtd",
                        "type": "reference",
                        "reference_table": "parent",
                        "reference_column": "id",
                        "distribution": "weighted",
                        "weights": [1] * 39 + [9],
                    },
                    {
                        "name": "bounded",
                        "type": "reference",
                        "reference_table": "parent",
                        "reference_column": "id",
                        "min_per_parent": 1,
                        "max_per_parent": 5,
                    },
                ],
            },
        ]
    ),
    "reference_to_sparse_parent": _base(
        [
            {
                "name": "parent",
                "row_count": 60,
                "generate_columns": [
                    {"name": "id", "type": "sequence", "start": 1},
                    {
                        "name": "k",
                        "type": "categorical",
                        "categories": ["x", "y", None],
                        "null_probability": 0.5,
                    },
                ],
            },
            {
                "name": "child",
                "row_count": 50,
                "generate_columns": [
                    {
                        "name": "p",
                        "type": "reference",
                        "reference_table": "parent",
                        "reference_column": "k",
                    },
                ],
            },
        ]
    ),
}


def digests(name: str) -> dict[str, Any]:
    cfg = PipelineConfig.model_validate(CORPUS[name]).model_dump()
    plan = compile_plan(cfg, empty_profile(), decoy_engine_version="test")
    tables = generate_tables(plan)
    cells = hashlib.sha256()
    for t in sorted(tables):
        cells.update(t.encode())
        for c in tables[t].column_names:
            cells.update(c.encode())
            cells.update(repr(tables[t].column(c).to_pylist()).encode())
    fingerprints = hashlib.sha256()
    for table in cfg["tables"]:
        for col in table["generate_columns"]:
            fingerprints.update(
                strategy_config_fingerprint(with_table(col, table["name"])).encode()
            )
    return {
        "cells": cells.hexdigest(),
        "config_json": hashlib.sha256(plan.generation.config_json.encode()).hexdigest(),
        "fingerprints": fingerprints.hexdigest(),
    }


def corpus_digests() -> dict[str, dict[str, Any]]:
    return {name: digests(name) for name in CORPUS}
