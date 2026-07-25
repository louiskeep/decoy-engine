"""bucket_perturb strategy handler (engine-v2 SP-08b, 2026-06-28).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.bucket_perturb``.
Core logic (bucket computation, deterministic position derivation) lives in the
transforms module for testability.

Config keys accepted via ``plan.provider_config``:
  bucket       str  ``"week"``, ``"month"``, or ``"quarter"``.
  date_format  str  Optional strptime/strftime format. Detected from data if absent.

Requires ``plan.namespace`` (same contract as date_shift): namespace is required for
``derive(job_seed, namespace, value)`` so the offset is isolated per column context.
"""

from __future__ import annotations

import pandas as pd

from decoy_engine.execution._adapter import StrategyContext, provider_config_to_dict
from decoy_engine.execution._errors import StrategyError
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.plan._types import ColumnSeed
from decoy_engine.transforms.bucket_perturb import (
    apply_bucket_perturb,
    validate_bucket_perturb_config,
)


class BucketPerturbStrategyHandler:
    """Coarse time-bucket generalization: snap dates to a deterministic position
    within their ISO week, calendar month, or calendar quarter.

    Delegates to ``decoy_engine.transforms.bucket_perturb.apply_bucket_perturb``.
    Requires ``plan.namespace`` for per-column derive() isolation (same contract as
    date_shift; raises StrategyError without it).
    """

    name: str = "bucket_perturb"

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        if plan.namespace is None:
            raise StrategyError(
                code="bucket_perturb_requires_namespace",
                strategy="bucket_perturb",
                message=f"column {column!r} uses bucket_perturb but has no namespace.",
            )
        cfg = provider_config_to_dict(plan.provider_config)
        bucket = str(cfg.get("bucket", "month"))
        date_format: str | None = cfg.get("date_format") or None

        # Validate fail-closed before touching any data. apply_bucket_perturb
        # must never run with an unrecognized bucket. Pass a resolved cfg so the
        # validator sees the default-applied bucket value, not the raw (possibly
        # absent) key.
        try:
            validate_bucket_perturb_config({**cfg, "bucket": bucket})
        except ValueError as exc:
            raise StrategyError(
                code="bucket_perturb_invalid_config",
                strategy="bucket_perturb",
                message=str(exc),
            ) from exc

        col = df[column]
        perturbed = apply_bucket_perturb(
            col,
            bucket=bucket,
            job_seed=ctx.mask_key,
            namespace=plan.namespace,
            date_format=date_format,
        )
        result = df.copy()
        result[column] = perturbed
        return result, []
