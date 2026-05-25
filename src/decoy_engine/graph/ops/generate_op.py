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

import os
from typing import Any

import pandas as pd

from decoy_engine.errors import ValidationError
from decoy_engine.graph.ops._base import OpError

KIND = "generate"
# Generation uses per-row Faker / scipy callbacks; stays on pandas. FK-aware
# generators (reference, foreign_key) may move to a polars-orchestrated
# hybrid in the Phase 9 follow-up.
NATIVE_ENGINE = "pandas"
INPUT_ARITY: tuple[int, int | None] = (0, 1)
OUTPUT_KIND = "stream"

_VALID_TYPES = {"faker", "sequence", "categorical", "formula", "distribution"}
# V2 Phase 3 D6: 'distribution' strategy samples from a snapshot dict
# whose shape matches decoy_engine.quality.snapshot output. Kind
# dispatch (numeric / categorical / datetime) lives inside the
# generator (`ColumnGenerator._generate_distribution_column`); the
# op-side validator below only checks the top-level snapshot shape.
_DISTRIBUTION_VALID_KINDS = {"numeric", "categorical", "datetime"}


def validate_config(config: dict[str, Any]) -> None:
    # Empty / missing `columns` is valid in column-replacer mode (1 input):
    # it just means "leave every upstream column untouched", a no-op. In
    # pure-source mode (0 inputs) the user can save with no columns and
    # the run will produce a row_count-tall df with no generated columns,
    # which is silly but not malformed. Validator stays structural.
    columns = config.get("columns") or {}
    if not isinstance(columns, dict):
        raise ValidationError("'columns' must be a mapping", "config.columns")
    if "row_count" in config:
        rc = config["row_count"]
        if not isinstance(rc, int) or rc <= 0:
            raise ValidationError("'row_count' must be a positive integer", "config.row_count")
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
        # D6: distribution columns must carry a snapshot dict with a
        # known kind. Sampler shape inside the snapshot (bin_edges,
        # top_values, etc.) is left to the generator to handle
        # defensively at apply time — operators may paste partial
        # snapshots while iterating, and the generator already logs
        # + falls back to nulls cleanly. Validator catches the
        # obvious "wrong kind" / "missing snapshot" cases here so
        # they surface at edit time, not at run time.
        if ctype == "distribution":
            snap = spec.get("snapshot")
            if not isinstance(snap, dict):
                raise ValidationError(
                    f"column {col_name!r} strategy 'distribution' requires "
                    "a 'snapshot' dict",
                    f"config.columns.{col_name}.snapshot",
                )
            kind = snap.get("kind")
            if kind not in _DISTRIBUTION_VALID_KINDS:
                raise ValidationError(
                    f"column {col_name!r} distribution snapshot.kind {kind!r} "
                    f"must be one of {sorted(_DISTRIBUTION_VALID_KINDS)}",
                    f"config.columns.{col_name}.snapshot.kind",
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
    fk_targets: dict[str, dict[str, Any]] = {}
    # self_ref_targets[child_col] -> parent_col (within this same op).
    # Resolved at apply time from in-flight `out` instead of via
    # pool_resolver, since the parent column is being produced in this
    # same invocation and isn't cached anywhere yet.
    self_ref_targets: dict[str, str] = {}
    # m2m_specs: list of m2m relationships where junction.node is
    # this op. Each spec carries left + right parent pool refs +
    # the two junction column names to emit. Processed AFTER the
    # standard column loop so we have all single-column outputs in
    # `out` first (rare but possible for the junction columns to
    # appear in `columns` with their own placeholder strategies).
    m2m_specs: list[dict[str, Any]] = []
    # multi_parent_targets[child_col] -> list of (parent_node, parent_col)
    # tuples. The runtime samples a tuple of (parent_val_1, parent_val_2,
    # ...) for each child row and concatenates with '|' so the join SQL
    # can later match on the composite key (engine-side: we materialize
    # the joint pool as zipped tuples).
    multi_parent_targets: dict[str, list[tuple[str, str]]] = {}
    current_node_id = getattr(ctx, "_current_node_id", None) if ctx is not None else None
    pool_resolver = getattr(ctx, "pool_resolver", None) if ctx is not None else None
    column_relationships = (
        getattr(ctx, "column_relationships", None) if ctx is not None else None
    ) or []
    for rel in column_relationships:
        if not isinstance(rel, dict):
            continue
        kind = rel.get("kind", "fk")
        # m2m: this op is the junction node?
        if kind == "m2m":
            junction = rel.get("junction") or {}
            if isinstance(junction, dict) and junction.get("node") == current_node_id:
                m2m_specs.append(rel)
            continue
        parent = rel.get("parent") or {}
        child = rel.get("child") or {}
        if not isinstance(child, dict):
            continue
        if child.get("node") != current_node_id:
            continue
        c_col = child.get("column")
        if not c_col or c_col not in columns:
            continue
        # Multi-parent FK: parent is a list of {node, column}.
        if isinstance(parent, list):
            specs: list[tuple[str, str]] = [
                (str(p["node"]), str(p["column"]))
                for p in parent
                if isinstance(p, dict) and p.get("node") and p.get("column")
            ]
            if specs:
                multi_parent_targets[c_col] = specs
            continue
        if not isinstance(parent, dict):
            continue
        # Custom-provider parent shape (tier-4 audit, 2026-05-20):
        # parent: {custom_provider: <name>} sources the FK pool from a
        # registered list-backed custom Faker provider instead of a
        # pipeline node's output. Bypasses the graph topology check
        # (custom providers aren't in the DAG) + column-presence check
        # (no column on a custom provider).
        custom_provider = parent.get("custom_provider")
        if custom_provider:
            target: dict[str, Any] = {"custom_provider": custom_provider}
            if rel.get("distribution"):
                target["distribution"] = rel["distribution"]
            if rel.get("weights"):
                target["weights"] = rel["weights"]
            if "min_per_parent" in rel:
                target["min_per_parent"] = rel["min_per_parent"]
            if "max_per_parent" in rel:
                target["max_per_parent"] = rel["max_per_parent"]
            fk_targets[c_col] = target
            continue
        p_node = parent.get("node")
        p_col = parent.get("column")
        if not p_node or not p_col:
            continue
        # Self-reference: parent is this same node. Defer pool lookup
        # to apply time when out[p_col] has been produced.
        if p_node == current_node_id:
            if p_col == c_col:
                # Cycle — validator should have rejected; skip
                # defensively at runtime.
                continue
            self_ref_targets[c_col] = p_col
            continue
        # Capture distribution / weights / cardinality knobs from the
        # FK entry so the runtime path can honor them. Defaults:
        # distribution=random, no weights, no cardinality bounds. The
        # platform-side picker writes these into the column_relationships
        # entry; the engine threads them down into ColumnGenerator's
        # column_config under the same names the columns generator
        # already reads (see _generate_reference_column).
        target = {
            "parent_node": p_node,
            "parent_column": p_col,
        }
        if rel.get("distribution"):
            target["distribution"] = rel["distribution"]
        if rel.get("weights"):
            target["weights"] = rel["weights"]
        if "min_per_parent" in rel:
            target["min_per_parent"] = rel["min_per_parent"]
        if "max_per_parent" in rel:
            target["max_per_parent"] = rel["max_per_parent"]
        fk_targets[c_col] = target

    # Materialize the parent pools we'll need + capture any coercion
    # advisories so the manifest assembler can hydrate them at finish
    # time. reference_data keyed by parent_node so the columns
    # generator's existing _generate_reference_column can read it
    # via column_config["reference_table"] = parent_node.
    reference_data: dict[str, pd.DataFrame] = {}
    fk_metrics: dict[str, dict[str, Any]] = {}
    for c_col, target in fk_targets.items():
        # Custom-provider FK: source the pool from the registered
        # custom Faker provider's values list (tier-4 audit). The
        # provider must be a list-backed provider (registered via
        # register_faker_list_provider / load_custom_providers /
        # platform-side sync_db_custom_faker_providers). Closure-only
        # providers can't surface their pool — they raise the same
        # EmptyParentPoolError shape so the operator sees a clear
        # signal in the manifest + advisory.
        if "custom_provider" in target:
            from decoy_engine.internal.faker_setup import get_custom_faker_provider_values

            pname = target["custom_provider"]
            pool = get_custom_faker_provider_values(pname)
            if not pool:
                from decoy_engine.errors import EmptyParentPoolError

                raise EmptyParentPoolError(
                    f"custom provider {pname!r} has no list-backed values "
                    f"(provider not registered or registered as a closure-only "
                    f"function — list-backed registration required for FK "
                    f"pool source)",
                    parent_node=f"@custom:{pname}",
                    parent_column="",
                )
            # Use a synthetic node-id key so the reference_data lookup
            # below + the column generator's `reference_table` key
            # stay self-consistent without colliding with real node
            # ids. The @custom: prefix is invalid in YAML node ids
            # (validators reject @ in identifiers), so collision is
            # impossible.
            synth_key = f"@custom:{pname}"
            reference_data[synth_key] = pd.DataFrame({"value": pool})
            target["parent_node"] = synth_key
            target["parent_column"] = "value"
            fk_metrics[c_col] = {
                "parent_node": synth_key,
                "parent_column": "value",
                "child_column": c_col,
                "pool_size": len(pool),
                "strategy_coerced": False,
                "kind": "custom_provider",
                "custom_provider": pname,
            }
            continue
        if pool_resolver is None:
            # Validator should catch this at validation time
            # (column_relationships present + this op has no
            # pool_resolver implies a runner / context wiring bug).
            continue
        try:
            pool = pool_resolver(target["parent_node"], target["parent_column"])
        except Exception:
            raise  # surface to runner's translate_engine_error
        reference_data[target["parent_node"]] = pd.DataFrame(
            {
                target["parent_column"]: pool,
            }
        )
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

        # Instance-wide default Faker locale (platform-supplied via
        # AppSettings.default_faker_locale). Per-column `locale` keys
        # still override at the column level — this only affects
        # columns that didn't pick their own.
        instance_locale = getattr(ctx, "instance_default_locale", None) if ctx is not None else None
        gen = ColumnGenerator(
            seed=seed,
            logger=logger,
            derive_key=pipeline_derive_key,
            instance_default_locale=instance_locale,
        )
        out = upstream.copy()
        # Two-pass column iteration to support self-reference. Pass 1
        # produces every column EXCEPT self-FK children (we don't know
        # the pool yet because the parent column may also be in this
        # node's output). Pass 2 fills the self-FK children using the
        # just-produced parent values as the pool. Validator's column-
        # cycle check ensures we don't have (a -> b, b -> a) within
        # one node, so pass 2 can always resolve.
        for col_name, spec in columns.items():
            if col_name in self_ref_targets:
                continue  # defer to pass 2
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
                # Distribution control: precedence is rel-level >
                # column-level cfg > default 'random'. Same for
                # weights + cardinality bounds.
                if "distribution" in target:
                    col_config["distribution"] = target["distribution"]
                else:
                    col_config.setdefault("distribution", "random")
                if "weights" in target:
                    col_config["weights"] = target["weights"]
                if "min_per_parent" in target:
                    col_config["min_per_parent"] = target["min_per_parent"]
                if "max_per_parent" in target:
                    col_config["max_per_parent"] = target["max_per_parent"]
                if original_type != "reference":
                    fk_metrics[col_name]["strategy_coerced"] = True
                    fk_metrics[col_name]["original_strategy"] = original_type

            # Multi-parent FK: child draws a composite (left, right, ...)
            # value joined with '|'. Pool built from zipped parent pools.
            if col_name in multi_parent_targets:
                parent_pools = []
                parent_node_keys = []
                missing = False
                for p_node, p_col in multi_parent_targets[col_name]:
                    if pool_resolver is None:
                        missing = True
                        break
                    try:
                        parent_pools.append(pool_resolver(p_node, p_col))
                        parent_node_keys.append(f"{p_node}.{p_col}")
                    except Exception:
                        raise
                if missing or not parent_pools:
                    # Fall through to whatever the original strategy
                    # was; engine drops rows post-pass if values
                    # don't resolve.
                    pass
                else:
                    min_len = min(len(p) for p in parent_pools)
                    composite_pool = [
                        "|".join(str(p[i]) for p in parent_pools) for i in range(min_len)
                    ]
                    composite_key = "__multi_parent__"
                    reference_data[composite_key] = pd.DataFrame(
                        {
                            composite_key: composite_pool,
                        }
                    )
                    col_config["type"] = "reference"
                    col_config["reference_table"] = composite_key
                    col_config["reference_column"] = composite_key
                    col_config.setdefault("distribution", "random")
                    fk_metrics[col_name] = {
                        "kind": "multi_parent",
                        "parents": parent_node_keys,
                        "child_column": col_name,
                        "pool_size": len(composite_pool),
                        "strategy_coerced": True,
                    }

            out[col_name] = gen.generate_column(
                num_rows=num_rows,
                column_config=col_config,
                table_name="__graph_generate__",
                reference_data=reference_data,
            )

        # Pass 2: self-FK children. Each child draws from its parent
        # column's just-produced values in `out`. Same HMAC-keyed
        # pick semantics as cross-node FK; the only difference is
        # where the pool comes from.
        for col_name, parent_col in self_ref_targets.items():
            spec = columns[col_name]
            col_config = dict(spec)
            col_config["name"] = col_name
            col_config.setdefault("type", col_config.pop("strategy", "faker"))
            original_type = col_config.get("type")
            self_ref_pool = list(out[parent_col].dropna().tolist())
            if not self_ref_pool:
                # Parent column produced no non-null values; engine
                # will fall through to the original strategy. Skip
                # the FK coercion entirely so the column at least
                # generates *something*.
                out[col_name] = gen.generate_column(
                    num_rows=num_rows,
                    column_config=col_config,
                    table_name="__graph_generate__",
                    reference_data=reference_data,
                )
                continue
            self_ref_key = f"__self_ref__{parent_col}"
            reference_data[self_ref_key] = pd.DataFrame(
                {
                    parent_col: self_ref_pool,
                }
            )
            col_config["type"] = "reference"
            col_config["reference_table"] = self_ref_key
            col_config["reference_column"] = parent_col
            col_config.setdefault("distribution", "random")
            fk_metrics[col_name] = {
                "kind": "self_reference",
                "parent_node": current_node_id,
                "parent_column": parent_col,
                "child_column": col_name,
                "pool_size": len(self_ref_pool),
                "strategy_coerced": original_type != "reference",
                "original_strategy": original_type if original_type != "reference" else None,
            }
            out[col_name] = gen.generate_column(
                num_rows=num_rows,
                column_config=col_config,
                table_name="__graph_generate__",
                reference_data=reference_data,
            )

        # Pass 3: m2m junction emission. For each spec where junction.node
        # is THIS op, sample (left, right) pairs from the parent pools
        # per pool_strategy and write to junction.columns.
        for spec in m2m_specs:
            _emit_m2m_junction(
                spec=spec,
                out=out,
                pool_resolver=pool_resolver,
                num_rows=num_rows,
                fk_metrics=fk_metrics,
                current_node_id=current_node_id,
                logger=logger,
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

    # ── PK uniqueness check ──
    #
    # For columns with `primary_key: true` in their config, scan the
    # output for duplicates. Tier-1 audit (2026-05-20) flipped the
    # default from lenient (log + warn) to strict (raise) — a non-unique
    # PK breaks join semantics downstream (same key identifies multiple
    # rows), so analytics pipelines shouldn't silently ship them.
    #
    # Opt-out: DECOY_PK_LENIENT=1 reverts to the old behavior (log a
    # warning, emit the metric to the manifest, continue). Useful for
    # one-off scrubs where the operator knows the collisions are fine
    # (e.g. faker-based PK on a tiny demo dataset).
    #
    # Either way the metric exports to the evidence manifest so
    # downstream auditors see the collision count. Common causes:
    # small row_count with faker strategy, truncated hash with too
    # few hex chars, categorical strategy on a PK (always a mistake).
    pk_lenient = os.environ.get("DECOY_PK_LENIENT", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    pk_metrics: dict[str, dict[str, Any]] = {}
    pk_duplicate_failures: list[tuple[str, int, int, str | None]] = []
    for col_name, spec in columns.items():
        if not isinstance(spec, dict) or not spec.get("primary_key"):
            continue
        if col_name not in out.columns:
            continue
        col = out[col_name]
        non_null = col.dropna()
        n_total = len(non_null)
        n_unique = non_null.nunique()
        dupes = n_total - n_unique
        pk_metrics[col_name] = {
            "primary_key": True,
            "total_non_null": int(n_total),
            "unique_values": int(n_unique),
            "duplicate_count": int(dupes),
        }
        if dupes > 0:
            strategy = spec.get("strategy")
            if logger is not None:
                logger.warning(
                    f"PK column {col_name!r} has {dupes} duplicate value(s) "
                    f"out of {n_total} non-null rows. The strategy "
                    f"({strategy!r}) doesn't guarantee uniqueness at this "
                    f"row count."
                )
            pk_duplicate_failures.append((col_name, n_total, n_unique, strategy))

    if ctx is not None and hasattr(ctx, "export"):
        ctx.export("rows_generated", len(out))
        ctx.export("columns_generated", len(columns))
        ctx.export("seed_used", seed)
        # Per-FK metrics for the platform-side evidence assembler.
        # Keyed by child column name so the manifest can join against
        # the declared_relationships block.
        if fk_metrics:
            ctx.export("fk_preservation", fk_metrics)
        if pk_metrics:
            ctx.export("pk_uniqueness", pk_metrics)

    # Strict PK uniqueness raise (tier-1 audit, 2026-05-20). Done after
    # the manifest export so even on failure the assembler sees the
    # collision count — auditors can see why the run aborted, not just
    # that it did. DECOY_PK_LENIENT=1 skips the raise.
    if pk_duplicate_failures and not pk_lenient:
        from decoy_engine.errors import PKDuplicatesError

        col_name, n_total, n_unique, strategy = pk_duplicate_failures[0]
        raise PKDuplicatesError(col_name, n_total, n_unique, strategy)

    return out


# ── Helpers for the FK paths above ────────────────────────────────


def _emit_m2m_junction(
    *,
    spec: dict[str, Any],
    out: "pd.DataFrame",
    pool_resolver: Any,
    num_rows: int,
    fk_metrics: dict[str, dict[str, Any]],
    current_node_id: str | None,
    logger: Any,
) -> None:
    """Emit the two junction columns for a many-to-many relationship.

    `spec` shape (validated by runner._validate_m2m_entry):
        kind: m2m
        junction:    { node: <this op's node id>, columns: [left_col, right_col] }
        left_parent:  { node: <l_node>,  column: <l_col> }
        right_parent: { node: <r_node>,  column: <r_col> }
        pool_strategy: cartesian | sampled | weighted   # default cartesian
        left_weights:  [w1, w2, ...]   # optional; parallel to left_pool
        right_weights: [w1, w2, ...]   # optional; parallel to right_pool

    Pool strategies:
      - cartesian: every (left, right) pair gets one row. row_count is
        ignored — output has `len(left_pool) * len(right_pool)` rows.
      - sampled:   pick `row_count` random pairs from the cartesian
        product, uniform per side. Same input keys -> same picks
        (HMAC-deterministic per-row index).
      - weighted:  pick `row_count` random pairs but bias each side by
        its weights list. Operators control per-pool popularity (e.g.
        which courses are popular, which students enroll most). Falls
        back to uniform when weights aren't provided or don't match
        the parent pool length. Same deterministic seed pattern as
        sampled.

    Mutates `out` in place by writing the two junction columns. Also
    populates fk_metrics for the manifest assembler.
    """
    junction = spec.get("junction") or {}
    left = spec.get("left_parent") or {}
    right = spec.get("right_parent") or {}
    j_cols = junction.get("columns") or []
    if len(j_cols) != 2:
        return  # validator should have caught
    left_col_out, right_col_out = j_cols[0], j_cols[1]
    pool_strategy = spec.get("pool_strategy", "cartesian")

    if pool_resolver is None:
        if logger:
            logger.warning(
                "m2m junction skipped: no pool_resolver in context "
                f"(node={current_node_id}, junction={j_cols})"
            )
        return

    left_pool = list(pool_resolver(left["node"], left["column"]))
    right_pool = list(pool_resolver(right["node"], right["column"]))
    if not left_pool or not right_pool:
        if logger:
            logger.warning("m2m junction: one or both parent pools empty; emitting empty columns")
        out[left_col_out] = []
        out[right_col_out] = []
        return

    if pool_strategy == "cartesian":
        # Full cross product. Use list comprehensions; for typical
        # junction tables (< 100 x 100) this is cheap.
        left_vals = []
        right_vals = []
        for lv in left_pool:
            for rv in right_pool:
                left_vals.append(lv)
                right_vals.append(rv)
    else:
        # sampled or weighted: deterministic pair picks. We HMAC the
        # row index to get the indices, with the bias step applied
        # when pool_strategy is "weighted" and a usable weights list
        # is present for that side.
        import hashlib
        import hmac

        n_left = len(left_pool)
        n_right = len(right_pool)
        seed_bytes = f"m2m:{current_node_id}:{left_col_out}:{right_col_out}".encode()

        # Pre-compute weighted cumulative indices when weights are
        # supplied + valid. The CDF lets us map a uniform [0, total)
        # draw to a weighted pool index via bisect_right. Falling back
        # to None means "use uniform modulo" — matches sampled.
        def _cdf_or_none(weights, n: int) -> tuple[list[float], float] | None:
            if not isinstance(weights, list):
                return None
            if len(weights) != n:
                if logger:
                    logger.warning(
                        f"m2m {pool_strategy} weights length {len(weights)} "
                        f"doesn't match pool size {n}; falling back to uniform "
                        f"for this side"
                    )
                return None
            # Coerce to floats; drop negatives + non-numerics by zeroing.
            cdf: list[float] = []
            running = 0.0
            for w in weights:
                try:
                    wf = float(w)
                except (TypeError, ValueError):
                    wf = 0.0
                if wf < 0:
                    wf = 0.0
                running += wf
                cdf.append(running)
            if running <= 0:
                if logger:
                    logger.warning(f"m2m {pool_strategy} weights sum to 0; falling back to uniform")
                return None
            return cdf, running

        left_cdf: tuple[list[float], float] | None = None
        right_cdf: tuple[list[float], float] | None = None
        if pool_strategy == "weighted":
            left_cdf = _cdf_or_none(spec.get("left_weights"), n_left)
            right_cdf = _cdf_or_none(spec.get("right_weights"), n_right)

        import bisect

        def _pick(cdf: tuple[list[float], float] | None, n: int, mac_bytes: bytes) -> int:
            if cdf is None:
                return int.from_bytes(mac_bytes[:4], "big") % n
            # Uniform draw in [0, total) from the high-entropy mac slice.
            draw = (int.from_bytes(mac_bytes[:8], "big") / 2**64) * cdf[1]
            return bisect.bisect_right(cdf[0], draw)

        left_vals = []
        right_vals = []
        for i in range(num_rows):
            mac = hmac.new(seed_bytes, str(i).encode(), hashlib.sha256).digest()
            li = _pick(left_cdf, n_left, mac[:8])
            ri = _pick(right_cdf, n_right, mac[8:16])
            if li >= n_left:
                li = n_left - 1
            if ri >= n_right:
                ri = n_right - 1
            left_vals.append(left_pool[li])
            right_vals.append(right_pool[ri])

    # If `out` has an existing row index (e.g. from upstream input),
    # truncate / extend to match the new column lengths. Cartesian
    # produces N*M rows; sampled produces num_rows.
    new_len = len(left_vals)
    if len(out) != new_len:
        # Replace the frame with one of the right length. Other columns
        # in `out` (rare for a pure-source m2m generate node) are
        # truncated to new_len rows.
        if len(out) > new_len:
            for col in out.columns:
                out_arr = out[col].iloc[:new_len].reset_index(drop=True)
                out[col] = out_arr
        # Drop the row-count-shaped index and rebuild against the new
        # data length. Other existing columns: pad with None if they
        # were shorter than the new length (rare).
        out.reset_index(drop=True, inplace=True)
    out[left_col_out] = left_vals
    out[right_col_out] = right_vals

    fk_metrics[f"__m2m__{left_col_out}_{right_col_out}"] = {
        "kind": "m2m",
        "junction_node": current_node_id,
        "junction_columns": j_cols,
        "left_parent": f"{left.get('node')}.{left.get('column')}",
        "right_parent": f"{right.get('node')}.{right.get('column')}",
        "pool_strategy": pool_strategy,
        "pair_count": new_len,
    }
