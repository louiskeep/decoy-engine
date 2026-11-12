"""Generators that read other generated data: derived expressions, formulas and FK references.

Split out of ``synthesize.py`` to keep it under the module size cap.
``synthesize`` imports these names back and dispatches through its own module globals, so
patching ``synthesize._formula`` and friends still reaches the dispatcher.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any

from decoy_engine import reasons
from decoy_engine.config import value_key
from decoy_engine.generation.relational import check_fitted_bounds, check_reference_pool
from decoy_engine.generation.statistical import (
    FanoutSpec,
    StatisticalSpecError,
    fitted_reference_values,
)
from decoy_engine.generators.derivation import TABLE_KEY, GenDeriveContext

if TYPE_CHECKING:
    from collections.abc import Callable

    import pyarrow as pa

    from decoy_engine.generation.pool import QualityWarning


def _derived_generate(
    col: dict[str, Any],
    n: int,
    generated: dict[str, list[Any]],
) -> list[Any]:
    """Evaluate a closed-grammar derived expression against already-generated siblings.

    Processes rows inline in declared order. Row context is built from columns in
    ``generated`` (those declared before this one). A sibling declared AFTER this
    column will be absent from the row context and raise at evaluation time;
    check_derived_column_refs validates ref existence at plan-compile time. The
    declared-order constraint mirrors the ``statistical / condition_on`` pattern.
    Deterministic by construction: same generated snapshot -> same output, no RNG.
    """
    from decoy_engine.transforms.derived import DerivedConfig, apply_derived

    config = DerivedConfig.from_dict(
        {
            "expression": col.get("expression", ""),
            "bounds": col.get("bounds"),
            "null_propagation": col.get("null_propagation", "explicit_null"),
        }
    )
    col_name = col.get("name", "?")
    out: list[Any] = []
    for i in range(n):
        row_ctx = {k: vals[i] for k, vals in generated.items() if i < len(vals)}
        out.append(apply_derived(config, row_ctx, column=col_name, row_index=i))
    return out


def _formula(col: dict[str, Any], n: int, seed: int, derive_key: Any = None) -> list[Any]:
    """Python-expression-driven values, parity-frozen vs V1
    ``_generate_formula_column`` (``columns.py:974+``).

    V1's structure (mirrored here):
      - empty ``formula`` -> None series (we just return Nones).
      - ``references: [...]`` set -> return ``[None] * n`` placeholders that
        ``generate_tables``'s ``_referenced_formula.fill_referenced_formula_columns``
        post-pass overwrites once every sibling column is finalized (the
        per-column loop can't read siblings that may not be generated yet).
        No warning is emitted here: the column is filled, not dropped.
      - else (inline path) -> per-row safe_eval with row-seeded ``random`` /
        ``faker`` scope.

    For the inline path we DELEGATE to V1 ``ColumnGenerator._eval_formula_inline``
    (Reading B: pragmatic guaranteed parity; the eval scope is generic Python
    expression machinery + Faker helpers, not v1-specific). A v2-native rewrite
    that lifts the eval scope into ``generation/`` can land alongside S9 v1
    removal. The delegation is the ENG-2 commit-1 of formula; it does not block
    ENG-2's Reading-B exit gate (parity tests are byte-identical).
    """
    formula = col.get("formula") or ""
    references = col.get("references") or []
    if not formula:
        return [None] * n
    if references:
        # Cross-column formula: return None placeholders that the
        # generate_tables post-pass (_referenced_formula.fill_referenced_formula_columns)
        # overwrites once every sibling column is finalized. The per-column
        # loop can't read siblings here, so no eval happens at dispatch.
        return [None] * n
    from decoy_engine.generators.columns import ColumnGenerator

    cg = ColumnGenerator(seed=seed, derive_key=derive_key)
    series = cg._eval_formula_inline(n, formula, col.get("name", "unnamed_column"), col)
    return series.tolist()


def parent_keys(pools: dict[str, pa.Table], table: str, column: str) -> list[Any]:
    """Distinct non-null values of a generated parent column, in first-seen order.

    Insertion-order unique + drop None, matching V1 `dropna().unique()` on a pandas Series. A
    naive set() would lose order -> different random.choice output for the same seed. The one
    parent key list the reference draw and the unique draws read (GEN-UNIQUE U8).
    """
    seen: set = set()
    keys: list[Any] = []
    for v in pools[table].column(column).to_pylist():
        if v is None:
            continue
        # Keyed by equality, so a list or dict parent key dedups instead of raising TypeError.
        # Arrow values come back as plain builtins, so every key is keyable.
        key = value_key(v)
        if key in seen:
            continue
        seen.add(key)
        keys.append(v)
    return keys


def _fitted_reference(
    col: dict[str, Any],
    ref_vals: list[Any],
    n: int,
    seed: int,
    derive_key: Any,
    fanout: FanoutSpec | None,
) -> list[Any]:
    """The `fitted` branch of `_reference`: parent degrees come from the pinned fan-out.

    The seed derives from the snapshot digest the plan pinned at compile time, so the
    `snapshot_file` path is never opened here. A missing spec refuses; it never falls
    back to uniform draws.
    """
    if fanout is None:
        raise StatisticalSpecError(
            code=reasons.plan.REFERENCE_FANOUT_MISSING,
            message=(
                f"reference column {str(col.get('name'))!r} asks for distribution "
                "'fitted' but no pinned fan-out reached generation. Compile the "
                "config into a Plan with a snapshot made by `decoy fit --fanout`, "
                "or use distribution: random."
            ),
        )
    col_seed = GenDeriveContext.for_column(
        derive_key=derive_key,
        column_config=col,
        fallback_seed=seed,
        content_digests={("reference", "snapshot_file"): fanout.seed_digest},
    ).base_int("py")
    return fitted_reference_values(ref_vals, fanout, n, random.Random(col_seed))


def _reference(
    col: dict[str, Any],
    n: int,
    seed: int,
    derive_key: Any = None,
    pools: dict[str, pa.Table] | None = None,
    fanout: FanoutSpec | None = None,
    *,
    policy: str | None = None,
    on_warning: Callable[[QualityWarning], None] | None = None,
) -> list[Any]:
    """FK / mint-a-pool: sample values from a parent table's already-generated key
    column. Parity-frozen vs V1 ``_generate_reference_column`` (``columns.py:758-865``)
    wherever the config gives a shape the draw can honor.

    Pattern (mirror V1):
      - Read parent values from ``pools[reference_table].column(reference_column)``.
      - INSERTION-ORDER unique + dropna (V1 uses pandas ``Series.dropna().unique()``,
        which is insertion-order; a naive ``set()`` would break parity).
      - ``check_reference_pool`` runs before any value is chosen: infeasible per-parent
        bounds and a ``weighted`` weights list that does not match the real key count
        are refused, and an empty parent pool follows ``policy`` (the orphan policy of
        the relationship that covers this column, ``None`` without one): ``fail``
        refuses, ``warn`` reports through ``on_warning``, anything else stays silent.
        ``fitted`` is exempt: its allocator owns the empty-pool refusal.
      - Empty parent pool that was not refused -> ``[None] * n``.
      - Reseed ``random`` from the per-column seed (V1's exact pattern -- the
        ``categorical`` generator does the same).
      - Dispatch by ``distribution``: ``random`` (random.choice), ``sequential``
        (parent_vals[i % len]), ``weighted`` (random.choices with weights), ``fitted``
        (parent degrees from a pinned fan-out, see ``_fitted_reference``). Unknown ->
        random (V1 warns; the v2 falls through silently, values match).
      - Optional cardinality repair through the V1 thin call
        ``ColumnGenerator._apply_cardinality_bounds``, which both generators route to
        ``generation/relational``.

    The PipelineConfig ``_reference_graph_valid`` validator + topo-sort in
    ``generate_tables`` guarantee the parent is in ``pools`` by the time we get
    here; this function does not re-check existence.
    """
    pools = pools or {}
    ref_table = col["reference_table"]
    ref_column = col["reference_column"]
    distribution = col.get("distribution", "random")
    min_per = int(col.get("min_per_parent") or 0)
    max_per = int(col.get("max_per_parent") or 0)

    ref_vals = parent_keys(pools, ref_table, ref_column)

    if distribution != "fitted":
        check_reference_pool(col, ref_vals, n, policy, col.get(TABLE_KEY), on_warning)

    if col.get("unique") is True:
        from decoy_engine.generation._unique import unique_reference

        return unique_reference(col, ref_vals, n, seed, derive_key)

    if distribution == "fitted":
        # A non-empty pool refuses bounds here; an empty one reaches the allocator's own refusal.
        check_fitted_bounds(col, ref_vals)
        return _fitted_reference(col, ref_vals, n, seed, derive_key, fanout)

    if not ref_vals:
        return [None] * n

    col_seed = GenDeriveContext.for_column(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    ).base_int("py")
    # Instance-local Random: parity-preserving, thread-safe (F1 fix).
    rng = random.Random(col_seed)

    values: list[Any] = []
    for i in range(n):
        if distribution == "random":
            values.append(rng.choice(ref_vals))
        elif distribution == "sequential":
            values.append(ref_vals[i % len(ref_vals)])
        elif distribution == "weighted":
            # The pool check already refused a weights list of the wrong length.
            values.append(rng.choices(ref_vals, weights=col.get("weights") or None, k=1)[0])
        else:
            # V1 unknown -> warn + random; the v2 falls through silently (parity
            # in values, not in log lines).
            values.append(rng.choice(ref_vals))

    if min_per > 0 or max_per > 0:
        from decoy_engine.generators.columns import ColumnGenerator

        # QA-1 H6 carry (2026-06-01): pass the column-scoped rng so the
        # repair's shuffle/choices stay deterministic without touching
        # module-global random. The local `rng` above is column-scoped
        # via col_seed.
        cg = ColumnGenerator(seed=seed, derive_key=derive_key)
        values = cg._apply_cardinality_bounds(
            values,
            ref_vals,
            min_per,
            max_per,
            rng=rng,
            table=col.get(TABLE_KEY),
            column=col.get("name"),
        )
    return values
