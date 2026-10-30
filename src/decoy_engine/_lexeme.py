"""Contract-aware CSV framing: which columns must reach the casting boundary as exact lexemes.

pandas' and Arrow's default NA vocabularies turn ``NA`` or an empty field into a null before the
declared-casting boundary can count it as a sentinel. A column that declares ``logical_type`` is
therefore read without type inference or NA conversion by every CSV reader; undeclared columns
keep the legacy behaviour. Pattern source: pandas ``read_csv(converters=)``, which bypasses NA
handling for the named columns, and PyArrow ``ConvertOptions`` explicit column types.

Dependency-free on purpose: the readers in ``profile`` and ``io`` both import it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LEXEME_KEY = "lexeme_columns"


def lexeme_columns_by_table(config: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    for table in config.get("tables") or ():
        declared = frozenset(
            c["name"] for c in table.get("columns") or () if c.get("logical_type") is not None
        )
        if declared:
            out[table["name"]] = declared
    return out


def profile_descriptors(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Source descriptors with the declared columns attached; the config itself is not mutated."""
    declared = lexeme_columns_by_table(config)
    out: dict[str, dict[str, Any]] = {}
    for name, descriptor in (config.get("sources") or {}).items():
        out[name] = (
            {**descriptor, LEXEME_KEY: sorted(declared[name])}
            if name in declared
            else dict(descriptor)
        )
    return out


def pandas_lexeme_kwargs(columns: Any) -> dict[str, Any]:
    """``read_csv`` options keeping these columns as exact text; ``{}`` when there are none."""
    names = list(columns or ())
    return {"converters": {name: str for name in names}} if names else {}
