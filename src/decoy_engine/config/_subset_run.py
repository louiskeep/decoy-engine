"""Refuse a pipeline config that sets `subset:` on an entry that cannot apply it.

Only `run_pipeline` applies the block (and the entries that delegate to it). A chunked entry,
`compile_plan` called directly and `probe_peak_bytes` would otherwise mask the full sources and
exit 0, which is wrong output with no error, so each calls this guard first.

Named limit: the guard reads the validated dump's `subset` key. A caller that strips the key,
or hands a plan compiled elsewhere to `run_sequential` or the out-of-core runner without
`compile_plan`, is not seen. A config whose subset was copied into `sources` by hand is
ordinary input.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from decoy_engine import reasons
from decoy_engine.errors import PipelineValidationError

__all__: list[str] = []

SUBSET_RUN_MESSAGE = "subset: is applied only by run_pipeline; this entry does not apply it"
SUBSET_LOADER_MESSAGE = "subset: cannot be applied to a source_loader run; pass sources instead"


def refuse_unapplied_subset(config: Mapping[str, Any]) -> None:
    """Raise `PipelineValidationError` when `config` sets a `subset` block."""
    if config.get("subset") is not None:
        raise PipelineValidationError(
            SUBSET_RUN_MESSAGE,
            path="subset",
            code=reasons.config.SUBSET_RUN_UNSUPPORTED,
        )


def refuse_subset_source_loader() -> None:
    """Raise `PipelineValidationError` for a subset on a run that reads through a loader."""
    raise PipelineValidationError(
        SUBSET_LOADER_MESSAGE, path="subset", code=reasons.config.SUBSET_RUN_UNSUPPORTED
    )
