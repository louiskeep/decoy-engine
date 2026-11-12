"""Split a pipeline config into the run config and the subset request it carries.

The run config keeps the `subset` key (set to `None`) so its dump, and the config hash the
plan stamps, equal those of a config that never had a subset block. The fingerprint is the
subset half of a run's identity: a subset run shares its config hash with the full run on
purpose, so the pair `(pipeline_config_hash, fingerprint)` is what tells them apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from decoy_engine.subset._api import subset_request_from_config
from decoy_engine.subset._scope import SubsetRequest


@dataclass(frozen=True)
class SubsetSplit:
    run_config: dict[str, Any]
    request: SubsetRequest | None
    fingerprint: str | None


def split_subset_config(config: dict[str, Any]) -> SubsetSplit:
    request = subset_request_from_config(config)
    if request is None:
        return SubsetSplit(config, None, None)
    return SubsetSplit({**config, "subset": None}, request, request.fingerprint())
