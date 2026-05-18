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

    # ── Sprint 4: FK preservation (item 4) ──
    #
    # Pattern: SDV HMA1 (sdv-dev/SDV, MIT). Parent-first DAG;
    # materialize parent pool; child samples with replacement.
    #
    # Read the engine-native column_relationships block off ctx
    # (runner.py:_execute_graph binds it before the topo loop).
    # Each entry shapes as:
    #   {"kind": "fk",
    #    "parent": {"node": "mask_2", "column": "customer_id"},
    #    "child":  {"node": "synth_1", "column": "customer_id"}}
    # We care about entries where child.node IS this op's node id;
    # those columns get their strategy coerced to `reference` at
    # runtime and their pool materialized from the parent.
    fk_targets: dict[str, dict[str, str]] = {}
    current_node_id = getattr(ctx, "_current_node_id", None) if ctx is not None else None
    pool_resolver = getattr(ctx, "pool_resolver", None) if ctx is not None else None
    column_relationships = (
        getattr(ctx, "column_relationships", None) if ctx is not None else None
    ) or []
    for rel in column_relationships:
        if not isinstance(rel, dict):
            continue
        parent = rel.get("parent") or {}
        child = rel.get("child") or {}
        if not isinstance(parent, dict) or not isinstance(child, dict):
            continue
        if child.get("node") != current_node_id:
            continue
        c_col = child.get("column")
        p_node = parent.get("node")
        p_col = parent.get("column")
        if c_col and p_node and p_col and c_col in columns:
            fk_targets[c_col] = {"parent_node": p_node, "parent_column": p_col}

    # Materialize the parent pools we'll need + capture any coercion
    # advisories so the manifest assembler can hydrate them at finish
    # time. reference_data keyed by parent_node so the columns
    # generator's existing _generate_reference_column can read it
    # via column_config["reference_table"] = parent_node.
    reference_data: dict[str, pd.DataFrame] = {}
    fk_metrics: dict[str, dict[str, Any]] = {}
    for c_col, target in fk_targets.items():
        if pool_resolver is None:
            # Validator should catch this at validation time
            # (column_relationships present + this op has no
            # pool_resolver implies a runner / context wiring bug).
            continue
        try:
            pool = pool_resolver(target["parent_node"], target["parent_column"])
        except Exception:
            raise  # surface to runner's translate_engine_error
        reference_data[target["parent_node"]] = pd.DataFrame({
            target["parent_column"]: pool,
        })
        # Track per-FK metrics for the manifest. _exports is hydrated
        # at finish time by the platform's evidence assembler.
        fk_metrics[c_col] = {
            "parent_node": target["parent_node"],
            "parent_column": target["parent_column"],
            "child_column": c_col,
            "pool_size": len(pool),
            "strategy_coerced": False,  # filled below
        }

    try:
        from decoy_engine.generators.columns import ColumnGenerator

        gen = ColumnGenerator(seed=seed, logger=logger, derive_key=pipeline_derive_key)
        out = upstream.copy()
        for col_name, spec in columns.items():
            col_config = dict(spec)
            col_config["name"] = col_name
            col_config.setdefault("type", col_config.pop("strategy", "faker"))

            # FK coercion: if this column is the child side of a
            # declared FK, override type to `reference` + populate
            # the reference_table / reference_column hints the
            # columns generator's _generate_reference_column reads.
            # Preserve any operator-set distribution / weights keys.
            if col_name in fk_targets:
                original_type = col_config.get("type")
                target = fk_targets[col_name]
                col_config["type"] = "reference"
                col_config["reference_table"] = target["parent_node"]
                col_config["reference_column"] = target["parent_column"]
                col_config.setdefault("distribution", "random")
                if original_type != "reference":
                    fk_metrics[col_name]["strategy_coerced"] = True
                    fk_metrics[col_name]["original_strategy"] = original_type

            out[col_name] = gen.generate_column(
                num_rows=num_rows,
                column_config=col_config,
                table_name="__graph_generate__",
                reference_data=reference_data,
            )
    except Exception as exc:
        raise OpError(f"generate op failed: {exc}") from exc

    # ── FK drop-row post-pass ──
    #
    # _generate_reference_column emits sentinel strings or None when a
    # parent value can't be resolved. Per the plan, declared-FK columns
    # drop the offending child row entirely (no sentinels in
    # production output). Build the drop mask from all FK columns +
    # filter out matching rows.
    if fk_targets:
        drop_mask = pd.Series([False] * len(out), index=out.index)
        for c_col in fk_targets:
            col = out[c_col]
            # Drop on: None / NaN, or the legacy sentinel strings the
            # generator emits when the reference table/column is
            # missing (defensive; the validator now catches most of
            # these earlier).
            sentinel_mask = col.isna()
            if col.dtype == object:
                str_col = col.astype(str)
                sentinel_mask = sentinel_mask | str_col.str.startswith(
                    ("REF_TABLE_NOT_FOUND_", "REF_COLUMN_NOT_FOUND_")
                )
            drop_mask = drop_mask | sentinel_mask
        dropped_count = int(drop_mask.sum())
        if dropped_count > 0:
            out = out[~drop_mask].reset_index(drop=True)
        # Annotate every FK with its dropped_rows count so the
        # manifest knows what fell out. Each FK is tagged with the
        # same total (we drop on the UNION of FK masks; the manifest
        # can interpret as "rows that failed at least one FK").
        for c_col in fk_targets:
            fk_metrics[c_col]["dropped_rows"] = dropped_count

    if ctx is not None and hasattr(ctx, "export"):
        ctx.export("rows_generated", int(len(out)))
        ctx.export("columns_generated", int(len(columns)))
        ctx.export("seed_used", seed)
        # Per-FK metrics for the platform-side evidence assembler.
        # Keyed by child column name so the manifest can join against
        # the declared_relationships block.
        if fk_metrics:
            ctx.export("fk_preservation", fk_metrics)

    return out
