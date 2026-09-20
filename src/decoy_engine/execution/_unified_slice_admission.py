"""Task 4.5 D3 (hardened, post-Codex-determination): the unified-slice
admission predicate, split out of `_unified_slice.py` to hold the ~600-LOC
orchestration cap (CLAUDE.md "Engineering best practices"; the same reason
`_pipeline_routing.py` / `_pipeline_routing_signals.py` are split).

Root cause of every prior finding here (exotic dtypes, dup names, metadata,
namespace): the legacy pandas route and the native/shadow-coordinator route
do not commute (Arrow<->pandas is lossy, dtype/index/metadata-dependent), and
a physical binding's own `input_schema` type is resolved from the PROFILE's
COARSE dtype label -- object/string/category all collapse to `pa.string()`
(`_shadow_bindings.py:98`, `native/_requirements.py:47-80`) -- never from the
real resident Arrow type. Treating profile-derived agreement as proof the
two pipelines are equivalent is the mistake; this module instead proves the
ACTUAL RESIDENT table, checked directly, belongs to one finite, reviewed
equivalence domain, for EVERY admitted node, not a blocklist of previously-
discovered edges.

Two stages, matching the plan:

- `cheap_admission`: everything decidable from `config` / the already-
  resolved routing facts / the resident source's own schema and data, with
  zero `execution.physical` import. Any doubt here declines before the lazy
  import in `_unified_slice._execute_admitted` ever runs. Also performs the
  ONE Arrow->pandas conversion this lane needs (source-shaped output
  assembly reuses it, `_unified_slice.py`'s own D2 comment) and validates
  that conversion carries no named/physical pandas index.
- `resident_contract_admission`: the facts only the REAL 4.3 compiler (and a
  companion/null-data check against the admitted source) can answer -- built
  from the compiled `PhysicalPlan`, never re-implemented by hand. This is
  the DOMINATING, all-node resident-contract gate: every node's resident
  Arrow type must equal its own compiled binding's input type EXACTLY *and*
  fall inside the fixed per-strategy admitted-type matrix below -- the first
  check alone is not enough, since a genuinely non-string column bound to
  redact/truncate (which the compiler gates on CONFIG only, never on input
  type) would still "match" its own compiled type.

This module makes NO ExecutionError/exception-boundary decisions of its own
(that is D8's job, owned by `_unified_slice.py`); it only decides admit vs.
decline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd
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
    "resident_contract_admission",
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

# The fixed, reviewed resident-type domain per slice strategy -- the actual
# set the 4.4 shadow corpus characterizes, not the compiler's coarse profile
# label. A resident type outside its strategy's set declines regardless of
# whether it happens to match the compiled `input_schema` type (e.g. a
# genuinely int64 column bound to redact, which the compiler's own config-
# only gate never rejects). Widening this later is a separately-proven
# slice, never a default.
_ADMITTED_RESIDENT_TYPES: dict[str, frozenset[pa.DataType]] = {
    "passthrough": frozenset({pa.string(), pa.int64(), pa.bool_()}),
    "redact": frozenset({pa.string()}),
    "truncate": frozenset({pa.string()}),
    # hash also requires null-freedom, enforced separately below via the
    # same `reject_null_bearing_int` guard the legacy adapter runs.
    "hash": frozenset({pa.string(), pa.int64()}),
}


def _find_table(config: Mapping[str, Any], table: str) -> dict[str, Any] | None:
    for tbl in config.get("tables") or ():
        if isinstance(tbl, dict) and tbl.get("name") == table:
            return tbl
    return None


@dataclass(frozen=True)
class CheapCandidate:
    """A job admitted through every check answerable without touching
    `execution.physical` or the source's actual masking behavior.

    `source_frame` is the ONE Arrow->pandas conversion of `source` this lane
    performs (`cheap_admission` builds and validates it); `_unified_slice.
    _execute_admitted` reuses it verbatim for source-shaped output assembly
    rather than converting a second time.
    """

    table: str
    source: pa.Table
    source_frame: pd.DataFrame


def cheap_admission(
    *,
    route: str,
    route_chunked: bool,
    native_route_enabled: bool,
    resolved_substrate: str,
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
    if resolved_substrate != "pandas":
        # D3: this lane returns a result identical to the PANDAS full-frame
        # route only. A non-pandas substrate (e.g. the polars opt-in) runs a
        # different legacy adapter and stamps its own provenance telemetry
        # (executed_substrate, pa<->pl conversion timings), so admitting it
        # would diverge on the caller-consumed quality_metrics. Decline to the
        # unchanged old route. `resolved_substrate` is post-resolve_substrate,
        # so substrate=None + DECOY_SUBSTRATE=polars is caught here too.
        return None
    if source_loader is not None:
        # A source_loader signals lazy/relationship loading (a different output
        # contract, `_isolated_worker._load_sources(lazy=True)`), outside this
        # slice's single-resident-table scope. Still a hard decline.
        return None
    # `sink` is intentionally NOT a decline (route activation, 2026-09-20). The
    # first check above already established route == "full_frame" (not chunked,
    # not native). On the full-frame route the sink is never consumed: the legacy
    # full-frame route ignores it (`_isolated_worker.py`'s own comment;
    # `_finalize_outputs` reads `result.outputs`), and the admitted path returns
    # outputs in-memory identically (execution goes through `_execute_admitted`,
    # which is never passed `sink`). The sink's fate is decided by ROUTE, not by
    # which full-frame implementation runs, so admitting a full-frame job that
    # carries an inert sink is behavior-preserving. The streaming/OOC route -- the
    # only one that writes a sink -- is already declined above by the route check.
    # This is what lets the platform worker (which always attaches a
    # ParquetTransactionalSink) reach the certified lane.
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

    source_descriptor = (config.get("sources") or {}).get(table)
    if (
        not isinstance(source_descriptor, dict)
        or source_descriptor.get("type") != "file"
        or source_descriptor.get("format") != "parquet"
    ):
        # D3 scopes the initial slice to a single non-FK PARQUET file source
        # (plan "Initial slice"). A csv / fixed_width / non-file source profiles
        # under a different (often loosely-typed) reader than the resident Arrow
        # table this lane masks, so its compiled plan can diverge from what the
        # legacy route would have run. Anything but the sanctioned Parquet file
        # shape declines to the unchanged old route.
        return None

    if set(caller_sources) != {table}:
        # D3: the legacy adapter echoes every resident source frame in
        # `outputs` (`_pipeline.py:588`'s own comment); a caller that loaded
        # an EXTRA table alongside the configured mask table would keep that
        # extra table (and a projection warning) in the old route's outputs
        # and lose it here, since this lane returns only the admitted table.
        return None

    source = caller_sources.get(table)
    if not isinstance(source, pa.Table):
        # Excludes both "absent" and a `LazySource` placeholder (TB-1): the
        # unified slice's D5 single-open seam holds only for an already-
        # resident table.
        return None
    if len(set(source.column_names)) != len(source.column_names):
        # Root-cause fix: the coordinator dispatches by NAME-keyed lookup
        # (`_shadow_coordinator.py:133-160`), so a duplicate resident field
        # name is unsafe regardless of config. Establishing this FIRST is
        # what makes every `set(...)`-based comparison below (config names
        # vs. resident names) a trustworthy 1:1 check rather than a set that
        # silently collapses a real cardinality mismatch.
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
    if any(_has_when_gate(col) for col in columns_cfg):
        # D3: a `when:` predicate gates masking to matching rows only
        # (`_pandas_adapter.py:405`'s `run_with_when_gate`); the coordinator
        # masks the whole array with no row gate, so an admitted `when:`
        # column would over-mask. Decline until the physical path implements
        # `when` gating (`_seed_envelope.py`'s `ColumnSeed.when`).
        return None

    try:
        source.validate(full=True)
    except pa.ArrowInvalid:
        # Root-cause fix: prove the ACTUAL resident table is well-formed
        # before anything downstream trusts its buffers, rather than
        # inheriting whatever `pa.Table.validate()`'s cheap default (schema-
        # only) would have missed.
        return None

    # Root-cause fix: this is the SAME Arrow->pandas conversion the legacy
    # route performs on this table (`to_pandas_fk_safe` reduces to a plain
    # `to_pandas()` here -- `profile.relationships` was already declined
    # above, so `fk_columns` is always empty); doing it once, here, and
    # carrying the frame forward on `CheapCandidate` means `_unified_slice.
    # _execute_admitted`'s source-shaped output assembly never re-converts.
    try:
        frame = source.to_pandas()
    except Exception:
        # Total-to-decline: any Arrow->pandas failure declines to the legacy
        # route rather than raising a different error than legacy's own coded
        # guards. Malformed `b"pandas"` schema metadata, for instance, can make
        # `to_pandas()` raise a JSONDecodeError here, where the legacy route
        # would instead reach its own reject-before-read guard (e.g.
        # `null_bearing_int_unsupported`). Declining preserves failure parity.
        return None
    if frame.index.name is not None or not frame.index.equals(pd.RangeIndex(len(frame))):
        # A named index (possibly reconstructed purely from the resident
        # table's own `b"pandas"` schema metadata, with no physical index
        # column at all) or a non-default range would become an extra
        # output column -- or shift row identity -- on the legacy route.
        return None
    if list(frame.columns) != list(source.column_names):
        # A PHYSICAL named index: pyarrow's `to_pandas()` pulls a real index
        # column out of `frame.columns` and into `frame.index`, so the
        # column set no longer matches the resident schema.
        return None

    # The resident `b"pandas"` metadata can LIE about a column's logical type:
    # e.g. an int64 array carrying transplanted StringDtype metadata reconstructs
    # to strings under `to_pandas()`. The legacy route masks THAT reconstructed
    # frame (hashing the strings), while the native coordinator masks the resident
    # physical array (hashing the integers), so identical schemas produce
    # different tokens. Require every column's pandas-to-Arrow round trip to be
    # type- AND value-identical to the resident input, so both routes operate on
    # the same values; decline any column where the metadata disagrees with the
    # physical buffer. (Ignore schema metadata here -- this is a physical-
    # consistency gate, distinct from the D9 output-metadata parity assertion.)
    try:
        round_trip = pa.Table.from_pandas(frame, preserve_index=False)
    except Exception:
        return None
    for name in source.column_names:
        resident_col = source.column(name).combine_chunks()
        if resident_col.null_count == len(resident_col):
            # An all-null column masks to all-null on either route regardless of
            # how pandas reconstructs its (value-free) type, so it cannot diverge.
            # pandas collapses an all-null column to the object/null dtype, which
            # would otherwise trip the type check below with no real divergence.
            continue
        rt_col = round_trip.column(name).combine_chunks()
        if rt_col.type != resident_col.type or not rt_col.equals(resident_col):
            return None

    return CheapCandidate(table=table, source=source, source_frame=frame)


def _has_when_gate(col: Mapping[str, Any]) -> bool:
    when = col.get("when")
    return isinstance(when, str) and bool(when.strip())


def resident_contract_admission(
    physical_plan: PhysicalPlan,
    *,
    table: str,
    source: pa.Table,
    plan: Plan,
    registry: ProviderRegistry,
    graph: RelationshipGraph,
) -> PhysicalTable | None:
    """The dominating, ALL-NODE resident-contract gate (root-cause fix,
    replacing the prior hash-only resident guard): proves the compiled plan
    is shaped correctly (an admitted native binding on every node, an
    operator from the four-entry allowlist, no prepass/diagnostic
    obligation, complete 1:1 coverage of the source's columns) AND that the
    ACTUAL resident Arrow table -- not the profile's coarse approximation of
    it -- belongs to the one finite, reviewed equivalence domain this
    slice's 4.4 corpus characterizes, for every node regardless of strategy.

    Two independent per-node type checks, both required: the resident type
    must equal the compiled binding's OWN `input_schema` type exactly (a
    dictionary/large_string/extension/null-typed/etc. resident column never
    equals the plain `pa.string()`/`pa.int64()`/`pa.bool_()` the compiler
    bound, so this alone rejects most exotic types); and the resident type
    must fall inside `_ADMITTED_RESIDENT_TYPES[node.strategy]` -- the fixed
    domain, needed because the compiler gates redact/truncate on CONFIG
    only, never on input type (`native/_requirements.py`'s own
    `redact_config_rejection` / `truncate_config_rejection`), so a
    genuinely non-string column bound to either would still "match" its own
    compiled type under the first check alone.

    Also runs the checks that need real data or a real host probe: a hash
    node's compiled native companion must actually be loadable
    (`native_kernel_rejection` is a strategy-name-only static check -- it
    says nothing about whether the compiled companion is present at THIS
    host); a hash node's namespace must encode as strict UTF-8 (the compiled
    kernel consumes it at every batch invocation, even an empty/all-null
    column, unlike the legacy per-value `derive()` call); and no hash/
    truncate column may carry a null-bearing integer (`_pandas_adapter.
    py:186-191`'s own reject, re-run here on the admitted source so the
    unified slice declines to the identical old-route failure rather than
    diverging). Any miss declines to the unchanged old route.
    """
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

    covered: list[str] = []
    hash_columns: list[str] = []
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
        column = node.columns[0]
        resident_type = source.schema.field(column).type
        if len(binding.input_schema) != 1 or binding.input_schema.field(column).type != (
            resident_type
        ):
            return None
        if resident_type not in _ADMITTED_RESIDENT_TYPES.get(node.strategy, frozenset()):
            return None
        if binding.operator_id == HASH_OPERATOR_ID:
            key_binding = binding.key_binding
            if key_binding is None:
                return None
            try:
                key_binding.namespace.encode("utf-8")
            except UnicodeEncodeError:
                return None
            hash_columns.append(column)
        covered.append(column)

    # 1:1 coverage across configured columns / physical nodes / resident
    # columns: `cheap_admission` already proved the resident names unique
    # and the configured columns match them 1:1, so cardinality-matching
    # `covered` against the resident schema here closes the loop across all
    # three sets (not merely a set-equality that would hide a duplicate
    # column claimed by two nodes).
    if len(covered) != len(set(covered)) or set(covered) != set(source.column_names):
        return None

    if hash_columns and not native_companion_status().ok:
        return None
    try:
        reject_null_bearing_int(plan, {table: source}, registry, graph)
    except ExecutionError:
        return None
    return physical_table
