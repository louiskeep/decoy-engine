"""Native-vs-oracle route dispatch for the Phase 1 streaming coordinator (Task 2.7,
extended by Phase 3 Task 3.1 for deterministic-faker pool masking).

Decides, once per table at PREFLIGHT, whether every masked node of `table` can run
on the native route -- a compiled kernel (`_kernels_scalar` / `_kernels_keyed`) or,
for a deterministic-reuse faker column (Task 3.1), a bounded value pool built once
and selected per chunk via the compiled `derive_index_batch` index kernel (Task 2.3)
-- or whether the WHOLE table must run on
the pinned pandas oracle (`decoy_engine.execution._chunked.run_mask_pipeline_chunked`).
The decision is atomic and whole-table: this phase never mixes a native column with
an oracle column in the same table, and it never falls back mid-stream -- a route
decided at preflight holds for every chunk that follows (Decision 10: a green oracle
run is never mistaken for proof the native route ran; the route TAG is the evidence).

Reuses `compile_native_plan`'s per-node `fallback_policy` (Task 2.6, config- and
type-aware) and the live `NATIVE_KERNEL_STRATEGIES` / `NATIVE_POOL_STRATEGIES`
allowlists (`_requirements.py`) as the single source of truth for "this strategy has
a native execution path this phase" rather than recomputing an admitted set. The two
allowlists stay distinct on purpose: a kernel strategy has a compiled Rust kernel, a
pool strategy (faker) runs the Python `PoolSampler` per chunk instead -- conflating
them would misdescribe what actually executes. This closes the FK-composite
`<group>` trap exactly: a `composite_fk_group` node's STATIC capabilities read as
native-ready (row-local, static output type, no group kernel needed to see
that), so `fallback_policy` alone resolves to `"native"` for it -- but no group
kernel exists. Gating on `node.kind == "scalar"` in addition to `fallback_policy`
excludes it (and any `composite` bundle node) without touching `_plan.py`.

The chunked coordinator's own profile (`_chunked_profile.first_chunk_profile`)
always reports `relationships=()`, so the FK-child reroute keys off the
CONFIG-declared relationships (`config["relationships"]`, the same source the
oracle's `_chunked_fk.py` walks), NOT the empty profile. Any table on either side
of a declared FK edge is rerouted to the oracle (narrower, never wider): FK
streaming is deferred (Part 2 Phase 4) and this phase reimplements none of the
parent-key/orphan-policy machinery, so admitting an FK child natively would skip
the oracle's referential-integrity enforcement. `<group>`-node exclusion via the
`kind == "scalar"` gate remains as a second, capability-level guard.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import pyarrow as pa

from decoy_engine.execution._transforms_gate import reject_per_table_transforms
from decoy_engine.execution.native._categorical_positional import (
    positional_config_for_column,
)
from decoy_engine.execution.native._chunk_masking import (  # noqa: F401 -- re-exported for tests
    _mask_chunk_native,
    _resolve_faker_pools,
    _resolve_truncate_keep,
)
from decoy_engine.execution.native._chunk_schema import NativeChunkSchemaDriftError
from decoy_engine.execution.native._chunked_group_key_gate import (
    group_by_columns,
    order_dependence_rejection,
    sibling_resident_sources,
)
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.native._group_key_ext import (
    RawHexDerivationKernel,
    load_compiled_raw_hex_kernel,
)
from decoy_engine.execution.native._index_ext import (
    IndexDerivationKernel,
    load_compiled_index_kernel,
)
from decoy_engine.execution.native._plan import compile_native_plan
from decoy_engine.execution.native._real_type_admission import real_type_rejection
from decoy_engine.execution.native._requirements import (
    CHUNKED_ROUTE_VETOED_STRATEGIES,
    NATIVE_KERNEL_STRATEGIES,
    NATIVE_POOL_STRATEGIES,
)
from decoy_engine.generation.pool import PoolCache

RouteTag = Literal["native_kernel", "native_pool", "oracle"]

# Strategies whose native chunk path calls the compiled index kernel, so preflight
# loads and self-tests it once and downgrades the table when it is missing.
_INDEX_KERNEL_STRATEGIES = frozenset({"faker", "categorical", "bucket_perturb", "date_shift"})


@dataclass(frozen=True)
class NodeRouteRecord:
    """The route one masked column took, for job evidence.

    Decision 10: job SUCCESS is never the proof a route ran; this record (and
    `NativeRouteEvidence.compiled_kernel_executed`, set once a compiled non-pool kernel
    invocation completes) is.
    """

    column: str
    strategy: str
    route: RouteTag


@dataclass
class NativeRouteEvidence:
    """Job evidence for `table`'s route decision and what actually ran.

    `native_admitted`, `reroute_reason`, and `node_routes` are fixed at PREFLIGHT
    -- before any chunk is processed -- and never change mid-stream (no mid-stream
    fallback). `compiled_kernel_executed` and `kernel_calls` /
    `kernel_elapsed_s` are RUNTIME counters: they start at zero even when
    `native_admitted` is True and only move once a chunk actually runs through a
    kernel, so they prove a real invocation happened rather than restating intent.
    """

    table: str
    native_admitted: bool
    reroute_reason: str | None
    node_routes: tuple[NodeRouteRecord, ...]
    compiled_kernel_executed: bool = False
    kernel_calls: dict[str, int] = field(default_factory=dict)
    kernel_elapsed_s: dict[str, float] = field(default_factory=dict)
    # Task 3.1 Step 7: the pool-selection counterpart of
    # `compiled_kernel_executed` / `kernel_calls`. The faker route runs its own
    # compiled kernel (`derive_index_batch`, Task 2.3), so it keeps its own
    # proof-of-execution pair rather than overloading the non-pool kernel fields
    # (`compiled_kernel_executed`: at least one compiled non-pool kernel invocation
    # completed; `kernel_calls`: branch executions per strategy);
    # `pool_select_calls` counts one unit per (column, chunk) selection, feeding
    # Task 3.6's exact-count route ledger.
    pool_select_executed: bool = False
    pool_select_calls: int = 0


def _oracle_evidence(
    table: str, reason: str, node_routes: tuple[NodeRouteRecord, ...] = ()
) -> NativeRouteEvidence:
    return NativeRouteEvidence(
        table=table, native_admitted=False, reroute_reason=reason, node_routes=node_routes
    )


def _downgrade_to_oracle(decision: NativeRouteEvidence, reason: str) -> NativeRouteEvidence:
    """Reroute an already-admitted decision to the oracle, re-tagging every
    node's route from `native_kernel` to `oracle` (never a per-column mix)."""
    return NativeRouteEvidence(
        table=decision.table,
        native_admitted=False,
        reroute_reason=reason,
        node_routes=tuple(
            NodeRouteRecord(column=r.column, strategy=r.strategy, route="oracle")
            for r in decision.node_routes
        ),
    )


