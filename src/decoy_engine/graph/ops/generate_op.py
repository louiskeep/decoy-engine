"""generate — produce synthetic rows.

Config:
    row_count: int           - how many rows to generate (required)
    seed: int                - default 42
    columns:
      <column_name>:
        strategy: 'faker' | 'sequence' | 'categorical' | 'formula'
        # ...strategy-specific keys

Two arities supported:
    INPUT_ARITY (0, 1)
        - 0 inputs: pure source — emit `row_count` synthetic rows
        - 1 input: replace the input's columns with generated values, keeping
          row count from the upstream df (config.row_count is ignored if input present)
"""

from typing import Any

import pandas as pd

from decoy_engine.graph.ops._base import OpError
from decoy_engine.internal.validator import ValidationError

KIND = "generate"
# Generation uses per-row Faker / scipy callbacks; stays on pandas. FK-aware
# generators (reference, foreign_key) may move to a polars-orchestrated
# hybrid in the Phase 9 follow-up.
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (0, 1)
OUTPUT_KIND = "stream"

_VALID_TYPES = {"faker", "sequence", "categorical", "formula"}


def validate_config(config: dict[str, Any]) -> None:
    # Empty / missing `columns` is valid in column-replacer mode (1 input):
    # it just means "leave every upstream column untouched", a no-op. In
    # pure-source mode (0 inputs) the user can save with no columns and
    # the run will produce a row_count-tall df with no generated columns,
    # which is silly but not malformed. Validator stays structural.
    columns = config.get("columns") or {}
    if not isinstance(columns, dict):
        raise ValidationError(
            "'columns' must be a mapping", "config.columns"
        )
    if "row_count" in config:
        rc = config["row_count"]
        if not isinstance(rc, int) or rc <= 0:
            raise ValidationError(
                "'row_count' must be a positive integer", "config.row_count"
            )
    for col_name, spec in columns.items():
        if not isinstance(spec, dict):
            raise ValidationError(
                f"column {col_name!r} spec must be a mapping",
                f"config.columns.{col_name}",
            )
        ctype = spec.get("strategy") or spec.get("type")
        if ctype not in _VALID_TYPES:
            raise ValidationError(
                f"unsupported type {ctype!r} (one of {sorted(_VALID_TYPES)})",
                f"config.columns.{col_name}.strategy",
            )


def apply(inputs, config, ctx) -> pd.DataFrame:
    # Tolerate missing/empty columns — see validate_config.
    columns = config.get("columns") or {}
    seed = int(config.get("seed", 42))

    if inputs:
        upstream = inputs[0]
        num_rows = len(upstream)
    else:
        num_rows = int(config.get("row_count") or 100)
        upstream = pd.DataFrame(index=range(num_rows))

    row_limit = config.get("__preview_row_limit")
    if row_limit:
        num_rows = min(num_rows, int(row_limit))
        upstream = upstream.head(num_rows)

    logger = ctx.logger if ctx is not None else None
    # `pipeline_derive_key` is the generate-side key resolver. When the
    # platform's admin policy says "no pipeline key", this is None and the
    # ColumnGenerator falls back to seed-based RNG (random per run); when
    # set, per-column seeds are HKDF-derived so the same key + same row
    # context always yields the same bytes.
    pipeline_derive_key = getattr(ctx, "pipeline_derive_key", None) if ctx is not None else None

    try:
        from decoy_engine.generators.columns import ColumnGenerator

        gen = ColumnGenerator(seed=seed, logger=logger, derive_key=pipeline_derive_key)
        out = upstream.copy()
        for col_name, spec in columns.items():
            col_config = dict(spec)
            col_config["name"] = col_name
            col_config.setdefault("type", col_config.pop("strategy", "faker"))
            out[col_name] = gen.generate_column(
                num_rows=num_rows,
                column_config=col_config,
                table_name="__graph_generate__",
                reference_data={},
            )
    except Exception as exc:
        raise OpError(f"generate op failed: {exc}") from exc

    if ctx is not None and hasattr(ctx, "export"):
        ctx.export("rows_generated", int(len(out)))
        ctx.export("columns_generated", int(len(columns)))
        ctx.export("seed_used", seed)

    return out
