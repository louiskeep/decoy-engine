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

import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
import pyarrow as pa

from decoy_engine.errors import DecoyError
from decoy_engine.execution._adapter import provider_config_to_dict
from decoy_engine.execution._chunked import run_mask_pipeline_chunked
from decoy_engine.execution._chunked_profile import first_chunk_profile
from decoy_engine.execution.native._crypto_ext import (
    CryptoExtensionUnavailableError,
    load_compiled_crypto_kernel,
)
from decoy_engine.execution.native._kernels_keyed import native_keyed_hash
from decoy_engine.execution.native._kernels_scalar import (
    native_passthrough,
    native_redact,
    native_truncate,
)
from decoy_engine.execution.native._plan import compile_native_plan
from decoy_engine.execution.native._requirements import (
    NATIVE_KERNEL_STRATEGIES,
    NATIVE_POOL_STRATEGIES,
)
from decoy_engine.generation.pool import (
    CardinalityMode,
    PoolBuilder,
    PoolCache,
    PoolSampler,
    ValuePool,
)
from decoy_engine.generation.pool._identity import DEFAULT_POOL_SCALE, resolve_faker_pool_identity
from decoy_engine.providers_v2 import get_default_registry

RouteTag = Literal["native_kernel", "native_pool", "oracle"]


class NativeChunkSchemaDriftError(DecoyError):
    """A chunk after the first no longer matches the schema the native route
    admitted at PREFLIGHT: a column went missing, an extra one appeared, or a
    column's Arrow type changed. Preflight only inspects the FIRST chunk (an
    eager check before any masking starts), so nothing upstream of this class
    catches a later chunk drifting -- without it, a missing column is silently
    dropped from the output, an extra one raises an uncoded `KeyError`, and a
    changed type is fed to a kernel that never validated it. Raised BEFORE the
    drifting chunk is yielded (Decision 10): chunks already consumed by the
    caller are unaffected, but there is no reversal of them.
    """

    code: str = "native_chunk_schema_drift"

    def __init__(self, message: str, *, table: str, chunk_index: int, detail: str) -> None:
        super().__init__(message)
        self.table = table
        self.chunk_index = chunk_index
        self.detail = detail


def _check_chunk_schema_drift(
    expected: pa.Schema, chunk: pa.Table, *, table: str, chunk_index: int
) -> None:
    """Raise `NativeChunkSchemaDriftError` if `chunk` no longer matches
    `expected` (the admitted first chunk's schema): a missing/extra column
    name, or a changed Arrow type on a column present in both."""
    expected_names = set(expected.names)
    actual_names = set(chunk.schema.names)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        raise NativeChunkSchemaDriftError(
            f"{table!r} chunk {chunk_index}: schema drift vs the admitted first "
            f"chunk (missing={missing}, extra={extra})",
            table=table,
            chunk_index=chunk_index,
            detail=f"missing:{missing};extra:{extra}",
        )
    for name in expected.names:
        expected_type = expected.field(name).type
        actual_type = chunk.schema.field(name).type
        if actual_type != expected_type:
            raise NativeChunkSchemaDriftError(
                f"{table!r} chunk {chunk_index}: column {name!r} type changed "
                f"{expected_type} -> {actual_type}",
                table=table,
                chunk_index=chunk_index,
                detail=f"type_changed:{name}:{expected_type}->{actual_type}",
            )


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


def plan_native_route(
    config: dict[str, Any], profile: Any, *, table: str, engine_version: str
) -> NativeRouteEvidence:
    """The full PREFLIGHT decision for `table`: config/profile admission, then
    (only when admitted AND a `hash` node is present) a probe that the compiled
    crypto extension loads. A missing or ABI-incompatible extension downgrades
    the WHOLE table to the oracle -- never just the hash column -- matching the
    no-partial-native-output rule; the other admitted columns never touch a
    native kernel either, since the route decision is atomic per table.
    """
    decision = _static_route_decision(config, profile, table=table, engine_version=engine_version)
    if not decision.native_admitted:
        return decision
    if any(n.strategy == "hash" for n in decision.node_routes):
        try:
            load_compiled_crypto_kernel()
        except CryptoExtensionUnavailableError:
            return _downgrade_to_oracle(decision, "crypto_extension_unavailable")
    return decision