def _route_tag_for(strategy: str) -> RouteTag:
    """The per-column route tag for an ADMITTED strategy: `"native_pool"` for
    a bounded-value-pool strategy (faker: pool selection via the compiled
    `derive_index_batch` kernel), `"native_kernel"` for everything else in
    `NATIVE_KERNEL_STRATEGIES`. Distinct tags keep `compiled_kernel_executed` (at
    least one compiled non-pool kernel invocation completed) from being misread as
    also proving a pool selection ran, and vice versa.
    """
    return "native_pool" if strategy in NATIVE_POOL_STRATEGIES else "native_kernel"


def _table_in_declared_relationship(config: dict[str, Any], table: str) -> bool:
    """True when `config` declares `table` on either side of any FK relationship.

    Reads the CONFIG-declared edges -- the same `config["relationships"]` the
    oracle's FK machinery (`_chunked_fk.py`) walks -- NOT `profile.relationships`,
    which the chunked coordinator's `first_chunk_profile` always leaves empty. A
    profile-based check therefore never fires in production and would admit an FK
    child natively, bypassing the oracle's orphan-policy (referential-integrity)
    enforcement. Any FK participation (parent or child) reroutes to the oracle:
    FK streaming is deferred (Part 2 Phase 4), and this phase reimplements none of
    the parent-key/orphan machinery, so admitting either side natively would be
    wider, not narrower, than the oracle contract.
    """
    for rel_entry in config.get("relationships") or ():
        if not isinstance(rel_entry, dict):
            continue
        parent = rel_entry.get("parent")
        if isinstance(parent, dict) and parent.get("table") == table:
            return True
        for child_info in rel_entry.get("children") or ():
            if isinstance(child_info, dict) and child_info.get("table") == table:
                return True
    return False


