"""Out-of-core relationship execution support."""

from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._budget import (
    OutOfCoreBudget,
    check_temp_disk_budget,
    resolve_budget,
    temp_disk_bytes,
)
from decoy_engine.execution.out_of_core._compat import (
    SUPPORTED_STRATEGIES,
    OutOfCoreCompatibility,
    OutOfCoreRejection,
    check_out_of_core_compatibility,
)
from decoy_engine.execution.out_of_core._join import mask_child_fk, mask_child_fk_fail
from decoy_engine.execution.out_of_core._mask import mask_batch, mask_column
from decoy_engine.execution.out_of_core._relation import (
    ParentKeyRelation,
    build_parent_key_relation,
    build_parent_key_relation_from_tables,
)
from decoy_engine.execution.out_of_core._runner import run_fk_out_of_core
from decoy_engine.execution.out_of_core._source import LazySource

__all__ = [
    "SUPPORTED_STRATEGIES",
    "ChildFkBatchJoiner",
    "LazySource",
    "OutOfCoreBudget",
    "OutOfCoreCompatibility",
    "OutOfCoreRejection",
    "ParentKeyRelation",
    "build_parent_key_relation",
    "build_parent_key_relation_from_tables",
    "check_out_of_core_compatibility",
    "check_temp_disk_budget",
    "mask_batch",
    "mask_child_fk",
    "mask_child_fk_fail",
    "mask_column",
    "resolve_budget",
    "run_fk_out_of_core",
    "temp_disk_bytes",
]
