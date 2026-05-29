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

from decoy_engine.generators.derivation import synthetic_column_seed

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
        return _sequence(col, n)
    if kind == "categorical":
        return _categorical(col, n, seed, derive_key)
    raise ValueError(
        f"generate column {col.get('name')!r}: generator type {kind!r} is not yet "
        f"implemented in this S6-ENG-2 sub-commit (faker / formula land in the next "
        f"sub-commits of S6-ENG-2)."
    )


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
