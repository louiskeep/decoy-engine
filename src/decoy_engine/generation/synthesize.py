"""Table-from-schema synthesis (engine-v2 S6).

Produces synthetic tables from a generate-mode ``PipelineConfig``: for each generate
table (``generate_columns`` + ``row_count``, no source), build ``row_count`` rows,
each declared column filled by its per-column generator. This is the v2 analogue of
V1 ``DataGenerator`` (``decoy_engine.generators``); it is PARITY-FROZEN to V1 under a
fixed seed (Reading B) -- we reproduce V1 output, we do not extend it.

S6-ENG-1 landed the spine + the ``sequence`` generator. S6-ENG-2 adds parity-frozen
``categorical`` (and on the next sub-commits, ``faker`` / ``formula``); S6-ENG-3 adds
FK-aware generation (mint-a-pool); S6-ENG-4 the seed / derive-key determinism envelope.

Parity seeding uses V1's ``synthetic_column_seed`` (``decoy_engine.generators.derivation``)
directly so the per-column seed is byte-identical to V1 ``ColumnGenerator._column_seed``
under the same ``derive_key`` (always ``None`` in ENG-2; ENG-4 wires the real key).
"""

from __future__ import annotations

import random
from typing import Any

import pyarrow as pa
from faker import Faker

from decoy_engine.generators.derivation import synthetic_column_seed
from decoy_engine.internal.faker_setup import get_faker_providers, make_faker

_DEFAULT_SEED = 42


def generate_tables(
    config: dict[str, Any], derive_key: Any = None
) -> dict[str, pa.Table]:
    """Build one Arrow table per generate table in ``config``.

    ``config`` is a validated, ``model_dump``-ed generate-mode ``PipelineConfig``.
    Returns ``{table_name: pa.Table}`` for every table that declares
    ``generate_columns`` (mask tables, if any, are skipped). ``derive_key`` is the
    pipeline-bound key resolver V1 ``ColumnGenerator`` threads -- ALWAYS ``None`` in
    S6-ENG-2 (parity-tested against V1 seed-only path); S6-ENG-4 wires the real
    ``pipeline_derive_key`` so generation + masking share one determinism envelope.

    The platform run path (S6-PLT) writes these through the same ``write_v2_outputs``
    + ``build_v2_target_node_runs`` path the mask spine uses.
    """
    seed = int((config.get("global_settings") or {}).get("seed", _DEFAULT_SEED))
    out: dict[str, pa.Table] = {}
    for table in config.get("tables") or []:
        gcols = table.get("generate_columns") or []
        if not gcols:
            continue  # a mask table; not this op's concern
        n = int(table.get("row_count") or 0)
        data = {
            col["name"]: _generate_column(col, n, seed, derive_key) for col in gcols
        }
        out[table["name"]] = pa.table(data)
    return out


def _generate_column(
    col: dict[str, Any], n: int, seed: int, derive_key: Any = None
) -> list[Any]:
    """Dispatch a generate column to its generator by ``type`` (mirrors V1
    ``ColumnGenerator.generators``)."""
    kind = col.get("type")
    if kind == "sequence":
        values: list[Any] = _sequence(col, n)
    elif kind == "categorical":
        values = _categorical(col, n, seed, derive_key)
    elif kind == "faker":
        values = _faker(col, n, seed, derive_key)
    elif kind == "formula":
        values = _formula(col, n, seed, derive_key)
    else:
        # The Literal on GenerateColumnConfig.type rejects anything outside this set
        # at validation; this branch is the defensive fallback for callers that
        # bypass validation (e.g. an unvalidated dict).
        raise ValueError(
            f"generate column {col.get('name')!r}: unexpected generator type {kind!r}"
        )
    return _apply_null_probability(values, col, seed, derive_key)


def _sequence(col: dict[str, Any], n: int) -> list[str]:
    """Sequential string values, parity-frozen vs V1 ``_generate_sequence_column``
    (``columns.py:305-319``).

    V1 ALWAYS wraps every value through ``f"{prefix}{value_str}{suffix}"`` (S6-ENG-1
    gate finding M1: the ENG-1 spine returned ints when unformatted; corrected here).
    Returns strings in every configuration. ``pad_length`` zero-fills the numeric
    body; ``prefix`` / ``suffix`` wrap it.
    """
    start = int(col.get("start", 1))
    step = int(col.get("step", 1))
    prefix = str(col.get("prefix", ""))
    suffix = str(col.get("suffix", ""))
    pad = int(col.get("pad_length", 0))
    out: list[str] = []
    for i in range(n):
        value = start + i * step
        value_str = str(value).zfill(pad) if pad > 0 else str(value)
        out.append(f"{prefix}{value_str}{suffix}")
    return out


