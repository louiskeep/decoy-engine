"""Task 4.4 C1/C2/C6 (extended by Task 4.6 slice 1): `ShadowCoordinator` --
the unified batch coordinator, run in SHADOW mode, for the bounded slice
(design doc section 8.3's actions minus publish).

`ShadowCoordinator` has no sink, publisher, or target argument anywhere in
its constructor or `run` -- not a stubbed no-op, an argument that does not
exist. Publication is structurally impossible from this class, not merely
skipped (C1, C7).

Assembly reproduces the pandas oracle's own schema-inference quirks for the
two degenerate shapes this slice's acceptance corpus exercises: a zero-row
column and an all-null (non-empty) column. Both are pinned from a real
`run_pipeline(substrate="pandas")` probe run at build time, not guessed, and
`tests/physical/test_shadow_corpus.py` re-verifies every one of them against
the live oracle -- this module never redefines the oracle's answer, it
reproduces it.

Task 4.6 slice 1 adds the deterministic-faker lifecycle: the compiled index
kernel is loaded at most ONCE per `run()` call, lazily, the first time a
bound faker node is reached, and BEFORE that node's pool is built; every
faker node's pool is resolved ONCE per unique `PoolIdentity` via a
run-scoped map (never per batch, never keyed by node_id -- two nodes sharing
an identity share one build), backed by a FRESH `PoolCache` scoped to this
one `run()` call, never the module-global default cache (whose identity
space omits the registry/backend version, so a stale cross-run hit could
silently pass as a build).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pyarrow as pa

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import (
    IndexDerivationKernel,
    load_compiled_index_kernel,
)
from decoy_engine.execution.physical._plan import ExecutionBinding, PhysicalPlan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_diff_codes import (
    DUPLICATE_NODE_DECLARATION,
    NATIVE_COMPANION_UNAVAILABLE,
    OPERATOR_NOT_EXECUTED,
    PLANNED_VS_ACTUAL_ROUTE_DIFF,
    RESOURCE_LIMIT_BREACH,
    SCHEMA_DIFF,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
from decoy_engine.generation.pool import PoolBuilder, PoolCache, ValuePool
from decoy_engine.generation.pool._identity import PoolIdentity, resolve_faker_pool_identity

if TYPE_CHECKING:
    from decoy_engine.providers_v2 import ProviderRegistry

__all__ = ["ShadowCoordinator", "ShadowRunResult"]

# Strategies whose masked output is a tokenized string regardless of input
# type (redact/truncate/hash/faker); passthrough is the one type-preserving
# strategy and gets its own assembly branch below.
_TOKENIZING_STRATEGIES = frozenset({"redact", "truncate", "hash", "faker"})


@dataclass(frozen=True)
class ShadowRunResult:
    """The staged (never published) result of one shadow run: the masked
    output tables, per-node route evidence keyed by `node_id`, and the
    (empty, for this slice's zero-diagnostic strategies) combined
    diagnostics. `warnings`/`row_errors` exist for shape parity with the
    oracle's `ExecutionResult` so a comparison harness can multiset-compare
    them uniformly even though every slice strategy is zero-diagnostic.
    """

    outputs: dict[str, pa.Table]
    route_evidence: dict[str, OperatorCallEvidence]
    warnings: tuple[object, ...] = ()
    row_errors: tuple[object, ...] = ()


def _batches(table: pa.Table, batch_size_rows: int) -> list[pa.Table]:
    """Order-preserving, row-identity-preserving batching. A zero-row table
    yields exactly ONE zero-row batch (C6): ordinary slicing produces no
    batch at all for an empty table, which would leave every planned
    operator unexecuted and the hash node's compiled-kernel evidence
    vacuous.
    """
    if table.num_rows == 0:
        return [table]
    return [
        table.slice(offset, batch_size_rows) for offset in range(0, table.num_rows, batch_size_rows)
    ]


def _assemble_column(strategy: str, parts: list[pa.Array]) -> pa.Array:
    """Reconcile the concatenated native output onto the oracle's own
    pandas-round-trip schema for this slice's two degenerate shapes (C3): a
    zero-row column and an all-null (non-empty) column. `_batches` always
    returns at least one batch, so `parts` is never empty.

    Reads the type off `combined` itself (the REAL Arrow array the native
    operator produced), not a profile-resolved label: a profile built via a
    pandas read reports a null-bearing integer column as `float64` already
    (pandas' own int+NaN promotion happening one layer up, at profiling
    time), while the resident Arrow array the coordinator actually operates
    on stays `int64` with a validity bitmap -- Arrow has no trouble
    representing that. Using the array's own type is what makes this
    reconciliation track the oracle's real behavior instead of the
    profiler's.
    """
    combined = pa.concat_arrays(parts)
    n = len(combined)
    if strategy in _TOKENIZING_STRATEGIES:
        # redact / truncate / hash emit strings the native kernel produced: an
        # empty column round-trips through the pandas oracle as `float64`, an
        # all-null one as `null`, a normal one stays exactly as produced.
        if n == 0:
            return pa.array([], type=pa.float64())
        return pa.nulls(n, type=pa.null()) if combined.null_count == n else combined
    # passthrough is value-identity, so its OUTPUT SCHEMA is exactly whatever
    # the pandas full-frame oracle infers when the table round-trips
    # `table.to_pandas()` -> `from_pandas(preserve_index=False)` (the oracle's
    # own mechanism, `_pandas_adapter.py`). Reproduce THAT -- a TABLE-level
    # round-trip, not an array-level `array.to_pandas()`: the two can diverge on
    # metadata-carrying dtypes across pandas versions (Codex final-gate:
    # nullable-int handling), while the single-column table round-trip matches
    # the oracle's per-column inference by construction for every admitted type
    # (large_string -> string, all-null bool/string -> null, int+null ->
    # float64, big-int/uint, empty -> pandas' own inference). The output equals
    # the ORACLE's output, not necessarily the source: passthrough itself never
    # masks, but the oracle's float64 promotion of a null-bearing integer loses
    # precision beyond 2**53 (e.g. 2**53+1 -> 2**53), and this reproduces that
    # exactly. So it reconciles the shadow to the oracle, never to the raw
    # source.
    normalized = pa.Table.from_pandas(pa.table({"c": combined}).to_pandas(), preserve_index=False)
    return normalized.column("c").combine_chunks()


@dataclass
class ShadowCoordinator:
    """Runs a C0-extended `PhysicalPlan` over a resident `ShadowSnapshot`.
    Carries no sink/publisher/target dependency at all -- neither the
    constructor nor `run` accepts one.

    `registry` (Task 4.6 slice 1) is the EXACT resolved `ProviderRegistry` a
    bound faker node's pool must build against -- the SAME registry the
    oracle used for the same job, never `get_default_registry()` (a
    caller-overridden registry builds different values under the same
    provider name, so falling back to the default would silently diverge
    from a custom-registry oracle run). Optional, defaulting to `None`, so
    the pre-existing unified-slice production caller (`_unified_slice.py`,
    which never admits a faker node in this slice) constructs
    `ShadowCoordinator(ctx=ctx)` unchanged; a bound faker node asserts the
    registry is present rather than silently masking with the wrong one.
    """

    ctx: ShadowContext
    registry: ProviderRegistry | None = None

    def run(self, plan: PhysicalPlan, snapshot: ShadowSnapshot) -> ShadowRunResult:
        outputs: dict[str, pa.Table] = {}
        route_evidence: dict[str, OperatorCallEvidence] = {}
        # Loaded at most once per run, lazily, the moment the first bound
        # faker node is reached -- and before that node's pool is built.
        index_kernel: IndexDerivationKernel | None = None
        # The authoritative build-once-per-identity store for this run,
        # keyed by `PoolIdentity` (NOT node_id): two nodes sharing an
        # identity share one build, and an A->B->A node order cannot rebuild
        # A. Backed by a fresh, run-scoped `PoolCache` -- never the
        # module-global default (see the module docstring).
        pools_by_identity: dict[PoolIdentity, ValuePool] = {}
        pool_cache = PoolCache()

        for table in plan.tables:
            source = snapshot.tables[table.table]
            columns: dict[str, pa.Array] = {}
            for node in table.nodes:
                binding = node.execution
                if binding is None:
                    # Out of the slice (a different strategy, or a
                    # native-admission miss); nothing to run for this node.
                    continue
                column = node.columns[0]
                if node.node_id in route_evidence:
                    # A duplicate column+strategy declaration (config accepts it)
                    # collides on node_id and would collapse two nodes into one
                    # evidence record + one output column. Surface it, don't hide it.
                    raise ShadowDifference(
                        code=DUPLICATE_NODE_DECLARATION,
                        detail=f"{table.table}: duplicate node_id {node.node_id!r}",
                    )
                evidence = OperatorCallEvidence(planned_operator=binding.operator_id)
                route_evidence[node.node_id] = evidence

                pool: ValuePool | None = None
                if binding.pool_binding is not None:
                    if index_kernel is None:
                        try:
                            index_kernel = load_compiled_index_kernel()
                        except CryptoExtensionUnavailableError as exc:
                            raise ShadowDifference(
                                code=NATIVE_COMPANION_UNAVAILABLE,
                                detail=(
                                    f"node={node.node_id!r}: compiled index companion unavailable"
                                ),
                            ) from exc
                    pool = self._resolve_pool(
                        binding=binding,
                        pools_by_identity=pools_by_identity,
                        pool_cache=pool_cache,
                    )

                parts: list[pa.Array] = []
                for batch in _batches(source, self.ctx.batch_size_rows):
                    if batch.num_rows > self.ctx.batch_size_rows:  # pragma: no cover
                        raise ShadowDifference(
                            code=RESOURCE_LIMIT_BREACH,
                            detail=f"node={node.node_id!r}: a batch exceeded the batch_size_rows budget",
                        )
                    array = batch.column(column)
                    parts.append(
                        run_operator(
                            array,
                            binding=binding,
                            ctx=self.ctx,
                            evidence=evidence,
                            pool=pool,
                            index_kernel=index_kernel,
                        )
                    )

                if not evidence.executed:  # pragma: no cover - run_operator always sets this
                    raise ShadowDifference(
                        code=OPERATOR_NOT_EXECUTED,
                        detail=f"node={node.node_id!r}: the bound operator never ran",
                    )
                if evidence.actual_operator != binding.operator_id:
                    raise ShadowDifference(
                        code=PLANNED_VS_ACTUAL_ROUTE_DIFF,
                        detail=(
                            f"node={node.node_id!r}: planned={binding.operator_id!r} "
                            f"actual={evidence.actual_operator!r}"
                        ),
                    )

                columns[column] = _assemble_column(node.strategy, parts)

            if columns:
                # Assemble in SOURCE-SCHEMA order -- the pandas full-frame
                # oracle preserves the source column order, NOT the node/config
                # declaration order this loop iterates in (Codex final-gate
                # HIGH: source [a,b] with config [b,a] otherwise diverged). For
                # the bounded slice EVERY source column must be configured with
                # an in-slice strategy, so the assembled set must equal the
                # source set exactly; a missing (unconfigured source column) or
                # unexpected column is a coded difference, not a silent drop.
                source_order = source.column_names
                if set(columns) != set(source_order):
                    raise ShadowDifference(
                        code=SCHEMA_DIFF,
                        detail=(
                            f"{table.table}: assembled columns {sorted(columns)} "
                            f"!= source columns {sorted(source_order)}"
                        ),
                    )
                outputs[table.table] = pa.table({name: columns[name] for name in source_order})

        return ShadowRunResult(outputs=outputs, route_evidence=route_evidence)

    def _resolve_pool(
        self,
        *,
        binding: ExecutionBinding,
        pools_by_identity: dict[PoolIdentity, ValuePool],
        pool_cache: PoolCache,
    ) -> ValuePool:
        """Resolve one faker node's pool, ONCE, outside the batch loop:
        consult the run-scoped identity map first (the authoritative
        build-once store -- an LRU-backed cache alone could evict an entry
        between two nodes sharing an identity and force a spurious rebuild),
        then the per-run `PoolCache`, and build via `PoolBuilder` only on a
        genuine miss on both. Uses the SAME `resolve_faker_pool_identity`
        the oracle and the native chunked route use, so all three can never
        compute different identities for one column.
        """
        if binding.pool_binding is None or binding.key_binding is None:
            # pragma: no cover - only ever called for a faker-bound node,
            # which C0 always binds both together for.
            raise AssertionError("_resolve_pool called without a pool_binding/key_binding")
        if self.registry is None:
            raise AssertionError(
                "a faker node is bound but ShadowCoordinator.registry is None; the "
                "shadow caller must thread the resolved ProviderRegistry (inputs.registry) "
                "for any run admitting a faker node."
            )
        if self.ctx.job_seed == b"":
            # Symmetric to the registry guard: a bound faker node with the
            # empty-default job_seed would build a wrong-but-passing pool
            # (job_seed governs pool content). from_key_provider always
            # supplies the real value, so this only fires on a mis-wired caller.
            raise AssertionError(
                "a faker node is bound but ShadowContext.job_seed is empty; the shadow "
                "caller must build the context via from_key_provider so job_seed is set."
            )
        builder = PoolBuilder(self.registry)
        pool_size, locale, build_config, identity = resolve_faker_pool_identity(
            builder=builder,
            provider=binding.pool_binding.provider,
            plan_pool_size=binding.pool_binding.plan_pool_size,
            namespace=binding.key_binding.namespace,
            job_seed=self.ctx.job_seed,
            cfg=dict(binding.resolved_config),
        )
        cached_pool = pools_by_identity.get(identity)
        if cached_pool is not None:
            return cached_pool
        from_secondary_cache = pool_cache.get(identity)
        pool = from_secondary_cache if isinstance(from_secondary_cache, ValuePool) else None
        if pool is None:
            pool = builder.build(
                provider=binding.pool_binding.provider,
                size=pool_size,
                job_seed=self.ctx.job_seed,
                locale=locale,
                config=build_config,
                namespace=binding.key_binding.namespace,
            )
            pool_cache.put(pool)
        pools_by_identity[identity] = pool
        return pool