def _first_when_column(config: dict[str, Any], table: str) -> str | None:
    """The first column of `table` carrying a nonblank `when:` predicate.

    A conditional column masks only the rows its predicate selects, and leaves the
    rest with their original value and type, so the native kernels (which mask
    every row) must not run it. A blank predicate has no effect and does not veto;
    this is the same normalization the plan compiler applies.
    """
    for table_cfg in config.get("tables") or ():
        if not isinstance(table_cfg, dict) or table_cfg.get("name") != table:
            continue
        for col in table_cfg.get("columns") or ():
            if isinstance(col, dict):
                when = col.get("when")
                if isinstance(when, str) and when.strip():
                    return str(col.get("name", "?"))
    return None


def _static_route_decision(
    config: dict[str, Any],
    profile: Any,
    *,
    table: str,
    engine_version: str,
    registry: Any = None,
    first_schema: pa.Schema | None = None,
) -> NativeRouteEvidence:
    """Config/profile-only admission: no I/O, no compiled-extension probe.

    A table admits only when EVERY masked node is a `scalar` node whose resolved
    `fallback_policy` is `"native"` AND whose strategy is in the live
    `NATIVE_KERNEL_STRATEGIES` OR `NATIVE_POOL_STRATEGIES` allowlist (Task 3.1: a
    deterministic-reuse faker column resolves `fallback_policy == "native"` too,
    via `_requirements.py`'s combined kernel-or-pool check). A `composite_fk_group`
    (`<group>`) or `composite` node fails the `kind == "scalar"` check regardless
    of its `fallback_policy`, which is exactly what excludes a table with a
    `<group>` node from this phase's native route.
    """
    # Any FK relationship touching `table` reroutes the whole table, keyed off the
    # CONFIG-declared edges (the chunked coordinator's profile always reports
    # `relationships=()`, so a profile-based check is inert here). A composite FK
    # child also collapses into a `<group>` node the `kind == "scalar"` gate below
    # would exclude, but that node is only present when the profile carries the
    # relationship; keying off config catches both the composite and the
    # single-column FK child under the production profile shape, before either can
    # bypass the oracle's orphan-policy enforcement.
    if _table_in_declared_relationship(config, table):
        return _oracle_evidence(table, "fk_relationship_not_native_route")

    plan = compile_native_plan(
        config,
        profile,
        engine_version=engine_version,
        registry=registry,
        resident_sources=sibling_resident_sources(config, table, first_schema),
    )
    table_nodes = [n for n in plan.nodes if n.table == table]
    if not table_nodes:
        return _oracle_evidence(table, "no_mask_nodes")

    # Two passes: first decide native_admitted from EVERY node (a rejection on
    # node 5 must still veto nodes 1-4), then stamp the table's ONE resulting
    # route onto every scalar column -- never a per-column mix, since a
    # non-admitted table runs 100% on the oracle. Non-scalar nodes
    # (`<group>` / `<composite>`) have no single column to tag and stay out of
    # `node_routes`; their exclusion is recorded in `reroute_reason` instead.
    reasons: list[str] = []
    scalar_columns: list[tuple[str, str]] = []
    for node in table_nodes:
        label = ",".join(node.columns) if node.columns else "?"
        if node.kind != "scalar":
            reasons.append(f"non_scalar_node:{node.kind}:{label}")
            continue
        column = node.columns[0]
        scalar_columns.append((column, node.strategy))
        if node.strategy in CHUNKED_ROUTE_VETOED_STRATEGIES:
            # A strategy native on the full-frame route but vetoed on this chunked route
            # would otherwise resolve fallback_policy == "native" and reach a missing chunk
            # handler, so veto the whole table to the oracle here. The set is empty today;
            # the reason carries the strategy so a future veto stays distinguishable.
            reasons.append(f"{node.strategy}_not_native_chunked_route:{column}")
            continue
        if node.strategy == "group_key" and node.fallback_policy == "native":
            # The sibling must reach group_key unmasked, or the native leg (which masks from
            # the source chunk) would read a different value than the oracle.
            group_by = group_by_columns(config, table).get(column)
            order_reason = (
                order_dependence_rejection(column, group_by, table_nodes) if group_by else None
            )
            if order_reason is not None:
                reasons.append(order_reason)
                continue
        no_kernel = node.strategy not in NATIVE_KERNEL_STRATEGIES
        no_pool_path = node.strategy not in NATIVE_POOL_STRATEGIES
        # The seeded non-deterministic categorical resolves a non-native policy (the
        # full-frame operator is source-keyed); this chunked-only route admits it, config only.
        positional = (
            node.strategy == "categorical"
            and positional_config_for_column(config, table, column) is not None
        )
        if node.fallback_policy != "native" and not positional:
            reasons.append(f"fallback_policy_not_native:{column}:{node.fallback_policy}")
        elif no_kernel and no_pool_path:
            # Defense in depth (see module docstring): `fallback_policy` already
            # encodes this via `_requirements.py`'s combined kernel-or-pool
            # check, so this branch should be unreachable for an admitted
            # node. Kept as a second, independent guard against the two
            # allowlists drifting apart; a faker node satisfying JC-5 is in
            # NATIVE_POOL_STRATEGIES, so it passes here alongside the
            # compiled-kernel strategies.
            reasons.append(f"no_native_kernel_or_pool:{column}:{node.strategy}")

    native_admitted = not reasons
    node_routes = tuple(
        NodeRouteRecord(
            column=column,
            strategy=strategy,
            route=_route_tag_for(strategy) if native_admitted else "oracle",
        )
        for column, strategy in scalar_columns
    )
    return NativeRouteEvidence(
        table=table,
        native_admitted=native_admitted,
        reroute_reason=None if native_admitted else "; ".join(reasons),
        node_routes=node_routes,
    )


