"""Cross-column ``formula`` post-pass for v2 generation.

A generate ``formula`` column whose expression reads sibling columns declares
them via ``references: [...]``. The per-column generation loop in
``generation.synthesize.generate_tables`` cannot evaluate such a formula inline
(the siblings may not be generated yet), so ``_formula`` returns ``[None] * n``
placeholders. This module overwrites those placeholders once every sibling
column is finalized, delegating to the existing
``ColumnGenerator.fill_referenced_formula_column`` (the same v6 per-row family
derivation the inline path uses -- no SEED_PROTOCOL_VERSION change).

Extracted from ``synthesize.py`` to keep that orchestration module under the
~600-LOC cap (best-practices section 4.1); imported lazily so configs without
any reference-bearing formula column never pay the pandas import.
"""

from __future__ import annotations

from typing import Any


def fill_referenced_formula_columns(
    gcols: list[dict[str, Any]],
    data: dict[str, list[Any]],
    seed: int,
    derive_key: Any = None,
) -> None:
    """Fill ``formula`` columns carrying ``references`` from finalized siblings.

    Mutates ``data`` in place. SINGLE declared-order pass: columns are filled
    in their declared order and each result is written back into the working
    DataFrame, so a referenced formula declared AFTER another referenced
    formula reads the earlier one's finalized value. A referenced formula that
    reads a LATER-declared referenced formula sees that sibling's None
    placeholder (resolved to "" in the eval scope); no multi-pass dependency
    resolver is built for out-of-order chains (YAGNI).

    Referenced-formula columns had no prior valid output (they were nulls), so
    there is nothing to preserve byte-for-byte.
    """
    targets = [
        col
        for col in gcols
        if col.get("type") == "formula"
        and (col.get("formula") or "")
        and (col.get("references") or [])
    ]
    if not targets:
        return

    import pandas as pd

    from decoy_engine.generation.synthesize import _apply_null_probability
    from decoy_engine.generators.columns import ColumnGenerator

    df = pd.DataFrame(data)
    cg = ColumnGenerator(seed=seed, derive_key=derive_key)
    for col in targets:
        name = col["name"]
        formula = col.get("formula") or ""
        references = col.get("references") or []
        series = cg.fill_referenced_formula_column(name, formula, references, df)
        # Apply null_probability the same way every other generated column
        # does (the placeholder pass already ran it over all-None values; we
        # re-run it over the computed values so the column's null contract
        # holds on real output).
        values = _apply_null_probability(series.tolist(), col, seed, derive_key)
        data[name] = values
        df[name] = values
