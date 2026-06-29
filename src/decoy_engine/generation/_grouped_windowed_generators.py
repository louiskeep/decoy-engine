"""SP-10c generate-path helpers for grouped_series, windowed_date, group_key.

Extracted from ``generation/synthesize.py`` to keep that module under its 600
LOC ceiling (best-practices section 4.1). These functions mirror the lazy-import
pattern already used for ``_referenced_formula.fill_referenced_formula_columns``
and ``_fidelity_gate.warn_on_low_fidelity``.

Called from ``generation/synthesize._generate_column`` when:
  kind == "grouped_series"  -> _grouped_series_generate
  kind == "windowed_date"   -> _windowed_date_generate
  kind == "group_key"       -> _group_key_generate
"""

from __future__ import annotations

from typing import Any


def _grouped_series_generate(
    col: dict[str, Any],
    n: int,
    seed: int,
    generated: dict[str, list[Any]],
) -> list[Any]:
    """SP-10c: grouped_series in a generate table.

    Builds a temporary DataFrame from the already-generated group_by and
    order_by columns, then applies apply_grouped_series. The group_by and
    order_by columns must be declared before this column in generate_columns
    (declared-order sequential semantics, same as derived and statistical).

    Methodology: pandas groupby cumcount / SDV per-group sequencing. Per-group
    step RNG uses numpy.random.default_rng seeded from SHA-256(seed || group).
    See transforms/grouped_series.py for full citations.

    Deterministic: same seed + same generated snapshot -> same output.
    """
    import pandas as pd

    from decoy_engine.transforms.grouped_series import GroupedSeriesConfig, apply_grouped_series

    config = GroupedSeriesConfig.from_dict(
        {
            "group_by": col.get("group_by", ""),
            "order_by": col.get("order_by", ""),
            "generator": col.get("generator", "cumcount"),
            "start": col.get("start", 0),
            "step": col.get("step", 1),
            "max_step": col.get("max_step", 10),
        }
    )
    group_vals = generated.get(config.group_by, [None] * n)
    order_vals = generated.get(config.order_by, list(range(n)))
    df = pd.DataFrame({config.group_by: group_vals, config.order_by: order_vals})
    # Convert seed int -> 8 bytes for the transform layer. The seed int from
    # _normalize_job_seed_int fits in 8 bytes (same as _normalize_job_seed).
    seed_bytes = seed.to_bytes(8, "big")
    return list(apply_grouped_series(config, df, seed=seed_bytes))


def _windowed_date_generate(
    col: dict[str, Any],
    n: int,
    seed: int,
    generated: dict[str, list[Any]],
) -> list[Any]:
    """SP-10c: windowed_date in a generate table.

    Builds a temporary DataFrame from the already-generated anchor column,
    then applies apply_windowed_date. The anchor column must be declared
    before this column in generate_columns.

    Methodology: pandas Timestamp + Timedelta date arithmetic; numpy seeded
    per-row offset sampling. See transforms/windowed_date.py for full citations.

    Deterministic: same seed + same anchor values -> byte-identical output.
    """
    import pandas as pd

    from decoy_engine.transforms.windowed_date import WindowedDateConfig, apply_windowed_date

    config = WindowedDateConfig.from_dict(
        {
            "anchor": col.get("anchor", ""),
            "min_days": col.get("min_days", 0),
            "max_days": col.get("max_days", 0),
            "distribution": col.get("distribution", "uniform"),
        }
    )
    anchor_vals = generated.get(config.anchor, [None] * n)
    df = pd.DataFrame({config.anchor: anchor_vals})
    seed_bytes = seed.to_bytes(8, "big")
    return apply_windowed_date(config, df, seed=seed_bytes)


def _group_key_generate(
    col: dict[str, Any],
    n: int,
    seed: int,
    generated: dict[str, list[Any]],
) -> list[Any]:
    """SP-10c: group_key in a generate table.

    Builds a temporary DataFrame from the already-generated group_by column,
    then applies apply_group_key. The group_by column must be declared before
    this column in generate_columns (declared-order sequential semantics).

    The namespace "group_key/<column_name>" isolates this column's HKDF-SHA256
    derivation from other group_key columns in the same job.

    Methodology: HKDF-SHA256 + HMAC-SHA256 keyed per-group identifier via
    decoy_engine.determinism._derive.derive(). Same hash-for-joinability
    primitive used throughout the engine. See transforms/group_key.py for
    full citations.

    Deterministic: same seed + same group_by values -> byte-identical keys.
    """
    import pandas as pd

    from decoy_engine.transforms.group_key import GroupKeyConfig, apply_group_key

    config = GroupKeyConfig.from_dict(
        {
            "group_by": col.get("group_by", ""),
            "length": col.get("length", 16),
            "prefix": col.get("prefix", ""),
        }
    )
    group_vals = generated.get(config.group_by, [None] * n)
    df = pd.DataFrame({config.group_by: group_vals})
    # Namespace isolates this column's derivation from other group_key columns.
    col_name = col.get("name", "group_key_col")
    namespace = f"group_key/{col_name}"
    # derive() requires exactly 8 bytes as the seed. The seed int from
    # _normalize_job_seed_int fits in 8 bytes (same as _normalize_job_seed).
    seed_bytes = seed.to_bytes(8, "big")
    return apply_group_key(config, df, seed=seed_bytes, namespace=namespace)
