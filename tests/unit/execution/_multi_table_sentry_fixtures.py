"""Strategy fixtures for the job-gate-9 registry sentry (B7 acceptance test 2).

`_auto_chunk_strategies.STRATEGY_FIXTURES` covers the chunk-admitted strategies; these
cover the rest of `SCALAR_HANDLERS`: the shuffle with no seed, and the non-deterministic
categorical, which is reproducible but whose split stays vetoed. The sentry asserts the two
sets together cover the live registry exactly.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

N = 60


def _col(name: str, strategy: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "strategy": strategy, **extra}


def _s(fmt: str) -> pa.Array:
    return pa.array([fmt.format(i=i) for i in range(N)])


EXTRA_FIXTURES: dict[str, tuple[list[dict[str, Any]], dict[str, pa.Array]]] = {
    "shuffle:deterministic": (
        [_col("val", "shuffle", deterministic=True, namespace="sh_ns")],
        {"val": _s("x{i}")},
    ),
    "shuffle:unseeded": (
        [_col("val", "shuffle", deterministic=False)],
        {"val": _s("x{i}")},
    ),
    "categorical:nondeterministic": (
        [
            _col(
                "val",
                "categorical",
                deterministic=False,
                namespace="cat_ns",
                provider_config={"categories": ["a", "b", "c", "d"]},
            )
        ],
        {"val": _s("x{i}")},
    ),
    "nested:deterministic": (
        [
            _col(
                "val",
                "nested",
                namespace="n_ns",
                provider_config={"target": "$.k", "strategy": "hash"},
            )
        ],
        {"val": pa.array([f'{{"k": "z{i}"}}' for i in range(N)])},
    ),
    "nested:nondeterministic_categorical": (
        [
            _col(
                "val",
                "nested",
                deterministic=False,
                namespace="nc_ns",
                provider_config={
                    "target": "$.k",
                    "strategy": "categorical",
                    "strategy_config": {"categories": ["a", "b", "c", "d"]},
                },
            )
        ],
        {"val": pa.array([f'{{"k": "z{i}"}}' for i in range(N)])},
    ),
    "nested:unseeded_shuffle": (
        [
            _col(
                "val",
                "nested",
                deterministic=False,
                provider_config={"target": "$.k", "strategy": "shuffle"},
            )
        ],
        {"val": pa.array([f'{{"k": "z{i}"}}' for i in range(N)])},
    ),
    "geo_generalize:zip": (
        [
            _col(
                "val",
                "geo_generalize",
                provider_config={
                    "type": "zip",
                    "cascade": ["zip5", "zip3", "suppress"],
                    "k_threshold": 1,
                },
            )
        ],
        {"val": pa.array([f"{98100 + i % 17:05d}" for i in range(N)])},
    ),
    "faker:seeded_from_job": (
        [_col("val", "faker", provider="person_first_name", deterministic=False)],
        {"val": _s("x{i}")},
    ),
    "formula": (
        [_col("val", "formula", provider_config={"formula": "value + 1"})],
        {"val": pa.array(list(range(N)))},
    ),
    "derived": (
        [
            _col("a", "passthrough"),
            _col("val", "derived", provider_config={"expression": "a + 10"}),
        ],
        {"a": pa.array([float(i) for i in range(N)]), "val": pa.array([0.0] * N)},
    ),
    "derived_aggregate": (
        [
            _col("a", "passthrough"),
            _col("val", "derived_aggregate", provider_config={"op": "sum", "column": "a"}),
        ],
        {"a": pa.array([float(i) for i in range(N)]), "val": pa.array([0.0] * N)},
    ),
    "grouped_series": (
        [
            _col("g", "passthrough"),
            _col("o", "passthrough"),
            _col("val", "grouped_series", provider_config={"group_by": "g", "order_by": "o"}),
        ],
        {
            "g": pa.array([f"g{i % 3}" for i in range(N)]),
            "o": pa.array(list(range(N))),
            "val": pa.array([0.0] * N),
        },
    ),
    "joint_mask": (
        [
            _col("id", "passthrough"),
            _col(
                "zip",
                "joint_mask",
                provider_config={
                    "columns": ["zip", "city", "state"],
                    "reference": "us_zip5_city_state",
                    "key_by": "id",
                },
            ),
            _col("city", "passthrough"),
            _col("state", "passthrough"),
        ],
        {
            "id": pa.array([f"P{i:04d}" for i in range(N)]),
            "zip": pa.array(["00000"] * N),
            "city": pa.array(["placeholder"] * N),
            "state": pa.array(["XX"] * N),
        },
    ),
}
