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

from collections.abc import Mapping, Sequence
from typing import Any

import pyarrow as pa

from decoy_engine.generators.derivation import TABLE_KEY, column_identity


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
    step RNG uses derive(seed, namespace, group_label) (HKDF-SHA256 + HMAC-
    SHA256); namespace = "grouped_series/<table, column identity>".
    See transforms/grouped_series.py for full citations.

    Deterministic: same seed + same generated snapshot -> same output.
    """
    import pandas as pd

    from decoy_engine.transforms.grouped_series import GroupedSeriesConfig, apply_grouped_series

    cfg_dict: dict[str, Any] = {
        "group_by": col.get("group_by", ""),
        "order_by": col.get("order_by", ""),
        "generator": col.get("generator", "cumcount"),
        "step": col.get("step", 1),
        "max_step": col.get("max_step", 10),
    }
    # Omit "start" when not provided so GroupedSeriesConfig.from_dict applies
    # its generator-specific default (0 for cumcount, 1 for monotone_walk).
    if "start" in col:
        cfg_dict["start"] = col["start"]
    config = GroupedSeriesConfig.from_dict(cfg_dict)
    group_vals = generated.get(config.group_by, [None] * n)
    order_vals = generated.get(config.order_by, list(range(n)))
    df = pd.DataFrame({config.group_by: group_vals, config.order_by: order_vals})
    # Convert seed int -> 8 bytes for the transform layer. The seed int from
    # _normalize_job_seed_int fits in 8 bytes (same as _normalize_job_seed).
    seed_bytes = seed.to_bytes(8, "big")
    namespace = f"grouped_series/{column_identity(col)}"
    return list(apply_grouped_series(config, df, seed=seed_bytes, namespace=namespace))


def _parent_anchor_cells(
    col: dict[str, Any],
    generated: dict[str, list[Any]],
    pools: Mapping[str, pa.Table],
    siblings: Sequence[Mapping[str, Any]],
) -> list[Any]:
    """The parent row's anchor for each row of a `windowed_date` column with `anchor_via`.

    The anchor's table and column come from the column's resolved input, so a same-named
    column of this table is never read. The row is found by the one parent lookup: a null
    foreign key gives a null anchor (and so a null date), and a foreign key with no parent row
    is that lookup's refusal. A parent row that is found but holds no anchor is refused here:
    the child asked for a date counted from its parent's, and there is none to count from.
    """
    from decoy_engine.config import iter_generate_input_refs
    from decoy_engine.generation.relational._parent_lookup import lookup_parent_values
    from decoy_engine.transforms.windowed_date import (
        WindowedDateError,
        WindowedDateParentAnchorNullError,
    )

    table, name, via = str(col.get(TABLE_KEY) or ""), str(col["name"]), str(col["anchor_via"])
    edges = iter_generate_input_refs({"name": table, "generate_columns": siblings}, col)
    anchor = next((e for e in edges if e.option == "anchor"), None)
    key = next((s.get("reference_column") for s in siblings if s.get("name") == via), None)
    # Plan compile refuses an anchor that resolves to no finished table; a caller that skipped
    # it gets the same fixed text as any other anchor that is not a date.
    if anchor is None or anchor.source_table not in pools or not isinstance(key, str):
        raise WindowedDateError(table=table, column=name)
    looked_up = lookup_parent_values(
        dict(pools),
        parent_table=anchor.source_table,
        key_column=key,
        value_column=anchor.source_column,
        foreign_keys=generated[via],
        child_table=table,
        child_column=via,
    )
    cells = zip(looked_up.values, looked_up.found, strict=True)
    if any(found and value is None for value, found in cells):
        raise WindowedDateParentAnchorNullError(table=table, column=name)
    return looked_up.values


def _windowed_date_generate(
    col: dict[str, Any],
    n: int,
    seed: int,
    generated: dict[str, list[Any]],
    pools: Mapping[str, pa.Table] | None = None,
    siblings: Sequence[Mapping[str, Any]] = (),
    *,
    reference_date: str | None = None,
) -> list[Any]:
    """SP-10c: windowed_date in a generate table.

    Builds a temporary DataFrame from the already-generated anchor column,
    then applies apply_windowed_date. The anchor column must be declared
    before this column in generate_columns. With ``anchor_via`` the anchor is
    the parent row's column instead (``_parent_anchor_cells``); the window and
    its per-row seeds are the same.

    Methodology: derive()-per-column namespace (HKDF-SHA256 + HMAC-SHA256)
    for per-row seed isolation; pandas Timestamp + Timedelta for date
    arithmetic. Namespace = "windowed_date/<table, column identity>".
    See transforms/windowed_date.py for full citations.

    Deterministic: same seed + same anchor values -> byte-identical output.
    """
    import pandas as pd

    from decoy_engine.transforms.windowed_date import WindowedDateConfig, apply_windowed_date

    # Build cfg_dict omitting optional keys so WindowedDateConfig.from_dict
    # applies its own defaults and validation (avoids the L2 drift where
    # hardcoding max_days=0 bypasses the "max_days required" check).
    cfg_dict: dict[str, Any] = {"anchor": col.get("anchor", "")}
    if "min_days" in col:
        cfg_dict["min_days"] = col["min_days"]
    if "max_days" in col:
        cfg_dict["max_days"] = col["max_days"]
    if "distribution" in col:
        cfg_dict["distribution"] = col["distribution"]
    config = WindowedDateConfig.from_dict(cfg_dict)
    if col.get("anchor_via") is not None:
        anchor_vals = _parent_anchor_cells(col, generated, pools or {}, siblings)
    else:
        anchor_vals = generated.get(config.anchor, [None] * n)
    if reference_date is not None:
        from decoy_engine.generation._pinned_anchor import resolve_anchor_cells

        anchor_vals = resolve_anchor_cells(anchor_vals, reference_date)
    df = pd.DataFrame({config.anchor: anchor_vals})
    seed_bytes = seed.to_bytes(8, "big")
    namespace = f"windowed_date/{column_identity(col)}"
    return apply_windowed_date(config, df, seed=seed_bytes, namespace=namespace)


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

    The namespace "group_key/<table, column identity>" isolates this column's HKDF-SHA256
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
    namespace = f"group_key/{column_identity(col)}"
    # derive() requires exactly 8 bytes as the seed. The seed int from
    # _normalize_job_seed_int fits in 8 bytes (same as _normalize_job_seed).
    seed_bytes = seed.to_bytes(8, "big")
    return apply_group_key(config, df, seed=seed_bytes, namespace=namespace)
