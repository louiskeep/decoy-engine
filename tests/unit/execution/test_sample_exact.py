"""SAMPLE SE2: which columns of a sample may differ from the full run (AT-12, AT-12a)."""

from __future__ import annotations

from typing import Any

from decoy_engine.execution._chunked_fk_vocab import CHUNK_SAFE_STRATEGIES
from decoy_engine.execution._strategies import SCALAR_HANDLERS
from decoy_engine.execution.pipeline._sample import (
    SAMPLE_EXACT_WHEN_DETERMINISTIC,
    approximate_columns,
    is_sample_exact,
)


def _col(name: str, strategy: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "strategy": strategy, **extra}


def test_at12_approximate_columns_name_the_columns_that_may_differ() -> None:
    config = {
        "tables": [
            {
                "name": "people",
                "columns": [
                    _col("a", "shuffle"),
                    _col("b", "faker", deterministic=False),
                    _col("c", "faker", deterministic=True),
                    _col("d", "hash", namespace="n"),
                    _col("e", "categorical", deterministic=True),
                    _col("f", "hash", namespace="n", cardinality_mode="unique"),
                    _col("g", "hash", namespace="n", cardinality_mode="match_source_cardinality"),
                    _col("h", "hash", namespace="n", cardinality_mode="scale_source_cardinality"),
                ],
            },
            {"name": "made", "generate_columns": [_col("x", "uuid"), _col("y", "int")]},
        ]
    }
    assert approximate_columns(config) == [
        ("people", "a"),
        ("people", "b"),
        ("people", "f"),
        ("people", "g"),
        ("people", "h"),
        ("made", "x"),
        ("made", "y"),
    ]


def test_at12a_every_handler_is_classified_and_an_unknown_one_is_approximate() -> None:
    for name in SCALAR_HANDLERS:
        assert isinstance(is_sample_exact(name, {"deterministic": True}), bool), name
    assert set(SCALAR_HANDLERS) >= CHUNK_SAFE_STRATEGIES
    assert set(SCALAR_HANDLERS) >= SAMPLE_EXACT_WHEN_DETERMINISTIC
    assert {"faker", "categorical"} == SAMPLE_EXACT_WHEN_DETERMINISTIC
    assert all(is_sample_exact(s, {}) for s in CHUNK_SAFE_STRATEGIES)
    assert is_sample_exact("faker", {"deterministic": True})
    assert not is_sample_exact("faker", {"deterministic": False})
    assert not is_sample_exact("shuffle", {"deterministic": True})
    assert not is_sample_exact("added_later_by_someone", {"deterministic": True})
