"""bucket_perturb strategy handler (engine-v2 SP-08b, 2026-06-28).

Thin V2 StrategyHandler wrapping ``decoy_engine.transforms.bucket_perturb``.
Core logic (bucket computation, deterministic position derivation) lives in the
transforms module for testability.

Config keys accepted via ``plan.provider_config``:
  bucket           str   ``"week"``, ``"month"``, or ``"quarter"``.
  date_format      str   Optional strptime/strftime format. Detected from data if absent.
  preserve_values  list  Optional ISO dates emitted unchanged (requires ``date_format``).

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
from decoy_engine.transforms.date_preserve import (
    PreserveSet,
    apply_excluding_preserved,
    preserve_set_from_config,
)


def _validated_config(plan: ColumnSeed) -> tuple[str, str | None, PreserveSet | None]:
    """Resolve ``(bucket, date_format, preserve)``, failing closed on an invalid config.

    Validates before any data is touched, on the raw ``date_format`` so a falsy
    non-string cannot slide into autodetect. ``bucket`` defaults to "month" and
    the validator sees that default, not the possibly absent raw key.
    """
    cfg = provider_config_to_dict(plan.provider_config)
    bucket = str(cfg.get("bucket", "month"))
    try:
        validate_bucket_perturb_config({**cfg, "bucket": bucket})
    except ValueError as exc:
        raise StrategyError(
            code="bucket_perturb_invalid_config",
            strategy="bucket_perturb",
            message=str(exc),
        ) from exc
    date_format: str | None = cfg.get("date_format") or None
    return bucket, date_format, preserve_set_from_config(cfg)


class BucketPerturbStrategyHandler:
    """Coarse time-bucket generalization: snap dates to a deterministic position
    within their ISO week, calendar month, or calendar quarter.

    Delegates to ``decoy_engine.transforms.bucket_perturb.apply_bucket_perturb``.
    Requires ``plan.namespace`` for per-column derive() isolation (same contract as
    date_shift; raises StrategyError without it).
    """

    name: str = "bucket_perturb"

    def preflight(self, plan: ColumnSeed, ctx: StrategyContext) -> None:
        """Validate the config even when a `when:` gate selects no row.

        The gate skips `run` on a zero-match predicate, which would let a
        `date_format` that cannot write a date back pass unchecked.
        """
        _validated_config(plan)

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
        namespace = plan.namespace
        bucket, date_format, preserve = _validated_config(plan)

        col = df[column]

        def perturb(series: pd.Series) -> pd.Series:
            return apply_bucket_perturb(
                series,
                bucket=bucket,
                job_seed=ctx.mask_key,
                namespace=namespace,
                date_format=date_format,
            )

        if preserve is None:
            perturbed = perturb(col)
        else:
            perturbed, preserved = apply_excluding_preserved(col, preserve, perturb)
            ctx.record_date_preserved(column, preserved)
        result = df.copy()
        result[column] = perturbed
        return result, []
