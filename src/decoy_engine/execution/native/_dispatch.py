"""Native-vs-oracle route dispatch for the Phase 1 streaming coordinator (Task 2.7,
extended by Phase 3 Task 3.1 for deterministic-faker pool masking).

Decides, once per table at PREFLIGHT, whether every masked node of `table` can run
on the native route -- a compiled kernel (`_kernels_scalar` / `_kernels_keyed`) or,
for a deterministic-reuse faker column (Task 3.1), a bounded value pool built once
and selected per chunk via `PoolSampler` -- or whether the WHOLE table must run on
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

from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution.native._chunk_masking import (
    _mask_chunk_native,
    _resolve_faker_pools,
    _resolve_truncate_keep,  # noqa: F401 -- re-exported (tests access via _dispatch._resolve_truncate_keep)
)
from decoy_engine.execution.native._chunk_schema import (
    NativeChunkSchemaDriftError,
    _check_chunk_schema_drift,
)
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.native._index_ext import (
    IndexDerivationKernel,
    load_compiled_index_kernel,
)
from decoy_engine.execution.native._plan import compile_native_plan
from decoy_engine.execution.native._requirements import (
    NATIVE_KERNEL_STRATEGIES,
    NATIVE_POOL_STRATEGIES,
)
from decoy_engine.generation.pool import PoolCache

RouteTag = Literal["native_kernel", "native_pool", "oracle"]


@dataclass(frozen=True)
class NodeRouteRecord:
    """The route one masked column took, for job evidence.

    Decision 10: job SUCCESS is never the proof a route ran; this record (and
    `NativeRouteEvidence.compiled_kernel_executed` for hash) is.
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
    # `compiled_kernel_executed` / `kernel_calls`. A faker column has no
    # compiled kernel, so it needs its own proof-of-execution pair rather
    # than overloading the hash-kernel fields; `pool_select_calls` counts
    # one unit per (column, chunk) selection, feeding Task 3.6's exact-count
    # route ledger.
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
    a bounded-value-pool strategy (faker, no compiled kernel), `"native_kernel"`
    for everything else in `NATIVE_KERNEL_STRATEGIES`. Distinct tags keep
    `compiled_kernel_executed` (a real Rust-kernel-invocation proof) from being
    misread as also proving a pool selection ran, and vice versa.
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