def _categorical(
    col: dict[str, Any], n: int, seed: int, derive_key: Any = None
) -> list[Any]:
    """Weighted / uniform random choice over ``categories``, parity-frozen vs V1
    ``_generate_categorical_column`` (``columns.py:321-353``).

    V1 reseeds ``random`` from the column seed (so output is stable across runs +
    order-independent across columns when keyed), then ``random.choices(categories,
    weights=weights, k=num_rows)``. ``weights`` is optional; when omitted the choice
    is uniform. We reuse V1 ``synthetic_column_seed`` for the per-column seed (Dennis
    S6-ENG-2 plan: import V1's helper, do not reinvent), so seed-only output is
    byte-identical to V1's under the same ``seed`` + ``derive_key=None``.
    """
    cats = col.get("categories", ["Category A", "Category B"])
    weights = col.get("weights")  # optional; None -> uniform
    col_seed = synthetic_column_seed(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    )
    random.seed(col_seed)
    return random.choices(cats, weights=weights, k=n)


def _faker(
    col: dict[str, Any], n: int, seed: int, derive_key: Any = None
) -> list[Any]:
    """Faker-driven values, parity-frozen vs V1 ``_generate_faker_column``
    (``columns.py:205-276``).

    Pattern (mirror V1): pick the Faker instance (fresh per-locale when ``locale``
    is set, otherwise a shared instance), look up the provider by ``faker_type``
    (default ``"word"``, fall back to ``"word"`` for unknown types), then per row
    seed ``random`` AND ``faker_inst.seed_instance`` with ``col_seed + i`` and call
    ``provider_func(**faker_kwargs)``. The per-row seed_instance override means the
    initial instance seed does not affect output -- parity holds independent of how
    the instance was constructed.

    ``faker_kwargs`` is optional; non-dict values are dropped (matches V1's silent
    drop, ``columns.py:253-259``).
    """
    faker_type = col.get("faker_type", "word")
    locale = col.get("locale")
    if locale:
        faker_inst = make_faker(locale)
    else:
        faker_inst = Faker()
        faker_inst.seed_instance(seed)
    providers = get_faker_providers(faker_inst)
    provider_func = providers.get(faker_type) or providers["word"]
    raw_kwargs = col.get("faker_kwargs") or {}
    faker_kwargs = raw_kwargs if isinstance(raw_kwargs, dict) else {}
    col_seed = synthetic_column_seed(
        derive_key=derive_key, column_config=col, fallback_seed=seed
    )
    out: list[Any] = []
    for i in range(n):
        row_seed = col_seed + i
        random.seed(row_seed)
        faker_inst.seed_instance(row_seed)
        out.append(provider_func(**faker_kwargs))
    return out


def _apply_null_probability(
    values: list[Any], col: dict[str, Any], seed: int, derive_key: Any = None
) -> list[Any]:
    """Apply V1's ``null_probability`` post-process (``columns.py:174-187``): per-row
    seeded coin-flip; same column + same row -> same null/non-null decision across
    runs. No-op when ``null_probability`` is unset or 0. Used uniformly by every
    generator (V1 applies it generically in ``generate_column``)."""
    null_prob = float(col.get("null_probability") or 0.0)
    if null_prob <= 0:
        return values
    # V1 calls `_column_seed(column_name)` WITHOUT the column_config for the null
    # injection (columns.py:183), so the seed is computed against just the name --
    # different from the generator's full-config seed because synthetic_column_seed
    # fingerprints strategy/config. Mirror V1's null-injection seed exactly so the
    # null/non-null row positions are byte-identical under the same seed.
    null_seed_cfg: dict[str, Any] = {"name": col["name"]}
    col_seed = synthetic_column_seed(
        derive_key=derive_key, column_config=null_seed_cfg, fallback_seed=seed
    )
    out = list(values)
    for i in range(len(out)):
        random.seed(col_seed + i)
        if random.random() < null_prob:
            out[i] = None
    return out


def _formula(
    col: dict[str, Any], n: int, seed: int, derive_key: Any = None
) -> list[Any]:
    """Python-expression-driven values, parity-frozen vs V1
    ``_generate_formula_column`` (``columns.py:974+``).

    V1's structure (mirrored here):
      - empty ``formula`` -> warn + None series (we just return Nones).
      - ``references: [...]`` set -> DEFER to V1's post-pass
        (``DataGenerator._process_referenced_formulas``); the per-column generator
        returns ``[None] * n`` placeholders. v2 returns the same placeholders;
        cross-column-reference formulas land in a later sprint (alongside the
        v2 post-pass plumbing).
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
    if not formula or references:
        # V1: warn or defer -- both return a None series of length n.
        return [None] * n
    from decoy_engine.generators.columns import ColumnGenerator

    cg = ColumnGenerator(seed=seed, derive_key=derive_key)
    series = cg._eval_formula_inline(
        n, formula, col.get("name", "unnamed_column"), col
    )
    return series.tolist()
