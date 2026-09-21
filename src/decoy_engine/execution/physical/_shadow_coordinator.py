"""Task 4.4 C1/C2/C6 (extended by Task 4.6 slices 1 and 3): `ShadowCoordinator`
-- the unified batch coordinator, run in SHADOW mode, for the bounded slice
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

Task 4.6 slice 3 adds an OUT_OF_CORE dispatch branch: when a compiled plan's
mask-table driver set is exactly `{DriverId.OUT_OF_CORE}`, `run()` skips the
per-node loop entirely and dispatches the whole relationship-JOB through the
existing Task 4.2 `OutOfCoreAdapter`, which delegates to `run_fk_out_of_core`
-- the FK machinery stays single-owner there, never reimplemented here. A
driver set mixing OUT_OF_CORE with any other masking driver, or pairing it
with a synthesis stage, is refused rather than dispatched partially.

Task 4.6 slice 5a adds a PURE-GENERATE dispatch branch: when a compiled
plan has no mask tables at all (`plan.tables` empty) and does carry a
synthesis stage, `run()` skips both the per-node loop and the OOC branch and
dispatches through the existing Task 4.2 `SynthesisStageAdapter`, which
delegates to `generate_tables` -- never reimplemented here. The admission
gate + adapter call themselves live in `_shadow_generation.py` (split out to
keep this module under the 600-LOC orchestration cap); `_dispatch_synthesis`
below is a thin wrap of it.

Task 4.6 slice 5b-i adds the INDEPENDENT-MIXED dispatch branch: when a
compiled plan has BOTH mask tables and a synthesis stage, `run()` dispatches
through `_shadow_mixed.dispatch_mixed`, which admits the job via its own
contract (no generate->mask FK edge, no OUT_OF_CORE mask driver, no
validators/quarantine/vault/fidelity), runs generation via the same adapter
path slice 5a uses, reuses THIS class's own per-node loop for the mask half
(by recursing into `run()` with `synthesis` stripped off the plan), and
stitches the two outputs via the shared `execution._stitch` helper. A shape
that dispatch does not admit (a generate->mask FK coupling, an OOC mask
driver, ...) declines coded rather than silently masking only part of the
job -- see `_shadow_mixed.py` for the full contract.

Task 4.6 slice 6 (the LAST masking slice) wraps `FullFrameAdapter` via `_shadow_full_frame.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
import pyarrow as pa

from decoy_engine.execution.native._crypto_ext import CryptoExtensionUnavailableError
from decoy_engine.execution.native._index_ext import (
    IndexDerivationKernel,
    load_compiled_index_kernel,
)
from decoy_engine.execution.physical._context import SeamContext
from decoy_engine.execution.physical._plan import ExecutionBinding, PhysicalPlan, SynthesisStage
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_diff_codes import (
    DUPLICATE_NODE_DECLARATION,
    FAKER_POOL_NON_STRING_OUTPUT,
    MIXED_DRIVER_UNSUPPORTED,
    NATIVE_COMPANION_UNAVAILABLE,
    OOC_DISPATCH_MISSING_DEPENDENCY,
    OPERATOR_NOT_EXECUTED,
    PLANNED_VS_ACTUAL_ROUTE_DIFF,
    RESOURCE_LIMIT_BREACH,
    SCHEMA_DIFF,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_fk import build_fk_dispatch, resolve_admitted_fk_node
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot
from decoy_engine.execution.physical._types import DriverId
from decoy_engine.generation.pool import PoolBuilder, PoolCache, ValuePool
from decoy_engine.generation.pool._identity import PoolIdentity, resolve_faker_pool_identity

if TYPE_CHECKING:
    from decoy_engine.execution._adapter import ExecutionResult
    from decoy_engine.generation.pool._events import QualityWarning
    from decoy_engine.plan._types import Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import RelationshipGraph
    from decoy_engine.relationships._graph import RelationshipEdge

__all__ = ["ShadowCoordinator", "ShadowRunResult"]

# Tokenizing strategies build a fresh column: empty -> float64, all-null -> null,
# else -> string (passthrough is separate). bucket_perturb differs ONLY on empty
# (it passes its source object series through -> Arrow null), so it is split out.
# group_key never emits an all-null column (a null sibling cell derives the key
# for "None"), so its all-null branch is dead, but empty -> float64 applies (the
# empty-frame golden pins this) -- so it belongs here, not in _NULL_ON_EMPTY.
_TOKENIZING_STRATEGIES = frozenset(
    {"redact", "truncate", "hash", "faker", "categorical", "group_key"}
)
_NULL_ON_EMPTY_STRATEGIES = frozenset({"bucket_perturb"})


@dataclass(frozen=True)
class ShadowRunResult:
    """The staged (never published) result of one shadow run: the masked
    output tables, per-node route evidence keyed by `node_id`, and
    diagnostics. `warnings`/`row_errors` mirror `ExecutionResult`'s shape for
    multiset comparison; every scalar/chunked/faker strategy here is
    zero-diagnostic, but slice 5b-ii's FK resolution can populate `warnings`.

    `driver_invocation` (slice 3, widened by slice 6) is the `SeamContext`
    the OUT_OF_CORE or FULL_FRAME branch recorded (`None` elsewhere -- the
    per-node loop has no single adapter call to name); `route_evidence`
    stays empty on both (no per-node loop runs there). `quality_metrics`
    (slice 4, widened by slice 6) carries `ExecutionResult.quality_metrics`
    through by reference for those same branches; the per-node loop below
    produces no metric-bearing strategy in this slice, so it keeps the
    empty default.
    """

    outputs: dict[str, pa.Table]
    route_evidence: dict[str, OperatorCallEvidence]
    warnings: tuple[object, ...] = ()
    row_errors: tuple[object, ...] = ()
    driver_invocation: SeamContext | None = None
    quality_metrics: dict[str, Any] = field(default_factory=dict)


def _pool_values_are_string_valued(values: np.ndarray[Any, Any]) -> bool:
    """True iff every value `values` holds is a string.

    `sample_faker_array` (`native/_chunk_masking.py`) always casts the
    gathered selection to `pa.string()`; a pool built from a custom-registry
    adapter that yields non-string values (an int, a float) would otherwise
    reach that cast and raise an uncoded `ArrowTypeError` instead of a coded
    shadow difference. A native numpy string dtype (`U`/`S`) is always fine;
    an object-dtype array (how `PoolBuilder.build` always stores pool
    values) is fine only when every non-null element is actually a `str` --
    a numeric dtype, or an object array holding a non-str/non-None element,
    fails the check.
    """
    kind = values.dtype.kind
    if kind in ("U", "S"):
        return True
    if kind != "O":
        return False
    return all(v is None or isinstance(v, str) for v in values.tolist())


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
    if strategy in _NULL_ON_EMPTY_STRATEGIES:  # empty + all-null -> null, else string
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


def _privacy_safe_driver_set(drivers: set[DriverId]) -> str:
    """A `MIXED_DRIVER_UNSUPPORTED` detail: driver NAMES only, sorted for a
    stable message -- never a table or column identifier."""
    return ",".join(sorted(d.value for d in drivers))


def _adapt_ooc_result(
    execution_result: ExecutionResult, seam_context: SeamContext
) -> ShadowRunResult:
    """Pure `ExecutionResult` -> `ShadowRunResult` conversion for the
    OUT_OF_CORE dispatch branch (Task 4.6 slice 3). Reshapes nothing: the
    SAME `pa.Table` objects `OutOfCoreAdapter.run` produced pass straight
    through `outputs`, so a value-equal fold in a comparison harness could
    never hide a mutation this step introduced -- there is none to hide.
    `route_evidence` is empty (an OOC table's nodes carry `execution=None` by
    construction; there is no per-node evidence to report). `quality_metrics`
    (Task 4.6 slice 4) forwards by reference, same as `warnings`/`row_errors`
    -- `run_fk_out_of_core`'s code_set corpus-provenance evidence would
    otherwise be silently dropped here, the one real gap this slice closes.
    """
    return ShadowRunResult(
        outputs=dict(execution_result.outputs),
        route_evidence={},
        warnings=execution_result.warnings,
        row_errors=execution_result.row_errors,
        driver_invocation=seam_context,
        quality_metrics=execution_result.quality_metrics,
    )


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

    def run(
        self,
        plan: PhysicalPlan,
        snapshot: ShadowSnapshot,
        *,
        admitted_edges: tuple[RelationshipEdge, ...] = (),
    ) -> ShadowRunResult:
        # Task 4.6 slice 5a: a PURE-GENERATE plan (no mask tables at all)
        # dispatches through the Task 4.2 SynthesisStageAdapter instead of
        # every branch below -- see _dispatch_synthesis. Checked first, and
        # unconditionally on plan.tables being empty, so every existing
        # scalar/chunked/OOC plan (which never has an empty table list AND a
        # synthesis stage at once outside the mixed case handled next) falls
        # straight through, byte-unchanged.
        if not plan.tables and plan.synthesis is not None:
            return self._dispatch_synthesis(plan.synthesis, snapshot)
        if plan.tables and plan.synthesis is not None:
            # Task 4.6 slice 5b-i/5b-ii: a MIXED plan (both present),
            # independent or FK-coupled. `_dispatch_mixed` admits via its own
            # contract, runs generation, reuses THIS loop for the mask half
            # (recursing into `run()` with `synthesis` stripped and an
            # admitted-edge allowlist), and stitches -- see `_shadow_mixed.py`.
            return self._dispatch_mixed(plan, snapshot)

        # Task 4.6 slice 3: an OUT_OF_CORE mask-table plan dispatches through
        # the Task 4.2 `OutOfCoreAdapter` (which owns the FK machinery via
        # `run_fk_out_of_core`) rather than running this loop -- the driver
        # set is computed ONCE, up front, so every existing scalar/chunked
        # plan (whose driver set never contains OUT_OF_CORE) falls straight
        # through to the loop below, byte-unchanged.
        mask_drivers = {table.driver for table in plan.tables}
        if mask_drivers == {DriverId.OUT_OF_CORE}:
            return self._dispatch_out_of_core(plan, snapshot)
        if mask_drivers == {DriverId.FULL_FRAME}:
            from decoy_engine.execution.physical import _shadow_full_frame

            result = _shadow_full_frame.dispatch_full_frame_if_applicable(self, plan, snapshot)
            if result is not None:
                return result
        if DriverId.OUT_OF_CORE in mask_drivers:
            # OUT_OF_CORE mixed with any other masking driver: never mask
            # part of a plan through the adapter and the rest scalar.
            raise ShadowDifference(
                code=MIXED_DRIVER_UNSUPPORTED, detail=_privacy_safe_driver_set(mask_drivers)
            )

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
        # Slice 5b-ii: an FK-child column resolves against its parent ahead of native binding.
        fk = build_fk_dispatch(snapshot, self.ctx.relationship_graph, admitted_edges)
        warnings: list[QualityWarning] = []

        for table in plan.tables:
            source = snapshot.tables[table.table]
            columns: dict[str, pa.Array] = {}
            for node in table.nodes:
                fk_resolution = resolve_admitted_fk_node(node, table.table, source, columns, fk)
                if fk_resolution is not None:
                    columns[node.columns[0]] = fk_resolution.column
                    warnings.extend(fk_resolution.warnings)
                    continue
                binding = node.execution
                if binding is None:
                    # Out of the slice (a different strategy, or a native-admission miss).
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
                if binding.needs_index_kernel and index_kernel is None:
                    try:
                        index_kernel = load_compiled_index_kernel()
                    except CryptoExtensionUnavailableError as exc:
                        raise ShadowDifference(
                            code=NATIVE_COMPANION_UNAVAILABLE,
                            detail=(f"node={node.node_id!r}: compiled index companion unavailable"),
                        ) from exc
                if binding.pool_binding is not None:
                    pool = self._resolve_pool(
                        binding=binding,
                        pools_by_identity=pools_by_identity,
                        pool_cache=pool_cache,
                    )

                # group_key is the one bound strategy that keys on a DIFFERENT
                # column than the one it writes: it reads the sibling `group_by`
                # column's ORIGINAL source value (admission proved that sibling is
                # unmasked/passthrough, so `batch.column(group_by)` equals what the
                # oracle reads) and writes the derived key to `column`. Every other
                # operator reads and writes the same column.
                input_column = binding.group_key_group_by or column
                parts: list[pa.Array] = []
                for batch in _batches(source, self.ctx.batch_size_rows):
                    if batch.num_rows > self.ctx.batch_size_rows:  # pragma: no cover
                        raise ShadowDifference(
                            code=RESOURCE_LIMIT_BREACH,
                            detail=f"node={node.node_id!r}: a batch exceeded the batch_size_rows budget",
                        )
                    array = batch.column(input_column)
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

        return ShadowRunResult(
            outputs=outputs, route_evidence=route_evidence, warnings=tuple(warnings)
        )

    def _dispatch_synthesis(
        self, physical_synthesis: SynthesisStage, snapshot: ShadowSnapshot
    ) -> ShadowRunResult:
        """Thin wrap of `_shadow_generation.dispatch_synthesis` (moved out to
        keep this module under the 600-LOC orchestration cap; see that
        module's docstring for the admission gate + dispatch contract) into
        this class's own `ShadowRunResult` shape. The SAME table objects
        `SynthesisStageAdapter.run` produced pass straight through
        `outputs`; `route_evidence`/`warnings`/`row_errors`/`quality_metrics`
        stay at their empty defaults -- this slice's admitted shape
        (sequence/categorical, no validators/quarantine) produces none of
        any of them on either side.
        """
        from decoy_engine.execution.physical._shadow_generation import dispatch_synthesis

        outputs, seam_context = dispatch_synthesis(self.ctx, physical_synthesis, snapshot)
        return ShadowRunResult(
            outputs=dict(outputs), route_evidence={}, driver_invocation=seam_context
        )

    def _dispatch_mixed(self, plan: PhysicalPlan, snapshot: ShadowSnapshot) -> ShadowRunResult:
        """Thin wrap of `_shadow_mixed.dispatch_mixed` (moved out to keep
        this module under the 600-LOC orchestration cap; see that module's
        docstring for the admission gate + generate/mask/stitch sequence).
        Passes `self` (not just `self.ctx`) since the mask half needs to
        recurse into this class's own `run()` for its per-node loop.
        """
        from decoy_engine.execution.physical._shadow_mixed import dispatch_mixed

        return dispatch_mixed(self, plan, snapshot)

    def _dispatch_out_of_core(
        self, plan: PhysicalPlan, snapshot: ShadowSnapshot
    ) -> ShadowRunResult:
        """Dispatch a `{OUT_OF_CORE}` plan through the Task 4.2
        `OutOfCoreAdapter`, which delegates to `run_fk_out_of_core` -- the FK
        machinery stays single-owner there, never re-implemented here. The
        adapter is constructed locally (never on `self`), matching every
        other driver adapter's per-call lifecycle.
        """
        # Imported here, not at module scope: this is the ONE place inside
        # `execution.physical` that reaches into the `drivers/` subpackage,
        # kept lazy so a caller that never takes the OOC branch never pays
        # for (or risks) importing DuckDB-backed machinery.
        from decoy_engine.execution.physical.drivers._out_of_core import OutOfCoreAdapter

        seed_plan, graph, registry = self._require_ooc_deps()
        adapter = OutOfCoreAdapter()
        result = adapter.run(
            seed_plan,
            {t: snapshot.tables[t] for t in snapshot.tables},
            registry=registry,
            relationship_graph=graph,
            sink=None,  # resident compare only, never publication (C1/C7)
            batch_rows=self.ctx.batch_size_rows,  # the coordinator's own budget
            key_provider=self.ctx.key_provider,  # legitimately optional (unkeyed job)
        )
        seam_context = adapter.last_invocation
        if seam_context is None:  # pragma: no cover - last_invocation is always set first
            # `last_invocation` is set unconditionally, before delegation
            # (`_out_of_core.py`), so a successful `.run()` return always
            # leaves it non-None; this is a type-narrowing guard against a
            # future adapter change, not a reachable branch today.
            raise AssertionError("OutOfCoreAdapter.run returned without setting last_invocation")
        return _adapt_ooc_result(result, seam_context)

    def _require_ooc_deps(self) -> tuple[Plan, RelationshipGraph, ProviderRegistry]:
        """Type-narrow the runtime OOC carriers, raising a coded difference
        naming whichever is absent -- never a bare `None` reaching the
        adapter/delegate uncoded. `ctx.plan` / `ctx.relationship_graph` /
        `self.registry` are declared `| None` for back-compat with every
        scalar/chunked/faker caller, which never sets or reads them; an
        OUT_OF_CORE dispatch genuinely requires all three.
        """
        if self.ctx.plan is None:
            raise ShadowDifference(
                code=OOC_DISPATCH_MISSING_DEPENDENCY, detail="ShadowContext.plan is None"
            )
        if self.ctx.relationship_graph is None:
            raise ShadowDifference(
                code=OOC_DISPATCH_MISSING_DEPENDENCY,
                detail="ShadowContext.relationship_graph is None",
            )
        if self.registry is None:
            raise ShadowDifference(
                code=OOC_DISPATCH_MISSING_DEPENDENCY, detail="ShadowCoordinator.registry is None"
            )
        return self.ctx.plan, self.ctx.relationship_graph, self.registry

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
        pool: ValuePool
        cached_pool = pools_by_identity.get(identity)
        if cached_pool is not None:
            pool = cached_pool
        else:
            from_secondary_cache = pool_cache.get(identity)
            built = from_secondary_cache if isinstance(from_secondary_cache, ValuePool) else None
            if built is None:
                built = builder.build(
                    provider=binding.pool_binding.provider,
                    size=pool_size,
                    job_seed=self.ctx.job_seed,
                    locale=locale,
                    config=build_config,
                    namespace=binding.key_binding.namespace,
                )
                pool_cache.put(built)
            pool = built
            pools_by_identity[identity] = pool

        # Admission (`_faker_pool_bindable`) proves only the provider NAME is
        # allowlisted and poolable; it never inspects what the bound adapter
        # actually produces. A custom-registry override can rebind that name
        # to a poolable adapter yielding non-string values, so the pool's
        # real value type is checked here, at the one point it is known,
        # rather than trusting admission's weaker guarantee.
        if not _pool_values_are_string_valued(pool.values):
            raise ShadowDifference(
                code=FAKER_POOL_NON_STRING_OUTPUT,
                detail=(
                    f"provider={binding.pool_binding.provider!r}: pool values are not "
                    "all string-valued, which the shadow faker operator requires"
                ),
            )
        return pool
