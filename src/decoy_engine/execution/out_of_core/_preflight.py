"""Fail-closed preflight for `run_fk_out_of_core`, run before any scratch dir or sink write.

Every gate here is a pure rejection: it raises a coded `ExecutionError` and has no
side effect, so a refused job leaves nothing behind.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine import reasons
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core import _mask_group_c
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.execution.out_of_core._null_int_guard import reject_null_bearing_int_ooc
from decoy_engine.execution.out_of_core._route_policy import resolve_reorder_threshold_rows
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._vault_collect import assert_vault_inputs
from decoy_engine.keyprovider import require_mask_key

if TYPE_CHECKING:
    from typing import Any

    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph


@dataclass(frozen=True)
class OocPreflight:
    mask_key: bytes | None
    reorder_threshold_rows: int


def run_preflight(
    plan: Plan,
    sources: Mapping[str, pa.Table | LazySource],
    *,
    registry: ProviderRegistry,
    relationship_graph: RelationshipGraph,
    key_provider: KeyProvider | None,
    vault_writer: Any,
    config: Mapping[str, Any] | None,
    batch_rows: int | None,
    out_of_core_reorder_threshold_rows: int | None,
) -> OocPreflight:
    if batch_rows is not None and batch_rows < 1:
        raise ExecutionError(
            code=reasons.runtime.OUT_OF_CORE_BATCH_ROWS_INVALID,
            message=f"batch_rows must be a positive row count, got {batch_rows}.",
        )
    # Validate the override before the caller creates a temp root: an invalid value must not leak a scratch dir.
    reorder_threshold_rows = resolve_reorder_threshold_rows(out_of_core_reorder_threshold_rows)
    # DE-02 (Codex BLOCKER 4): fail-closed gate FIRST, before any admissibility or source
    # check, so a keyed out-of-core job can never run off job_seed at GA from any entry point.
    mask_key = require_mask_key(plan, key_provider)
    assert_vault_inputs(vault_writer, config, mask_key)
    work = order_work(build_work_list(plan, registry), relationship_graph)
    # Schema-only read (a LazySource's schema is cached), so a forced out-of-core
    # job with a non-string faker source refuses here, before any scratch dir or
    # sink write, rather than aborting mid-run (B4).
    source_types = {
        (table_name, field.name): field.type
        for table_name, source in sources.items()
        if getattr(source, "schema", None) is not None
        for field in source.schema
    }
    compat = check_out_of_core_compatibility(
        plan, work, relationship_graph, source_types=source_types
    )
    if not compat.accepted:
        raise ExecutionError(
            code=compat.primary_code or reasons.runtime.OUT_OF_CORE_REJECTED,
            message=compat.message(),
        )
    _mask_group_c.preflight_group_c(work)
    for edge in relationship_graph.edges:
        if edge.parent_table not in sources or edge.child_table not in sources:
            raise ExecutionError(
                code=reasons.runtime.OUT_OF_CORE_SOURCE_MISSING,
                message="out-of-core FK route requires every parent and child source.",
            )
    # Last: it may scan a stats-less source, so run it only once the cheap gates passed.
    reject_null_bearing_int_ooc(work, sources, relationship_graph)
    return OocPreflight(mask_key, reorder_threshold_rows)