def _static_route_decision(
    config: dict[str, Any], profile: Any, *, table: str, engine_version: str
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

    plan = compile_native_plan(config, profile, engine_version=engine_version)
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
        no_kernel = node.strategy not in NATIVE_KERNEL_STRATEGIES
        no_pool_path = node.strategy not in NATIVE_POOL_STRATEGIES
        if node.fallback_policy != "native":
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
    admits no faker node -- there is nothing to select, so nothing was loaded.
    Loading + self-testing the kernel happens ONCE here, at preflight, never
    per chunk; the verified wrapper is threaded through `_mask_native` ->
    `_mask_chunk_native` -> `_sample_faker_chunk` so each faker column-chunk
    makes exactly one real batch call.
    """

    evidence: NativeRouteEvidence
    index_kernel: IndexDerivationKernel | None


def plan_native_route(
    config: dict[str, Any],
    profile: Any,
    *,
    table: str,
    engine_version: str,
    first_schema: pa.Schema | None = None,
) -> NativePreflight:
    """The full PREFLIGHT decision for `table`: config/profile admission, then
    (when `first_schema` is given) the actual first-chunk coverage + faker
    source-type guards, then (only when still admitted) the required companion
    probes -- crypto (any `hash` node) and index (any admitted `faker` node) --
    in that order. Guards run BEFORE probes so a schema-rejected table (an
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
    """
    decision = _static_route_decision(config, profile, table=table, engine_version=engine_version)
    if not decision.native_admitted:
        return NativePreflight(decision, None)

    if first_schema is not None:
        if decision.native_admitted:
            covered = {n.column for n in decision.node_routes}
            actual = set(first_schema.names)
            if actual != covered:
                # Narrower, never wider: a column the compiled plan does not cover
                # (e.g. an unconfigured-column policy) is not something this phase's
                # native path has reasoned about, so the whole table reroutes. Report
                # BOTH sides of the symmetric difference: `actual - covered` (a column
                # this chunk has that the plan does not cover) alone would silently
                # read as "nothing extra" when the real drift is the OTHER direction --
                # a configured column the compiled plan expects that this chunk is
                # missing entirely (`covered - actual`). The `!=` check above already
                # reroutes correctly on either side; only the diagnostic was one-sided.
                decision = _downgrade_to_oracle(
                    decision,
                    "uncovered_columns:"
                    f"{sorted(actual - covered)};missing_configured_columns:"
                    f"{sorted(covered - actual)}",
                )

        if decision.native_admitted:
            # Native faker selection converts the source column with per-chunk
            # `source.to_pandas()` (_sample_faker_chunk), which diverges from the
            # oracle's table-level `Table.to_pandas()` for non-string nullable
            # extension types: a nullable Int64 source can materialize as float64 in
            # a later chunk (3 -> 3.0), so deterministic canonicalization raises
            # `float_canonicalization_unsupported` AFTER earlier chunks already
            # yielded -- partial native output the whole-frame oracle never produces.
            # C1's faker columns are string-typed; a faker column over a non-string
            # source reroutes the WHOLE table to the oracle (narrower, never wider).
            for node in decision.node_routes:
                if node.strategy != "faker":
                    continue
                ftype = first_schema.field(node.column).type
                if not (pa.types.is_string(ftype) or pa.types.is_large_string(ftype)):
                    decision = _downgrade_to_oracle(
                        decision, f"faker_source_type_not_string:{node.column}:{ftype}"
                    )
                    break

    if decision.native_admitted and any(n.strategy == "hash" for n in decision.node_routes):
        try:
            load_compiled_crypto_kernel()
        except CryptoExtensionUnavailableError:
            decision = _downgrade_to_oracle(decision, "crypto_extension_unavailable")

    index_kernel: IndexDerivationKernel | None = None
    if decision.native_admitted and any(n.strategy == "faker" for n in decision.node_routes):
        try:
            index_kernel = load_compiled_index_kernel()
        except CryptoExtensionUnavailableError:
            decision = _downgrade_to_oracle(decision, "index_extension_unavailable")
            index_kernel = None

    return NativePreflight(decision, index_kernel)


def _rechain(first: pa.Table, rest: Iterator[pa.Table]) -> Iterator[pa.Table]:
    yield first
    yield from rest


def _mask_native(
    config: dict[str, Any],
    chunks: Iterator[pa.Table],
    *,
    table: str,
    engine_version: str,
    key_provider: Any,
    evidence: NativeRouteEvidence,
    pool_cache: PoolCache | None = None,
    native_threads: int | None = None,
    index_kernel: IndexDerivationKernel | None = None,
) -> Iterator[pa.Table]:
    """Eagerly resolve the plan + mask key, then return the lazy per-chunk
    native masking generator (mirrors `run_mask_pipeline_chunked`'s own
    eager-validation-then-lazy-masking contract). Every admitted faker
    column's pool is resolved here too (Task 3.1 Step 2), before any chunk
    is masked, so the pool is built exactly once per invocation. `index_kernel`
    is the preflight-verified compiled index kernel (Task 2.3), threaded
    through to `_mask_chunk_native` -> `_sample_faker_chunk` so every faker
    column-chunk selection makes exactly one real batch call; it is `None`
    whenever the admitted table has no faker node."""
    from decoy_engine.keyprovider import require_mask_key
    from decoy_engine.plan import compile_plan

    first = next(chunks, None)
    if first is None:
        return iter(())
    profile = first_chunk_profile(first, table=table, engine_version=engine_version)
    plan = compile_plan(config, profile, decoy_engine_version=engine_version, no_profile=True)

    if key_provider is None:
        ref = (config.get("global_settings") or {}).get("mask_secret_ref")
        if ref:
            from decoy_engine.keyprovider import key_provider_from_ref

            key_provider = key_provider_from_ref(ref)
    mask_key = require_mask_key(plan, key_provider)

    table_seed = next((ts for (name, ts) in plan.seed_envelope.per_table if name == table), None)
    if table_seed is None:  # pragma: no cover - admission implies a seed envelope
        raise AssertionError(
            f"native route admitted {table!r} but the compiled plan has no seed "
            "envelope for it; the admission precondition should have excluded this."
        )
    col_seed_by_name = dict(table_seed.per_column)
    cache = pool_cache if pool_cache is not None else PoolCache()
    pool_by_column = _resolve_faker_pools(
        col_seed_by_name, job_seed=plan.seed_envelope.job_seed, pool_cache=cache
    )

    def _masked() -> Iterator[pa.Table]:
        expected_schema = first.schema
        for i, chunk in enumerate(_rechain(first, chunks)):
            _check_chunk_schema_drift(expected_schema, chunk, table=table, chunk_index=i)
            yield _mask_chunk_native(
                chunk,
                col_seed_by_name=col_seed_by_name,
                mask_key=mask_key,
                evidence=evidence,
                pool_by_column=pool_by_column,
                native_threads=native_threads,
                index_kernel=index_kernel,
            )

    return _masked()


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
    """
    chunk_iter = iter(chunks)
    first = next(chunk_iter, None)
    if first is None:
        decision = _oracle_evidence(table, "empty_input")
        if route_evidence_sink is not None:
            route_evidence_sink.append(decision)
        return run_mask_pipeline_chunked(
            config,
            chunk_iter,
            table=table,
            engine_version=engine_version,
            key_provider=key_provider,
        )

    profile = first_chunk_profile(first, table=table, engine_version=engine_version)
    preflight = plan_native_route(
        config,
        profile,
        table=table,
        engine_version=engine_version,
        first_schema=first.schema,
    )
    decision = preflight.evidence

    if route_evidence_sink is not None:
        route_evidence_sink.append(decision)

    restored = _rechain(first, chunk_iter)
    if not decision.native_admitted:
        return run_mask_pipeline_chunked(
            config, restored, table=table, engine_version=engine_version, key_provider=key_provider
        )
    return _mask_native(
        config,
        restored,
        table=table,
        engine_version=engine_version,
        key_provider=key_provider,
        evidence=decision,
        pool_cache=pool_cache,
        native_threads=native_threads,
        index_kernel=preflight.index_kernel,
    )


__all__ = [
    "NativeChunkSchemaDriftError",
    "NativePreflight",
    "NativeRouteEvidence",
    "NodeRouteRecord",
    "plan_native_route",
    "run_native_or_oracle_chunked",
]