@dataclass(frozen=True)
class NativePreflight:
    """The full PREFLIGHT result for `table`: the route evidence, plus the
    index-derivation kernel wrapper (Task 2.3) verified for this decision.

    `index_kernel` is `None` whenever the route is not native-admitted or
    admits no faker, categorical, bucket_perturb or date_shift node -- there is
    nothing to derive, so nothing was loaded. `raw_hex_kernel` is likewise `None`
    unless an admitted group_key node needs it.
    Loading + self-testing the kernel happens ONCE here, at preflight, never
    per chunk; the verified wrapper is threaded through `_chunked_entry._native_route`
    -> `_mask_chunk_native`, which hands it to each index-kernel column's operator.
    """

    evidence: NativeRouteEvidence
    index_kernel: IndexDerivationKernel | None
    # Source columns the plan does not cover that the native route carries unchanged
    # (admitted only under the `warn` policy, see `plan_native_route`).
    unconfigured_passthrough: tuple[str, ...] = ()
    # The raw-hex kernel group_key derives with (a different compiled entry point from the
    # index kernel), loaded and self-tested once here for any admitted group_key node.
    raw_hex_kernel: RawHexDerivationKernel | None = None


def plan_native_route(
    config: dict[str, Any],
    profile: Any,
    *,
    table: str,
    engine_version: str,
    first_schema: pa.Schema | None = None,
    adapter: Any = None,
    unconfigured_policy: Literal["warn", "error"] | None = None,
    registry: Any,
) -> NativePreflight:
    """The full PREFLIGHT decision for `table`: config/profile admission, then
    (when `first_schema` is given) the actual first-chunk coverage + faker
    source-type guards, then (only when still admitted) the required companion
    probes -- crypto (any `hash` node) and index (any admitted `faker`,
    `categorical`, `bucket_perturb` or `date_shift` node), then raw-hex (any admitted
    `group_key` node) -- in that order. Guards run BEFORE probes so a schema-rejected table (an
    uncovered column, or a faker node over a non-string source) keeps its own
    reroute reason and never reaches either probe; each probe is itself gated
    on the decision still being admitted, so a rejection from an earlier probe
    (or guard) short-circuits the rest. A missing or ABI-incompatible companion
    downgrades the WHOLE table to the oracle -- never just the offending column
    -- matching the no-partial-native-output rule; the other admitted columns
    never touch a native kernel either, since the route decision is atomic per
    table.

    `first_schema` is `None` for admission-only callers (e.g. tests probing
    the static/config-level decision alone): they get static admission plus
    the companion probes, with no schema-based guard applied -- exactly what
    those callers assert.

    A nonblank `when:` predicate on any column, or an `adapter` that is neither
    `None` nor the pandas adapter, reroutes the whole table to the oracle right
    after static admission (reasons `when_predicate_not_native:<column>` and
    `adapter_requested`; the adapter reason applies only to a table that would
    otherwise admit).

    `unconfigured_policy` is the resolved `unconfigured_column_policy`. Only `"warn"`
    lets a source column the plan does not cover stay on the native route, where it is
    carried unchanged (`NativePreflight.unconfigured_passthrough`); `None` and `"error"`
    keep the veto, so the oracle raises `undeclared_output_columns` as before. A
    configured column the source lacks always vetoes. `registry` is the run's provider
    registry; it decides which providers are composite nodes.
    """
    decision = _static_route_decision(
        config,
        profile,
        table=table,
        engine_version=engine_version,
        registry=registry,
        first_schema=first_schema,
    )
    # A `when:` predicate names the reroute reason even when another column would
    # have vetoed the table anyway: its meaning (leave unselected rows untouched)
    # is the one a caller can act on.
    when_column = _first_when_column(config, table)
    if when_column is not None:
        decision = _downgrade_to_oracle(decision, f"when_predicate_not_native:{when_column}")
    elif decision.native_admitted and adapter is not None:
        from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

        # The native route masks through Arrow kernels and never calls an
        # adapter, so a caller that asked for any adapter but the stock pandas
        # one gets the oracle route, which calls it. An exact type check: a
        # subclass may override `run`, and the native route would bypass that.
        if type(adapter) is not PandasExecutionAdapter:
            decision = _downgrade_to_oracle(decision, "adapter_requested")
    if not decision.native_admitted:
        return NativePreflight(decision, None, raw_hex_kernel=None)

    unconfigured: tuple[str, ...] = ()
    if first_schema is not None:
        if decision.native_admitted:
            covered = {n.column for n in decision.node_routes}
            from decoy_engine.execution._transforms import stored_index_fields

            # A stored pandas index field is consumed as the index on the oracle route and
            # never yielded, so it is neither covered nor unconfigured.
            stored = stored_index_fields(first_schema)
            uncovered = [n for n in first_schema.names if n not in covered and n not in stored]
            missing = covered - set(first_schema.names)
            if missing or (uncovered and unconfigured_policy != "warn"):
                # Report BOTH sides of the symmetric difference: a column this chunk has
                # that the plan does not cover alone would read as "nothing extra" when
                # the real drift is a configured column this chunk is missing entirely.
                decision = _downgrade_to_oracle(
                    decision,
                    f"uncovered_columns:{sorted(uncovered)};"
                    f"missing_configured_columns:{sorted(missing)}",
                )
            else:
                unconfigured = tuple(uncovered)

        if decision.native_admitted:
            # A non-string faker source's per-chunk Arrow type can drift across
            # chunks (a nullable Int64 source can materialize as float64 in a
            # later chunk, 3 -> 3.0), and the compiled index kernel's admitted
            # input and canonicalization are scoped to the one variant C1 needs
            # (string/large_string): admitting anything else risks a rejection
            # surfacing only on a later chunk, after earlier chunks already
            # yielded -- partial native output the whole-frame oracle never
            # produces. C1's faker columns are string-typed; a faker column over
            # a non-string source reroutes the WHOLE table to the oracle
            # (narrower, never wider).
            for node in decision.node_routes:
                if node.strategy != "faker":
                    continue
                ftype = first_schema.field(node.column).type
                if not (pa.types.is_string(ftype) or pa.types.is_large_string(ftype)):
                    decision = _downgrade_to_oracle(
                        decision, f"faker_source_type_not_string:{node.column}:{ftype}"
                    )
                    break

        if decision.native_admitted:
            # Admission rests on the first chunk's real Arrow types and each
            # provider's output type, never the profile's coarse dtype labels.
            reason = real_type_rejection(
                config, decision.node_routes, first_schema, table=table, profile=profile
            )
            if reason is not None:
                decision = _downgrade_to_oracle(decision, reason)

    if decision.native_admitted and any(n.strategy == "hash" for n in decision.node_routes):
        try:
            load_compiled_crypto_kernel()
        except CryptoExtensionUnavailableError:
            decision = _downgrade_to_oracle(decision, "crypto_extension_unavailable")

    index_kernel: IndexDerivationKernel | None = None
    if decision.native_admitted and any(
        n.strategy in _INDEX_KERNEL_STRATEGIES for n in decision.node_routes
    ):
        try:
            index_kernel = load_compiled_index_kernel()
        except CryptoExtensionUnavailableError:
            decision = _downgrade_to_oracle(decision, "index_extension_unavailable")
            index_kernel = None

    raw_hex_kernel: RawHexDerivationKernel | None = None
    if decision.native_admitted and any(n.strategy == "group_key" for n in decision.node_routes):
        # One code for every loader failure (no companion, ABI mismatch, missing
        # `derive_hex_raw_batch`, failed load-time self-test): the table runs on the oracle.
        try:
            raw_hex_kernel = load_compiled_raw_hex_kernel()
        except CryptoExtensionUnavailableError:
            decision = _downgrade_to_oracle(decision, "raw_hex_extension_unavailable")
            raw_hex_kernel = None

    return NativePreflight(decision, index_kernel, unconfigured, raw_hex_kernel)


