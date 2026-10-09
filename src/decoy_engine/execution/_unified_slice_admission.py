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

import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._fk_keys import to_pandas_fk_safe
from decoy_engine.execution._guards import reject_null_bearing_int
from decoy_engine.execution._operator_registry import OPERATORS
from decoy_engine.execution._unified_slice_resident_types import (
    _ADMITTED_RESIDENT_TYPES,
    _group_key_sibling_admitted,
    deterministic_resident_types,
    positional_resident_types,
)
from decoy_engine.execution._unified_slice_when import when_columns_admitted
from decoy_engine.execution.native._companion_status import native_kernel_availability
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
    "BACKEND_BY_OPERATOR_ID",
    "BUCKET_PERTURB_OPERATOR_ID",
    "CATEGORICAL_OPERATOR_ID",
    "DATE_SHIFT_OPERATOR_ID",
    "FAKER_OPERATOR_ID",
    "GROUP_KEY_OPERATOR_ID",
    "HASH_OPERATOR_ID",
    "CheapCandidate",
    "cheap_admission",
    "resident_contract_admission",
]

# Every table below is derived from the operator registry (`_operator_registry.OPERATORS`);
# edit the registry, not these. They keep their names so consumers do not change. The
# registry is a leaf module, so this cheap-admission surface still reaches no
# `execution.physical`. `HASH_OPERATOR_ID` and `FAKER_OPERATOR_ID` are read by
# `_unified_slice_evidence.py`'s positive-kernel-evidence check.
ALLOWED_OPERATOR_IDS = frozenset(spec.operator_id for spec in OPERATORS.values())
# The backend each admitted operator plans to run on, in the chunked route's vocabulary.
# A new operator names its backend in its registry entry, so it cannot be left out.
BACKEND_BY_OPERATOR_ID: Final[Mapping[str, str]] = MappingProxyType(
    {spec.operator_id: spec.planned_backend for spec in OPERATORS.values()}
)
HASH_OPERATOR_ID = OPERATORS["hash"].operator_id
# Phase 5 Track B / S-slate: like hash, categorical and bucket_perturb consume
# their namespace through the compiled index kernel at every batch invocation
# and need the native companion loadable at this host, so
# `resident_contract_admission` gates all three the same way (the
# `_COMPANION_DEPENDENT_OPERATOR_IDS` set below).
CATEGORICAL_OPERATOR_ID = OPERATORS["categorical"].operator_id
BUCKET_PERTURB_OPERATOR_ID = OPERATORS["bucket_perturb"].operator_id
GROUP_KEY_OPERATOR_ID = OPERATORS["group_key"].operator_id
DATE_SHIFT_OPERATOR_ID = OPERATORS["date_shift"].operator_id
FAKER_OPERATOR_ID = OPERATORS["faker"].operator_id

# The operators whose native execution needs a compiled kernel loadable at this host: hash
# (crypto), the index-kernel operators (categorical, bucket_perturb, date_shift, faker), and
# group_key (raw-hex). A table carrying any of these declines to the oracle when the
# companion is absent -- the CI `substrate(pandas)` leg. Derived from the operator registry
# (every operator that names a `required_kernel`); edit the registry.
_COMPANION_DEPENDENT_OPERATOR_IDS = frozenset(
    spec.operator_id for spec in OPERATORS.values() if spec.required_kernel is not None
)

# Which compiled kernel each companion-dependent operator actually loads, so the
# admission gate requires ONLY the kernel(s) this table's operators use (PER-
# OPERATOR, not the global `.ok`). hash -> the crypto `derive_batch`; categorical
# / bucket_perturb -> the index `derive_index_batch`; group_key -> the raw-hex
# `derive_hex_raw_batch`. A companion missing only the additive raw-hex symbol
# therefore keeps hash / categorical / bucket_perturb native and declines just
# group_key (matching `_group_key_ext`'s own hash-only-stays-native contract).
# Derived from the operator registry; edit the registry.
_OPERATOR_REQUIRED_KERNEL: dict[str, str] = {
    spec.operator_id: spec.required_kernel
    for spec in OPERATORS.values()
    if spec.required_kernel is not None
}

# The ONE diagnostic obligation the coordinator routes, per operator: date_shift's
# format_error row errors, which the coordinator collects, rebases, and hands to
# `finalize_validators_and_quarantine` (a non-empty set raises there and the
# unified slice reroutes the table to the oracle, which fails identically). Any
# other obligation (a warning reducer, a second trigger) or any other operator
# carrying one still declines: nothing else is routed. This is coordinator POLICY,
# read from each descriptor's `routed_diagnostics`; it must not be derived from the
# capability reducers, or the `obligations <= routed` gate below would always pass.
_ROUTED_DIAGNOSTIC_OBLIGATIONS: dict[str, frozenset[str]] = {
    spec.operator_id: spec.routed_diagnostics
    for spec in OPERATORS.values()
    if spec.routed_diagnostics
}

