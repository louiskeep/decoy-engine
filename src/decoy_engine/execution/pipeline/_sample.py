"""Which masked columns of a sample may differ from the full run.

A sample keeps a prefix or a closure of the source rows, so a strategy gives a kept row the full
run's value only when its output depends on that row's value alone. `CHUNK_SAFE_STRATEGIES` is
that set (it is what the chunked FK gate already relies on); faker and categorical join it when
the column is deterministic. Anything else, including a strategy added later, is approximate:
the label says "may differ", so over-labeling is the safe side.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from decoy_engine.config import classify_table_kinds
from decoy_engine.execution._chunked_fk_vocab import CHUNK_SAFE_STRATEGIES

# The column option is `deterministic` (`_strategies/_faker.py`, `_categorical.py`).
SAMPLE_EXACT_WHEN_DETERMINISTIC: frozenset[str] = frozenset({"faker", "categorical"})

# Pool and cardinality modes whose output depends on the whole column (runtime `nunique`, pool
# assignment), so a kept row can differ from the full run.
_WHOLE_COLUMN_MODES = frozenset({"unique", "match_source_cardinality", "scale_source_cardinality"})


def is_sample_exact(strategy: str, column: Mapping[str, Any]) -> bool:
    """True when the strategy gives a sampled row the same value as the full run."""
    if strategy in CHUNK_SAFE_STRATEGIES:
        return True
    # An absent option reads as false, the config default.
    return strategy in SAMPLE_EXACT_WHEN_DETERMINISTIC and bool(column.get("deterministic", False))


def approximate_columns(config: dict[str, Any]) -> list[tuple[str, str]]:
    """(table, column) for every column of a sample that may differ from the full run, in config
    order: a non-exact mask strategy, a whole-column cardinality mode, any generate column."""
    kinds = classify_table_kinds(config)
    found: list[tuple[str, str]] = []
    for table in config.get("tables") or []:
        name = table["name"]
        if kinds.get(name) == "generate":
            found.extend((name, col["name"]) for col in table["generate_columns"])
            continue
        for col in table.get("columns") or []:
            if col.get("cardinality_mode") in _WHOLE_COLUMN_MODES or not is_sample_exact(
                col["strategy"], col
            ):
                found.append((name, col["name"]))
    return found