def run_native_or_oracle_chunked(
    config: dict[str, Any],
    chunks: Iterable[pa.Table],
    *,
    table: str,
    engine_version: str,
    key_provider: Any = None,
    route_evidence_sink: list[NativeRouteEvidence] | None = None,
    pool_cache: PoolCache | None = None,
    native_threads: int | None = None,
) -> Iterator[pa.Table]:
    """Mask `table` chunk-by-chunk, routing every node to the native kernels
    when the WHOLE table admits (Task 2.7), or to the pinned pandas oracle
    (`run_mask_pipeline_chunked`) otherwise. Same byte-parity contract as the
    oracle coordinator: concatenating the yielded chunks equals the full-frame
    run (reconciling the two routes' output-schema artifacts is the caller's
    job, per `tests/parity/native/test_phase2_gate.py`).

    The route decision runs EAGERLY, before any chunk is yielded, matching the
    oracle coordinator's own eager-validation contract; only the per-chunk
    masking is lazy. `route_evidence_sink`, when given, receives ONE
    `NativeRouteEvidence` immediately (mirroring `_chunked.py`'s
    `chunk_result_sink` pattern): its route fields are fixed at that point,
    while `compiled_kernel_executed` / `kernel_calls` mutate as the returned
    iterator is actually consumed, so a caller that never exhausts the iterator
    correctly sees no kernel executed yet.

    `pool_cache`, when given, lets a caller share ONE `PoolCache` across
    multiple `run_native_or_oracle_chunked` calls (e.g. two tables, or two
    invocations in the same job) so a faker pool built for one call is warm
    for the next; omitted, each call gets its own fresh cache (Task 3.1's
    per-invocation default, mirroring `_chunked.py`'s `run_mask_pipeline_chunked`).

    `native_threads` is the per-job native thread budget passed down to the compiled
    keyed-hash kernel (`derive_batch`) and, since Task 2.3, the compiled index kernel
    (`derive_index_batch`) a faker column's selection uses. `None` (the default)
    means one thread, so output is byte-identical to the serial path and every
    existing caller is unaffected; an explicit count lets either kernel derive rows
    in parallel within the one shared pool. It changes only throughput, never
    output bytes (both compiled kernels are thread-invariant).

    A table that declares transforms raises `per_table_transforms_present`
    before any chunk is read: this entry masks raw chunks, so the ops would be
    dropped silently.
    """
    # Before `_run_chunked` touches `chunks`: a rejected job must not consume a
    # chunk, and this entry keeps its own route name in the message.
    reject_per_table_transforms(config, table=table, route="native chunked execution")
    # Lazy import: `_chunked_entry` imports this module for `plan_native_route`.
    from decoy_engine.execution.native._chunked_entry import _run_chunked

    return _run_chunked(
        config,
        chunks,
        table=table,
        engine_version=engine_version,
        key_provider=key_provider,
        native_threads=native_threads,
        route_evidence_sink=route_evidence_sink,
        pool_cache=pool_cache,
        enforce_schema_rule=False,
    )


__all__ = [
    "NativeChunkSchemaDriftError",
    "NativePreflight",
    "NativeRouteEvidence",
    "NodeRouteRecord",
    "plan_native_route",
    "run_native_or_oracle_chunked",
]