def _resolve_truncate_keep(cfg: dict[str, Any]) -> str:
    """Resolve the legacy `from_end` key to `keep` the way `TruncateHandler.run`
    does: an explicit `keep` wins; otherwise `from_end` maps tail/head.

    This is only the from_end->keep RESOLUTION, not the config VALIDATION: an
    invalid `keep` is rejected upstream at admission (Task 2.6's
    `truncate_config_rejection`, which reroutes the table before it reaches here)
    and again by `native_truncate` itself, so a bad value never reaches this
    admitted-only path.
    """
    keep = cfg.get("keep")
    if keep is not None:
        return keep
    return "tail" if bool(cfg.get("from_end", False)) else "head"


def _sample_faker_chunk(
    source: pa.Array | pa.ChunkedArray,
    *,
    pool: ValuePool,
    col_seed: Any,
    mask_key: bytes | None,
) -> pa.Array:
    """Select one chunk's faker values from the already-built `pool`.

    Mirrors `FakerStrategyHandler.run` exactly (Task 3.1 Step 3), scoped to
    the ONE JC-5-admitted variant (`faker_pool_precondition_met` already
    proved `deterministic=True`, `cardinality_mode=reuse`, a namespace, and a
    pool_size before this table reached the native route): `select_seed` is
    always `mask_key` (the DE-02 seam re-keys deterministic selection onto
    the keyed IKM; pool BUILD stays on job_seed, resolved once before the
    chunk loop by `_resolve_faker_pools`).

    Null handling is POSITIONAL, never label-aligned: `chunk_source` is a
    freshly built `pd.Series` with a plain 0..n-1 RangeIndex (an Arrow
    column carries no label index to misalign with in the first place), and
    the output is built as a plain Python list indexed by position, then
    handed to `pa.array` -- the same shape `FakerStrategyHandler.run` uses to
    avoid ever assigning an index-carrying Series onto a differently-indexed
    frame.
    """
    if mask_key is None:  # pragma: no cover - require_mask_key never returns None
        raise AssertionError(
            "faker pool selection reached with mask_key=None; require_mask_key "
            "always resolves a concrete key before the native route dispatches."
        )
    n = len(source)
    chunk_source = pd.Series(source.to_pandas())
    scale = col_seed.scale if col_seed.scale is not None else DEFAULT_POOL_SCALE
    sampled = PoolSampler().sample(
        pool,
        n,
        mode=CardinalityMode(col_seed.cardinality_mode),
        seed=mask_key,
        source=chunk_source,
        namespace=col_seed.namespace,
        deterministic=True,
        scale=scale,
    )
    na_mask = chunk_source.isna().to_numpy()
    values = list(sampled)
    return pa.array([None if na_mask[i] else values[i] for i in range(n)], type=pa.string())


def _mask_chunk_native(
    chunk: pa.Table,
    *,
    col_seed_by_name: dict[str, Any],
    mask_key: bytes | None,
    evidence: NativeRouteEvidence,
    pool_by_column: dict[str, ValuePool],
) -> pa.Table:
    """Mask one chunk column-by-column through the admitted native kernels.

    Every column name in `chunk` is guaranteed present in `col_seed_by_name` by
    the caller's admission precondition (`run_native_or_oracle_chunked` rejects a
    table with any uncovered column at preflight), so a missing lookup here is a
    precondition violation, not a data-shape surprise. `pool_by_column` is
    populated once, before the chunk loop, for every admitted faker column
    (Task 3.1 Step 2); a faker column always has an entry by the same
    precondition.
    """
    arrays: dict[str, pa.Array] = {}
    for name in chunk.schema.names:
        col_seed = col_seed_by_name[name]
        strategy = col_seed.strategy
        cfg = provider_config_to_dict(col_seed.provider_config)
        source = chunk.column(name)
        t0 = time.perf_counter()
        if strategy == "passthrough":
            arrays[name] = native_passthrough(source)
        elif strategy == "redact":
            arrays[name] = native_redact(source, redact_with=cfg.get("redact_with", "REDACTED"))
        elif strategy == "truncate":
            # Admission (`truncate_config_rejection`) already proved `length` is
            # a valid positive int before this table reached the native route;
            # `native_truncate` re-validates it anyway (defense in depth).
            length = cfg.get("length")
            arrays[name] = native_truncate(
                source,
                length=length if isinstance(length, int) else 0,
                keep=_resolve_truncate_keep(cfg),
                mask_char=cfg.get("mask_char"),
            )
        elif strategy == "hash":
            arrays[name] = native_keyed_hash(
                source,
                mask_key=mask_key,
                namespace=col_seed.namespace,
                truncate=cfg.get("truncate"),
            )
            # native_keyed_hash never falls back to the pure-Python reference
            # (see _kernels_keyed.py); a successful call IS the compiled kernel.
            evidence.compiled_kernel_executed = True
        elif strategy == "faker":
            arrays[name] = _sample_faker_chunk(
                source,
                pool=pool_by_column[name],
                col_seed=col_seed,
                mask_key=mask_key,
            )
            evidence.pool_select_executed = True
            evidence.pool_select_calls += 1
        else:  # pragma: no cover - preflight admission already excludes this
            raise AssertionError(
                f"native route admitted column {name!r} with strategy {strategy!r}, "
                "which is outside NATIVE_KERNEL_STRATEGIES and NATIVE_POOL_STRATEGIES; "
                "the preflight admission check should have excluded this table."
            )
        evidence.kernel_calls[strategy] = evidence.kernel_calls.get(strategy, 0) + 1
        evidence.kernel_elapsed_s[strategy] = evidence.kernel_elapsed_s.get(strategy, 0.0) + (
            time.perf_counter() - t0
        )
    return pa.table(arrays)