# Track A Option 2: the sanctioned single-file-source formats. Widened from
# parquet-only once compilation sources its types from the resident Arrow
# table rather than a separate descriptor-backed re-read (see the format
# check below); a non-file source (s3/gcs) stays out of scope.
_ADMITTED_SOURCE_FORMATS = frozenset({"parquet", "csv", "fixed_width"})


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

    `boundary_conversion_ms` is the wall-clock cost of the two Arrow/pandas
    crossings `cheap_admission` pays (the `to_pandas_fk_safe` conversion and
    the admission-only round-trip `Table.from_pandas` consistency check).
    `_unified_slice._execute_admitted` adds its output bridge to it, so
    `ExecutionResult.boundary_conversion_ms` is the lane's boundary overhead
    outside the per-node timing scopes, disjoint from `timings`. It is not the
    same quantity as the pandas oracle's `conversion_ms` (the oracle has no
    consistency round-trip), and conversions inside a node's scope, such as
    passthrough assembly, are counted in that node's timing instead.
    """

    table: str
    source: pa.Table
    source_frame: pd.DataFrame
    boundary_conversion_ms: float


def cheap_admission(
    *,
    route: str,
    route_chunked: bool,
    resolved_substrate: str,
    sink: TransactionalSink | None,
    source_loader: Callable[[str], pa.Table] | None,
    fidelity_report: bool,
    post_validation: bool = False,
    vault_writer: Any,
    config: Mapping[str, Any],
    profile: Profile,
    table_kinds: Mapping[str, str],
    caller_sources: Mapping[str, pa.Table | LazySource],
    registry: ProviderRegistry | None = None,
) -> CheapCandidate | None:
    """D3's no-I/O admission half. Returns `None` on ANY doubt -- the caller
    falls through to the unchanged old route without a reason code, matching
    the native lane's own `(None, None)` contract at this same call tier
    (the coded reason is a nice-to-have for a future telemetry pass, not a
    D9 requirement, so it is not threaded through here)."""
    if route != "full_frame" or route_chunked:
        return None
    if resolved_substrate != "pandas":
        # D3: this lane returns a result identical to the PANDAS full-frame
        # route only. Any future non-pandas substrate would run a different
        # adapter and stamp its own provenance telemetry (executed_substrate,
        # boundary conversion timings), so admitting it would diverge on the
        # caller-consumed quality_metrics. Decline to the unchanged old route.
        # `resolved_substrate` is post-resolve_substrate; pandas is the only
        # substrate that resolves (any other value already raised
        # invalid_substrate upstream), so this stays a defence-in-depth guard.
        return None
    if source_loader is not None:
        # A source_loader signals lazy/relationship loading (a different output
        # contract, `_isolated_worker._load_sources(lazy=True)`), outside this
        # slice's single-resident-table scope. Still a hard decline.
        return None
    # `sink` is intentionally NOT a decline (route activation, 2026-09-20). The
    # first check above already established route == "full_frame" (not chunked,
    # not native). The full-frame route never consumes it (auto-chunk is B6a's exception): the legacy
    # full-frame route ignores it (`_isolated_worker.py`'s own comment;
    # `_finalize_outputs` reads `result.outputs`), and the admitted path returns
    # outputs in-memory identically (execution goes through `_execute_admitted`,
    # which is never passed `sink`). The sink's fate is decided by ROUTE, not by
    # which full-frame implementation runs, so admitting a full-frame job that
    # carries an inert sink is behavior-preserving. The streaming/OOC route -- the
    # only one that writes a sink -- is already declined above by the route check.
    # This is what lets the platform worker (which always attaches a
    # ParquetTransactionalSink) reach the certified lane.
    if fidelity_report or post_validation or vault_writer is not None:
        # A1: post_validation forces the full pandas full-frame path (same as
        # fidelity_report) so the finalize seam runs the scan suite; the unified
        # slice returns early without it, so admitting an opted-in job would
        # silently skip post-validation while reporting success.
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
        or source_descriptor.get("format") not in _ADMITTED_SOURCE_FORMATS
    ):
        # Track A Option 2: parquet, csv, and fixed_width all admit here. The
        # divergence risk this check used to guard against -- a csv/fixed_width
        # source profiling under a different (often loosely-typed) reader than
        # the resident Arrow table this lane masks -- is closed by making
        # compilation source its column types from that SAME resident table
        # (`resolve_input_arrow_type`'s `resident_sources` argument, threaded
        # through `_shadow_bindings`/`native/_requirements`), not by excluding
        # the format. A non-file source (s3/gcs) is still out of scope.
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
        # Excludes both "absent" and a `LazySource` kept lazy for the auto-chunk lane
        # (B6b): the unified slice's D5 single-open seam holds only for an already-
        # resident table. Any other lazy source was resolved to a table before this.
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
    if not when_columns_admitted(columns_cfg, source, registry, table=table):
        # A `when:` column runs on this route only when the chunked route's per-column
        # verdict admits it (see `_unified_slice_when`); one miss sends the table to the oracle.
        return None

    try:
        source.validate(full=True)
    except pa.ArrowInvalid:
        # Root-cause fix: prove the ACTUAL resident table is well-formed
        # before anything downstream trusts its buffers, rather than
        # inheriting whatever `pa.Table.validate()`'s cheap default (schema-
        # only) would have missed.
        return None

    # Root-cause fix: this is the SAME Arrow->pandas conversion the legacy route
    # performs on this table (`_pandas_adapter.py`'s `to_pandas_fk_safe`), doing
    # it once here and carrying the frame forward on `CheapCandidate` so
    # `_unified_slice._execute_admitted`'s source-shaped output assembly never
    # re-converts. `fk_columns` is empty (relationships were declined above), but
    # a group_key `group_by` SIBLING is in the oracle's fk-safe set too
    # (`_pandas_adapter.py`'s `group_key_group_by_columns`): an integer sibling
    # must be read through the lossless nullable dtype (`int64`->`Int64`) the
    # oracle uses, or a passthrough sibling's output pandas metadata (numpy_type)
    # diverges from the oracle's and the flag-off/flag-on parity gate fails on
    # `schema.equals(check_metadata=True)`. Mirroring the oracle's per-column
    # routing keeps every non-group_key table on the plain path unchanged (the
    # sibling set is empty then).
    group_key_sibling_cols: set[str] = set()
    for col in columns_cfg:
        if col.get("strategy") != "group_key":
            continue
        pcfg = col.get("provider_config")
        # Mirror the oracle's non-empty guard (`_runner.py` `and group_by`): an
        # empty `""` group_by is never a real sibling column, so it must not
        # enter the routing set even though `to_pandas_fk_safe` would skip it.
        group_by = pcfg.get("group_by") if isinstance(pcfg, dict) else None
        if isinstance(group_by, str) and group_by:
            group_key_sibling_cols.add(group_by)
    conversion_t0 = time.perf_counter()
    try:
        frame = to_pandas_fk_safe(source, group_key_sibling_cols)
    except Exception:
        # Total-to-decline: any Arrow->pandas failure declines to the legacy
        # route rather than raising a different error than legacy's own coded
        # guards. Malformed `b"pandas"` schema metadata, for instance, can make
        # `to_pandas()` raise a JSONDecodeError here, where the legacy route
        # would instead reach its own reject-before-read guard (e.g.
        # `null_bearing_int_unsupported`). Declining preserves failure parity.
        return None
    boundary_conversion_ms = (time.perf_counter() - conversion_t0) * 1000.0
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
    round_trip_t0 = time.perf_counter()
    try:
        round_trip = pa.Table.from_pandas(frame, preserve_index=False)
    except Exception:
        return None
    boundary_conversion_ms += (time.perf_counter() - round_trip_t0) * 1000.0
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

    return CheapCandidate(
        table=table,
        source=source,
        source_frame=frame,
        boundary_conversion_ms=boundary_conversion_ms,
    )


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
    operator from the allowlist, no prepass, no diagnostic obligation beyond
    the routed set, complete 1:1 coverage of the source's columns) AND that the
    ACTUAL resident Arrow table -- not the profile's coarse approximation of
    it -- belongs to the one finite, reviewed equivalence domain this
    slice's 4.4 corpus characterizes, for every node regardless of strategy.

    Track A Option 2 guard reconciliation: this used to run two independent
    per-node type checks -- the resident type against the compiled binding's
    OWN `input_schema` type, and the resident type against `_ADMITTED_
    RESIDENT_TYPES[node.strategy]`. The first is now tautological: `input_
    schema` is built from this SAME resident type (`resolve_input_arrow_type`'s
    `resident_sources` argument, threaded from `execution_binding_for_slice_
    node` through the compiler), not a separate profile re-read, so it can
    never disagree with `source.schema.field(column).type` for a well-formed
    binding. Only the structural shape (exactly one field, correctly named)
    is still checked; the type comparison is dropped. The domain check stays,
    and stays load-bearing: the compiler gates redact/truncate on CONFIG only,
    never on input type (`native/_requirements.py`'s own `redact_config_
    rejection` / `truncate_config_rejection`), so a genuinely non-string
    column bound to either would still compile a "matching" binding.

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
    required_kernels: set[str] = set()
    for node in nodes:
        binding = node.execution
        if binding is None:
            return None
        if node.kind != "scalar" or len(node.columns) != 1:
            return None
        if binding.operator_id not in ALLOWED_OPERATOR_IDS:
            return None
        if binding.required_prepasses:
            return None
        routed = _ROUTED_DIAGNOSTIC_OBLIGATIONS.get(binding.operator_id, frozenset())
        if not set(binding.diagnostic_obligations) <= routed:
            return None
        column = node.columns[0]
        if binding.operator_id == GROUP_KEY_OPERATOR_ID:
            # group_key keys on a SIBLING column, so its resident-type + residency
            # checks must run on that sibling BEFORE the target-name schema lookup
            # below (which would otherwise validate the wrong column, or raise for
            # a binding whose input_schema is keyed by the sibling, not the
            # target). A miss declines cleanly.
            if not _group_key_sibling_admitted(binding, physical_table, source):
                return None
            # group_key consumes its namespace through the compiled raw-hex kernel
            # at every batch, like the other companion-dependent operators.
            key_binding = binding.key_binding
            if key_binding is None:
                return None
            try:
                key_binding.namespace.encode("utf-8")
            except UnicodeEncodeError:
                return None
            required_kernels.add(_OPERATOR_REQUIRED_KERNEL[GROUP_KEY_OPERATOR_ID])
            covered.append(column)
            continue
        resident_type = source.schema.field(column).type
        # Structural shape only (see the docstring): the type-equality half of
        # this check is dropped as tautological now that `input_schema` is
        # itself built from `resident_type`.
        if len(binding.input_schema) != 1 or binding.input_schema.names != [column]:
            return None
        # An untyped/all-null-inferred resident column (`pa.null()`, the type
        # pandas/Arrow give a wholly-null CSV column with no declared dtype)
        # is not in any strategy's admitted domain, so it declines here rather
        # than being treated as compatible with whatever the strategy expects.
        domain = positional_resident_types(node.strategy, binding.params)
        if domain is None:
            # C5c-ii: a deterministic (non-positional) Faker node keys from the source value, so
            # it admits bool/int/uint beside string; the `cheap_admission` round-trip guard above
            # already declined any column whose pandas conversion is not value-identical.
            domain = deterministic_resident_types(node.strategy, binding.params)
        if domain is None:
            domain = _ADMITTED_RESIDENT_TYPES.get(node.strategy, frozenset())
        if resident_type not in domain:
            return None
        if binding.operator_id in _COMPANION_DEPENDENT_OPERATOR_IDS:
            # hash / categorical / bucket_perturb / date_shift / faker consume their namespace
            # through the compiled companion at every batch; a missing KeyBinding
            # or a non-UTF-8 namespace declines to the oracle (the compiled
            # kernel requires an encodable namespace).
            key_binding = binding.key_binding
            if key_binding is None:
                return None
            try:
                key_binding.namespace.encode("utf-8")
            except UnicodeEncodeError:
                return None
            required_kernels.add(_OPERATOR_REQUIRED_KERNEL[binding.operator_id])
        covered.append(column)

    # 1:1 coverage across configured columns / physical nodes / resident
    # columns: `cheap_admission` already proved the resident names unique
    # and the configured columns match them 1:1, so cardinality-matching
    # `covered` against the resident schema here closes the loop across all
    # three sets (not merely a set-equality that would hide a duplicate
    # column claimed by two nodes).
    if len(covered) != len(set(covered)) or set(covered) != set(source.column_names):
        return None

    # PER-OPERATOR companion gate: require only the kernel(s) this table's
    # operators use, so a companion missing an ADDITIVE symbol (e.g. raw-hex)
    # declines just the operators that need it, not every native operator.
    if required_kernels:
        availability = native_kernel_availability()
        if not all(getattr(availability, kernel) for kernel in required_kernels):
            return None
    try:
        reject_null_bearing_int(plan, {table: source}, registry, graph)
    except ExecutionError:
        return None
    return physical_table
