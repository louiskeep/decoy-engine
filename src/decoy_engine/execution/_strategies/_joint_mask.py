"""joint_mask strategy handler (engine-v2 SP-08, 2026-06-28).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.joint_mask``.
Core logic (HMAC-keyed row selection, gen-mode seeded sampling, config
validation) lives in the transforms module so it can be tested and reused
outside the execution layer.

joint_mask is a MULTI-COLUMN transform: it writes all ``columns`` from the
``joint_masks`` config in a single pass. The handler runs once per joint_mask
group and the ``column`` parameter is the first (alphabetically sorted) column
in the group, used only for adapter-level dispatch tracking.

Config keys accepted via ``plan.provider_config``:
  columns     list[str]  Output columns to write (must match reference table).
  reference   str        Reference table name (e.g. ``us_zip5_city_state``).
  key_by      str        Source column for HMAC-keyed selection (mask mode).
  mode        str        ``"mask"`` (default) or ``"gen"``.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine import fileaccess
from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.joint_mask import JointMaskConfig, apply_joint_mask


class JointMaskHandler:
    """Reference-tuple masking: replaces coupled columns with a consistent row.

    Delegates to ``decoy_engine.transforms.joint_mask.apply_joint_mask``.
    Config validation runs at execution time, pre-mutation, fail-closed: an invalid
    ``provider_config`` raises ``PlanCompileError`` (via ``JointMaskConfig.from_dict``)
    before any row is processed.
    """

    name: str = "joint_mask"

    def gated_output_columns(self, plan: ColumnSeed) -> tuple[str, ...]:
        """Every column this handler writes, for the `when`-gate stitch-back.

        joint_mask is multi-column: `run` writes all `columns` from the config in
        one pass, but the adapter dispatches on just one of them (the per-column
        node's dispatch column). The `when` gate must copy ALL of these back into
        the full frame for the selected rows; copying only the dispatch column
        leaves the joint_mask's other outputs unmasked for those rows. Duck-typed
        (`getattr`) by `run_with_when_gate`, the same pattern as `preflight`; a
        handler that writes only its dispatch column simply does not define it.
        """
        cols = provider_config_to_dict(plan.provider_config).get("columns")
        return tuple(cols) if cols else ()

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        cfg = provider_config_to_dict(plan.provider_config)
        mode = str(cfg.get("mode", "mask"))
        with fileaccess.at_position(
            f"tables.{ctx.current_table}.columns.{ctx.nested_outer_column or column}"
        ):
            joint_cfg = JointMaskConfig.from_dict(cfg)
        # Cross-column V1: key_by is a SOURCE key (its docstring: "key_by: Source
        # column"; the FK-key identity analogy), so read its immutable pre-mask
        # value from ctx.group_anchor_snapshots -- the date_shift group-anchor
        # precedent -- never the live (possibly masked) frame. The adapters
        # snapshot every joint_mask key_by (`source_anchor_columns`).
        key_series = None
        if mode == "mask":
            key_series = ctx.group_anchor_snapshots.get((ctx.current_table, joint_cfg.key_by))
            if key_series is None:
                # Fail closed whenever the required pre-mask snapshot is absent,
                # regardless of whether key_by happens to be a live-frame column.
                # Without the snapshot the only fallback is a masked live read or
                # the synthetic null key, both of which silently select the wrong
                # reference tuple. A missing snapshot means key_by names no source
                # column or an adapter failed to snapshot it: either way, raise.
                raise StrategyError(
                    code="joint_mask_key_by_snapshot_missing",
                    strategy="joint_mask",
                    message=(
                        f"joint_mask on {','.join(joint_cfg.columns)!r} reads key_by "
                        f"{joint_cfg.key_by!r} but no pre-mask source snapshot was provided; "
                        "the run cannot select the reference tuple off the source key "
                        "deterministically. Check that key_by names a source column the "
                        "adapter snapshots pre-mask."
                    ),
                )
            # The snapshot spans the FULL pre-mask table; the frame the handler
            # sees may be a `when`-gated subset (df.loc[mask]), which keeps the
            # source index labels. Align the snapshot to those rows by index label
            # (the group_anchor_snapshots contract) so a partial `when` selection
            # keys each retained row off its OWN source value, not row 0..len-1. A
            # frame label the snapshot does not carry is a real misalignment: fail
            # closed rather than key off the wrong source row.
            if not df.index.isin(key_series.index).all():
                raise StrategyError(
                    code="joint_mask_key_by_snapshot_misaligned",
                    strategy="joint_mask",
                    message=(
                        f"joint_mask key_by {joint_cfg.key_by!r}: the pre-mask source "
                        "snapshot does not row-align with the frame being masked (the "
                        "frame carries rows absent from the snapshot), so keyed selection "
                        "would be wrong. The run fails closed."
                    ),
                )
            key_series = key_series.loc[df.index]
        updated_df = apply_joint_mask(
            df,
            joint_cfg,
            mode=mode,
            job_seed=ctx.mask_key,
            namespace=plan.namespace,
            key_series=key_series,
        )
        return updated_df, []
