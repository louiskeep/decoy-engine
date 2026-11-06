"""Refuse a pipeline config that sets `subset:` until a run can apply it.

`PipelineConfig.subset` validates, but no run path calls `run_subset`, so a run
used to mask the full sources and exit 0. That is wrong output with no error.
This guard turns it into a refusal. It is called first in `run_pipeline`,
`run_pipeline_isolated`, `run_mask_pipeline_chunked`, `run_mask_chunked` and
`compile_plan` (the last covers the platform lanes that compile a plan
themselves). SUB-RUN replaces this module when subsetting runs inside a run.

Named limit: the guard reads the validated dump's `subset` key. A caller that
strips the key, or hands a plan compiled elsewhere to `run_sequential` or the
out-of-core runner without `compile_plan`, is not seen. A config whose subset
was copied into `sources` by hand is ordinary input.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from decoy_engine import reasons
from decoy_engine.errors import PipelineValidationError

__all__: list[str] = []

SUBSET_RUN_MESSAGE = (
    "subset: is not applied by a pipeline run yet; run the subset step first and "
    "point this run's sources at its output, or remove the subset: block"
)


def refuse_unapplied_subset(config: Mapping[str, Any]) -> None:
    """Raise `PipelineValidationError` when `config` sets a `subset` block."""
    if config.get("subset") is not None:
        raise PipelineValidationError(
            SUBSET_RUN_MESSAGE,
            path="subset",
            code=reasons.config.SUBSET_RUN_UNSUPPORTED,
        )
