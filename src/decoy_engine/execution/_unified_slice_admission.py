"""Task 4.5 D3: the unified-slice admission predicate, split out of
`_unified_slice.py` to hold the ~600-LOC orchestration cap (CLAUDE.md
"Engineering best practices"; the same reason `_pipeline_routing.py` /
`_pipeline_routing_signals.py` are split).

Two stages, matching the plan:

- `cheap_admission`: everything decidable from `config` / the already-
  resolved routing facts / the resident source's schema, with zero
  `execution.physical` import. Any doubt here declines before the lazy
  import in `_unified_slice._execute_admitted` ever runs.
- `compiled_plan_admission` / `keyed_hash_and_null_int_admission`: the facts
  only the REAL 4.3 compiler (and a companion/null-data check against the
  admitted source) can answer -- built from the compiled `PhysicalPlan`,
  never re-implemented by hand.

This module makes NO ExecutionError/exception-boundary decisions of its own
(that is D8's job, owned by `_unified_slice.py`); it only decides admit vs.
decline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._guards import reject_null_bearing_int
from decoy_engine.execution.native._companion_status import native_companion_status
from decoy_engine.profile._readers import LazySource

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from decoy_engine.execution._transactional_sink import TransactionalSink
    from decoy_engine.execution.physical._plan import PhysicalPlan, PhysicalTable
    from decoy_engine.plan._types import Plan
    from decoy_engine.profile._types import Profile
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph

__all__ = [
    "ALLOWED_OPERATOR_IDS",
    "HASH_OPERATOR_ID",
    "CheapCandidate",
    "cheap_admission",
    "compiled_plan_admission",
    "keyed_hash_and_null_int_admission",
]

# The four operators the 4.4 shadow coordinator dispatches for this slice
# (`_shadow_bindings.OPERATOR_ID_BY_STRATEGY.values()`); restated here rather
# than imported so this module's cheap-admission surface stays importable
# with zero `execution.physical` reach. Public (no leading underscore):
# `_unified_slice.py`'s D7 evidence check reads `HASH_OPERATOR_ID` too.
ALLOWED_OPERATOR_IDS = frozenset(
    {"native_passthrough", "native_redact", "native_truncate", "native_keyed_hash"}
)
HASH_OPERATOR_ID = "native_keyed_hash"


def _find_table(config: Mapping[str, Any], table: str) -> dict[str, Any] | None:
    for tbl in config.get("tables") or ():
        if isinstance(tbl, dict) and tbl.get("name") == table:
            return tbl
    return None


@dataclass(frozen=True)
class CheapCandidate:
    """A job admitted through every check answerable without touching
    `execution.physical` or the source's actual masking behavior."""

    table: str
    source: pa.Table


def cheap_admission(
    *,
    route: str,
    route_chunked: bool,
    native_route_enabled: bool,
    sink: TransactionalSink | None,
    source_loader: Callable[[str], pa.Table] | None,
    fidelity_report: bool,
    vault_writer: Any,
    config: Mapping[str, Any],
    profile: Profile,
    table_kinds: Mapping[str, str],
    caller_sources: Mapping[str, pa.Table | LazySource],
) -> CheapCandidate | None:
    """D3's no-I/O admission half. Returns `None` on ANY doubt -- the caller
    falls through to the unchanged old route without a reason code, matching
    the native lane's own `(None, None)` contract at this same call tier
    (the coded reason is a nice-to-have for a future telemetry pass, not a
    D9 requirement, so it is not threaded through here)."""
    if route != "full_frame" or route_chunked or native_route_enabled:
        return None
    if sink is not None or source_loader is not None:
        return None
    if fidelity_report or vault_writer is not None:
        return None
    if config.get("validators") or config.get("quarantine") or config.get("run_storm"):
        return None
    if profile.relationships:
        return None

    mask_tables = [name for name, kind in table_kinds.items() if kind == "mask"]
    if len(table_kinds) != 1 or len(mask_tables) != 1:
        return None
    table = mask_tables[0]

    source = caller_sources.get(table)
    if not isinstance(source, pa.Table):
        # Excludes both "absent" and a `LazySource` placeholder (TB-1): the
        # unified slice's D5 single-open seam holds only for an already-
        # resident table.
        return None

    table_cfg = _find_table(config, table)
    if table_cfg is None or table_cfg.get("transforms"):
        return None

    columns_cfg = table_cfg.get("columns") or ()
    if not columns_cfg or not all(isinstance(col, dict) for col in columns_cfg):
        return None
    names = [col.get("name") for col in columns_cfg]
    if any(name is None for name in names) or len(set(names)) != len(names):
        return None
    if set(names) != set(source.column_names):
        # No duplicates/omissions/undeclared-passthrough columns (D3): the
        # configured surface must equal the source schema exactly.
        return None
    if any(bool(col.get("vault", False)) for col in columns_cfg):
        return None

    return CheapCandidate(table=table, source=source)


def compiled_plan_admission(
    physical_plan: PhysicalPlan, *, table: str, source: pa.Table
) -> PhysicalTable | None:
    """D3's compiled-plan half: every node the 4.3 compiler produced for
    `table` must carry an admitted native binding, an operator from the
    four-entry allowlist, no prepass/diagnostic obligation, and complete 1:1
    coverage of the source's columns. Any miss declines -- the compiler
    already ran, so this is a read of its output, never a re-implementation
    of what it decided."""
    from decoy_engine.execution.physical._types import DriverId

    physical_table = next((t for t in physical_plan.tables if t.table == table), None)
    if physical_table is None or physical_table.driver != DriverId.FULL_FRAME:
        return None
    nodes = physical_table.nodes
    if not nodes:
        return None
    node_ids = [node.node_id for node in nodes]
    if len(set(node_ids)) != len(node_ids):
        return None

    covered: set[str] = set()
    for node in nodes:
        binding = node.execution
        if binding is None:
            return None
        if node.kind != "scalar" or len(node.columns) != 1:
            return None
        if binding.operator_id not in ALLOWED_OPERATOR_IDS:
            return None
        if binding.required_prepasses or binding.diagnostic_obligations:
            return None
        covered.add(node.columns[0])
    if covered != set(source.column_names):
        return None
    return physical_table


def keyed_hash_and_null_int_admission(
    physical_table: PhysicalTable,
    *,
    plan: Plan,
    source: pa.Table,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
    table: str,
) -> bool:
    """The two admission checks that need real data or a real host probe,
    not just the compiled plan's static shape (D3): a hash node's compiled
    native companion must actually be loadable (`native_kernel_rejection`
    is a strategy-name-only static check -- it says nothing about whether
    the compiled companion is present at THIS host), and no hash/truncate
    column may carry a null-bearing integer (`_pandas_adapter.py:186-191`'s
    own reject, re-run here on the admitted source so the unified slice
    declines to the identical old-route failure rather than diverging).
    Returns True when both hold."""
    has_hash_node = any(
        node.execution is not None and node.execution.operator_id == HASH_OPERATOR_ID
        for node in physical_table.nodes
    )
    if has_hash_node and not native_companion_status().ok:
        return False
    try:
        reject_null_bearing_int(plan, {table: source}, registry, graph)
    except ExecutionError:
        return False
    return True
