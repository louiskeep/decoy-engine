"""Table-from-schema synthesis (engine-v2 S6).

Produces synthetic tables from a generate-mode ``PipelineConfig``: for each generate
table (``generate_columns`` + ``row_count``, no source), build ``row_count`` rows,
each declared column filled by its per-column generator. This is the v2 analogue of
V1 ``DataGenerator`` (``decoy_engine.generators``); it is PARITY-FROZEN to V1 under a
fixed seed (Reading B) -- we reproduce V1 output, we do not extend it.

S6-ENG-1 lands the spine + the ``sequence`` generator (enough for the gate: a
single-column generate config produces ``row_count`` rows). S6-ENG-2 adds the
remaining parity-frozen generators (``faker`` / ``categorical`` / ``formula``);
S6-ENG-3 adds FK-aware generation (mint-a-pool); S6-ENG-4 the seed / derive-key
determinism envelope. Each is parity-frozen against its V1 ``ColumnGenerator`` method.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa

_DEFAULT_SEED = 42


def generate_tables(config: dict[str, Any]) -> dict[str, pa.Table]:
    """Build one Arrow table per generate table in ``config``.

    ``config`` is a validated, ``model_dump``-ed generate-mode ``PipelineConfig``.
    Returns ``{table_name: pa.Table}`` for every table that declares
    ``generate_columns`` (mask tables, if any, are skipped). The platform run path
    (S6-PLT) writes these through the same ``write_v2_outputs`` +
    ``build_v2_target_node_runs`` path the mask spine uses.
    """
    seed = int((config.get("global_settings") or {}).get("seed", _DEFAULT_SEED))
    out: dict[str, pa.Table] = {}
    for table in config.get("tables") or []:
        gcols = table.get("generate_columns") or []
        if not gcols:
            continue  # a mask table; not this op's concern
        n = int(table.get("row_count") or 0)
        data = {col["name"]: _generate_column(col, n, seed) for col in gcols}
        out[table["name"]] = pa.table(data)
    return out


def _generate_column(col: dict[str, Any], n: int, seed: int) -> list[Any]:
    """Dispatch a generate column to its generator by ``type`` (mirrors V1
    ``ColumnGenerator.generators``)."""
    kind = col.get("type")
    if kind == "sequence":
        return _sequence(col, n)
    raise ValueError(
        f"generate column {col.get('name')!r}: unsupported generator type {kind!r} "
        f"(S6-ENG-1 ships 'sequence'; faker/categorical/formula land in S6-ENG-2)"
    )


def _sequence(col: dict[str, Any], n: int) -> list[Any]:
    """Sequential values (mirrors V1 ``_generate_sequence_column``): ``start`` +
    ``i * step``, optionally formatted with ``prefix`` / ``suffix`` / ``pad_length``.
    Returns ints when unformatted, strings when any formatting is requested.
    """
    start = int(col.get("start", 1))
    step = int(col.get("step", 1))
    prefix = str(col.get("prefix", ""))
    suffix = str(col.get("suffix", ""))
    pad = int(col.get("pad_length", 0))
    formatted = bool(prefix or suffix or pad)
    out: list[Any] = []
    for i in range(n):
        num = start + i * step
        if formatted:
            body = str(num).zfill(pad) if pad else str(num)
            out.append(f"{prefix}{body}{suffix}")
        else:
            out.append(num)
    return out