def _rechain(first: pa.Table, rest: Iterator[pa.Table]) -> Iterator[pa.Table]:
    yield first
    yield from rest


def _resolve_faker_pools(
    col_seed_by_name: dict[str, Any], *, job_seed: bytes, pool_cache: PoolCache
) -> dict[str, ValuePool]:
    """Build/fetch every admitted faker column's pool ONCE, before the chunk
    loop (Task 3.1 Step 2). Keyed by unique `PoolIdentity`, not by column: two
    columns sharing provider + locale + config + namespace share one pool and
    one `pool_cache` entry, matching `FakerStrategyHandler`'s own per-chunk
    cache consult on the oracle side. Uses the SAME
    `resolve_faker_pool_identity` the oracle handler uses (HIGH 1), so the
    two routes can never build different pools for what should be one
    identity.
    """
    registry = get_default_registry()
    builder = PoolBuilder(registry)
    pools_by_column: dict[str, ValuePool] = {}
    for name, col_seed in col_seed_by_name.items():
        if col_seed.strategy != "faker":
            continue
        provider = col_seed.provider
        if provider is None:  # pragma: no cover - admission requires a provider
            raise AssertionError(
                f"native route admitted faker column {name!r} with no provider; "
                "admission should have excluded this."
            )
        cfg = provider_config_to_dict(col_seed.provider_config)
        pool_size, locale, build_config, identity = resolve_faker_pool_identity(
            builder=builder,
            provider=provider,
            plan_pool_size=col_seed.pool_size,
            namespace=col_seed.namespace,
            job_seed=job_seed,
            cfg=cfg,
        )
        cached = pool_cache.get(identity)
        pool = cached if isinstance(cached, ValuePool) else None
        if pool is None:
            pool = builder.build(
                provider=provider,
                size=pool_size,
                job_seed=job_seed,
                locale=locale,
                config=build_config,
                namespace=col_seed.namespace,
            )
            pool_cache.put(pool)
        pools_by_column[name] = pool
    return pools_by_column


def _mask_native(
    config: dict[str, Any],
    chunks: Iterator[pa.Table],
    *,
    table: str,
    engine_version: str,
    key_provider: Any,
    evidence: NativeRouteEvidence,
    pool_cache: PoolCache | None = None,
) -> Iterator[pa.Table]:
    """Eagerly resolve the plan + mask key, then return the lazy per-chunk
    native masking generator (mirrors `run_mask_pipeline_chunked`'s own
    eager-validation-then-lazy-masking contract). Every admitted faker
    column's pool is resolved here too (Task 3.1 Step 2), before any chunk
    is masked, so the pool is built exactly once per invocation."""
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
    decision = plan_native_route(config, profile, table=table, engine_version=engine_version)
    if decision.native_admitted:
        covered = {n.column for n in decision.node_routes}
        actual = set(first.schema.names)
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
                f"{sorted(actual - covered)};missing_configured_columns:{sorted(covered - actual)}",
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
            ftype = first.schema.field(node.column).type
            if not (pa.types.is_string(ftype) or pa.types.is_large_string(ftype)):
                decision = _downgrade_to_oracle(
                    decision, f"faker_source_type_not_string:{node.column}:{ftype}"
                )
                break

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
    )


__all__ = [
    "NativeChunkSchemaDriftError",
    "NativeRouteEvidence",
    "NodeRouteRecord",
    "plan_native_route",
    "run_native_or_oracle_chunked",
]
