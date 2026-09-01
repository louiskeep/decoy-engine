"""ExecutionAdapter protocol + ExecutionResult + strategy-handler contract (S9).

The boundary between planning and execution (S9 spec §2). Concrete adapter at
S9 close: PandasExecutionAdapter. The boundary is Arrow-shaped (`pa.Table` in,
`pa.Table` out); what a strategy does internally (pandas Series ops today,
Polars in S12) is invisible to the boundary.

Refinement vs the spec's StrategyHandler signature: the rarely-used run() deps
(registry, pool_cache, relationship_graph, namespace_registry, job_seed) are
bundled into a frozen `StrategyContext` rather than passed as five separate
kwargs, so a no-backend strategy (passthrough/redact/truncate) does not carry
five unused parameters. Scalar handlers receive one `column: str`; the composite
handler (later slice) writes multiple columns.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import pandas as pd
import pyarrow as pa

from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._row_errors import RowError, RowErrorRecord
from decoy_engine.generation.pool._events import QualityWarning
from decoy_engine.instrumentation.timing import StrategyTimingRecord

if TYPE_CHECKING:
    from decoy_engine.execution._output_projection import UnconfiguredColumnPolicy
    from decoy_engine.generation.pool._cache import PoolCache
    from decoy_engine.keyprovider import KeyProvider
    from decoy_engine.plan._types import ColumnSeed, Plan
    from decoy_engine.providers_v2 import ProviderRegistry
    from decoy_engine.relationships import NamespaceRegistry, RelationshipGraph


@dataclass(frozen=True)
class ExecutionResult:
    """The output of `ExecutionAdapter.run(...)` (S9 spec §2).

    `outputs` maps table name -> masked or generated `pa.Table`. A
    multi-table job (FK parent + child masked in one run) carries one
    entry per table; a single-table job carries one. `output` is a
    convenience accessor for the single-table case (it raises rather
    than guess when the result is multi-table; per the slice-2h
    contract widening, PQ-S9-C).

    FC-1 (2026-06-02) adds `table_kinds`: a per-table `"mask"` or
    `"generate"` classification populated by `run_pipeline` so the
    downstream manifest writer can stamp `kind` per node-run. PO D1
    sub-decision 2026-06-01 (resolved per-table). Empty dict on the
    pre-FC-1 single-kind adapter call paths (`PandasExecutionAdapter.run`
    + `generate_tables`); `run_pipeline` populates it.
    """

    outputs: dict[str, pa.Table]
    timings: tuple[StrategyTimingRecord, ...] = ()
    boundary_conversion_ms: float = 0.0
    warnings: tuple[QualityWarning, ...] = ()
    quality_metrics: dict[str, Any] = field(default_factory=dict)
    table_kinds: dict[str, str] = field(default_factory=dict)
    # Sprint 2 honesty pack (D7): per-row failures recorded by strategy
    # handlers (bucketize/date_shift format_error, code_set mask_error),
    # table-attributed by the adapter's drain point. Additive; default empty
    # tuple leaves every existing ExecutionResult construction unchanged.
    row_errors: tuple[RowErrorRecord, ...] = ()

    @property
    def output(self) -> pa.Table:
        """The single masked table. Raises if the result holds 0 or >1 tables."""
        if len(self.outputs) != 1:
            raise ExecutionError(
                code="multi_table_result_has_no_single_output",
                message=(
                    f"ExecutionResult holds {len(self.outputs)} tables "
                    f"({sorted(self.outputs)}); use .outputs[table] for a multi-table job."
                ),
            )
        return next(iter(self.outputs.values()))


@dataclass(frozen=True)
class StrategyContext:
    """Shared per-job dependencies threaded into every strategy handler.

    `job_seed` (8 bytes) is the GENERATION seed: reproducibility for fresh
    synthetic data (faker/categorical pool builds, non-deterministic composites).
    `mask_key` is the KEYED-mask IKM (DE-02): every re-identification-protecting
    derivation (`derive` / `derive_index` / `PoolSampler.sample` in deterministic
    mode / the raw `text_mask` HMAC) draws from it, NOT from `job_seed`. When no
    secret is present `mask_key == job_seed` (8 bytes) so output is byte-identical
    to pre-DE-02; with a KeyProvider secret it is the 32-byte HKDF mask root.
    `mask_key` defaults to `job_seed` when left unset (see `__post_init__`), so a
    handler that reads `ctx.mask_key` is correct even for callers that predate the
    field.
    """

    registry: ProviderRegistry
    pool_cache: PoolCache
    relationship_graph: RelationshipGraph
    namespace_registry: NamespaceRegistry
    job_seed: bytes
    # DE-02 (2026-07-14): the keyed-mask IKM. Empty sentinel means "not supplied"
    # -> falls back to job_seed in __post_init__, keeping every pre-DE-02
    # construction (including the test fixtures that never pass it) byte-identical.
    mask_key: bytes = b""
    # Sprint 2 honesty pack (D7): the shared per-row-error sink. The
    # dataclass is frozen, but a mutable field's CONTENTS may still be
    # mutated: handlers append RowError instances here; nothing reassigns
    # the field (trap T6 -- `ctx.row_errors = [...]` would raise
    # FrozenInstanceError and, more importantly, would break the drain
    # point's identity-based reference to this list).
    row_errors: list[RowError] = field(default_factory=list)
    # HC-1 slice 1: the shared code_set corpus-provenance evidence sink,
    # keyed by (table, column) (dedupes if a column's handler runs more than
    # once, e.g. under a when_gate). Table-qualified because two tables can
    # legally declare a same-named code_set column bound to DIFFERENT corpora
    # (e.g. both tables have a "code" column, one icd10 one mcc); a bare-column
    # key would let the second table's stamp silently overwrite the first's,
    # dropping audit provenance for whichever table lost the race (Codex P2
    # MULTI-TABLE EVIDENCE COLLISION). `CodeSetHandler.run` populates one entry
    # per (table, column) (counts + identifiers only, no raw codes), including
    # the `table`/`column` identity in the evidence dict itself since the
    # flattened metrics list below discards the key; `PandasExecutionAdapter.run`
    # / `run_sequential` copy it into
    # `ExecutionResult.quality_metrics['code_set_corpora']` at the end of the
    # job. Same mutate-in-place pattern as `row_errors` above.
    code_set_corpora: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # Codex P2 MULTI-TABLE EVIDENCE COLLISION remediation: the table the
    # in-flight scalar handler dispatch belongs to. Mutated via
    # `object.__setattr__` (frozen dataclass, same escape hatch as `mask_key`'s
    # `__post_init__` normalization below) by the nearest enclosing point that
    # iterates tables and owns `code_set_corpora`
    # (`_pandas_adapter._dispatch_mask_node`, and the orphan-REMAP closure for
    # the narrower parent-key-strategy case), immediately before invoking a
    # handler, so `CodeSetHandler.run` can key its evidence sink by
    # (table, column) instead of a bare column name.
    current_table: str = ""
    # Codex round-4 P2 NESTED CODE_SET MIS-KEYED EVIDENCE remediation: the
    # OUTER column identity when a scalar handler is running as a nested
    # strategy's child. `NestedStrategyHandler.run` invokes the child handler
    # with the synthetic column name `_nested_leaves` (a batch-collection
    # column that does not exist in the source frame), so a child handler
    # that stamps column-keyed evidence (CodeSetHandler) must not key it off
    # the `column` parameter it was literally called with -- that would
    # record a nonexistent `_nested_leaves` column, and two nested code_set
    # columns in the same table would collide on that same synthetic name,
    # silently dropping one corpus's provenance. Empty string means "not
    # running as a nested child"; `CodeSetHandler.run` falls back to its own
    # `column` parameter in that case. Set/restored via `object.__setattr__`
    # around the child dispatch (same escape hatch as `current_table`),
    # mirroring `_orphan.py`'s prior_table save/restore since nested
    # dispatch can itself run mid-dispatch of an enclosing node.
    nested_outer_column: str = ""
    # Codex round-7 P2 CROSS-INVOCATION MASKING/EVIDENCE DIVERGENCE
    # remediation: the job-wide pinned corpus record per (table, column). The
    # round-6 fix pinned one corpus record within a SINGLE `CodeSetHandler.run`
    # so masking and evidence could not diverge on a mid-run file replacement.
    # But a code_set on an FK PARENT under `orphan_policy=REMAP` dispatches
    # `run` TWICE -- once for the parent column, and once from the orphan-REMAP
    # closure (`_orphan.make_remap_fn`) which re-runs the parent's strategy to
    # mask orphan keys -- and each call independently re-resolved from the
    # loader cache. A customer corpus file replaced between those two calls
    # then masked real parent values off v1 and remapped orphans off v2, and
    # the second call's evidence stamp overwrote the first's with v2. Pinning
    # the resolved record here (job scope == this StrategyContext's lifetime),
    # keyed by the same (table, column) identity the evidence sink uses, makes
    # every `run` invocation for one logical column in one job share ONE corpus
    # version. Value typed loosely (`Any`) so the execution boundary does not
    # import the transforms-layer `_CorpusRecord`. Same mutate-in-place pattern
    # as `code_set_corpora` above; a fresh dict per job via default_factory.
    code_set_records: dict[tuple[str, str], Any] = field(default_factory=dict)
    # HC-3a (Codex R1 P1 #1): pre-mask entity-anchor snapshots for
    # `date_shift` columns configured with `group_by`, keyed by (table,
    # group_by_column). Each route (pandas full-frame/chunked, sequential,
    # polars-native) copies the group column's values BEFORE any node masks,
    # so `DateShiftStrategyHandler` derives every row's offset from the
    # entity's ORIGINAL id -- never a value some earlier (possibly when-gated)
    # node already masked in place, which would split one patient's rows onto
    # different offsets and break the interval the feature exists to preserve.
    # The handler aligns a snapshot to the (possibly when-gated subset) frame
    # it sees by index label, and FAILS CLOSED if a configured group_by has no
    # snapshot rather than falling back to the live (mutable) frame. Same
    # mutate-in-place / default-empty pattern as the sinks above; a fresh dict
    # per job via default_factory.
    group_anchor_snapshots: dict[tuple[str, str], pd.Series] = field(default_factory=dict)
    # Phase 4 slice 1 (DGRN -> windowed_date): the durable global row offset of
    # this call's FIRST row, i.e. the position the physical row 0 of `df`
    # occupies in the full, unchunked source. Full-frame and every other
    # existing caller default to 0 (their row 0 IS the durable row 0), so this
    # field is byte-unchanged for every pre-existing construction. Only
    # `windowed_date` reads it (`ctx.row_offset` -> `apply_windowed_date`'s
    # `row_offset` param); every other handler ignores it, matching the
    # value-keyed (not position-keyed) contract the other CHUNK_SAFE
    # strategies rely on.
    row_offset: int = 0

    def __post_init__(self) -> None:
        # A frozen dataclass forbids attribute assignment; object.__setattr__ is
        # the sanctioned escape hatch for post-init normalization. `mask_key`
        # defaults to the job_seed so no-secret runs feed the identical IKM.
        if not self.mask_key:
            object.__setattr__(self, "mask_key", self.job_seed)

    def code_set_corpora_metrics(self) -> dict[str, Any]:
        """HC-1 slice 1: the code_set corpus-provenance evidence block, or {}.

        Both the full-frame (`PandasExecutionAdapter.run`) and the
        table-at-a-time (`run_sequential`) paths merge this into the job's
        `ExecutionResult.quality_metrics`. Empty dict when no code_set column
        ran, so an unrelated job's quality_metrics is untouched.
        """
        if not self.code_set_corpora:
            return {}
        return {"code_set_corpora": list(self.code_set_corpora.values())}


class StrategyHandler(Protocol):
    """A single scalar masking strategy, invoked through the boundary.

    `preflight` is an OPTIONAL extra method, not part of this Protocol's
    formal shape (duck-typed via `getattr(handler, "preflight", None)` in
    `execution._when_gate.run_with_when_gate`, Codex P2 FAIL-CLOSED
    VALIDATION BYPASSED BY A ZERO-MATCH `when` GATE remediation). A handler
    whose `run()` does fail-closed validation that must happen even when a
    `when:` gate matches zero rows (e.g. `CodeSetHandler` loading/validating
    its corpus) defines `preflight(plan, ctx) -> None`; the when-gate calls
    it unconditionally before its zero-match short-circuit. Handlers with no
    such need simply do not define it.
    """

    name: str

    def run(
        self,
        df: pd.DataFrame,
        column: str,
        plan: ColumnSeed,
        ctx: StrategyContext,
    ) -> tuple[pd.DataFrame, list[QualityWarning]]:
        """Mutate `df[column]` per the plan; return (df, warnings)."""
        ...


@runtime_checkable
class ExecutionAdapter(Protocol):
    """The planning/execution boundary (S9 spec §2). Narrow by design.

    `runtime_checkable` so a second concrete adapter (S11's polars adapter) can
    assert conformance via `isinstance`; this is name-presence only, the real
    signature conformance is the mypy gate.
    """

    adapter_name: str
    adapter_version: str

    def run(
        self,
        plan: Plan,
        sources: Mapping[str, pa.Table],
        *,
        registry: ProviderRegistry,
        pool_cache: PoolCache | None = None,
        relationship_graph: RelationshipGraph,
        namespace_registry: NamespaceRegistry,
        # DE-03 fail-closed output projection. `unconfigured_column_policy` None
        # resolves to the release-phase default; `generate_output_tables` names
        # the generate-echo tables exempt from the mask plan's declared surface.
        unconfigured_column_policy: UnconfiguredColumnPolicy | None = None,
        generate_output_tables: frozenset[str] = frozenset(),
        # DE-02: the keyed-mask secret source, injected at run time and NEVER
        # serialized into the plan. None -> the no-secret job_seed fallback.
        key_provider: KeyProvider | None = None,
        # Phase 4 slice 1: forwarded to StrategyContext.row_offset (see its
        # docstring). Default 0 keeps every pre-existing caller byte-unchanged.
        row_offset: int = 0,
        # Phase 4 slice 4: a caller-resolved (table, column) -> corpus-record
        # mapping seeded into StrategyContext.code_set_records, so every chunk
        # of a multi-call job masks against the identical pinned corpus (see
        # `_chunked_code_set.py`). None (default) leaves the field empty,
        # unchanged for every pre-existing caller.
        code_set_records: Mapping[tuple[str, str], object] | None = None,
    ) -> ExecutionResult: ...

    def supports_strategy(self, strategy_name: str) -> bool: ...

    def shutdown(self) -> None: ...


def provider_config_to_dict(provider_config: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    """Flatten a `ColumnSeed.provider_config` tuple-of-pairs into a dict."""
    return dict(provider_config)


def pandas_column_to_kernel_input(column: pd.Series) -> pa.Array | list[Any]:
    """Convert one pandas column to the masking kernel's expected input.

    The fast path is a whole-column Arrow conversion. A pandas object column
    can legitimately mix Python scalar types (e.g. str and int identifiers in
    one column); Arrow has no single type for that, so
    `pa.array(column, from_pandas=True)` raises. Falling back to a plain
    Python list of scalars (None-normalized for NaN/NaT/None) lets the
    kernel's per-value dispatch (`decoy_engine.kernel._scalar._array_to_pylist`)
    mask every value anyway, matching the pre-kernel per-value handlers this
    replaced (SC1 port) instead of raising on a column shape that used to
    mask successfully.
    """
    try:
        return pa.array(column, from_pandas=True)
    except pa.ArrowException:
        na_mask = column.isna().to_numpy()
        return [None if is_na else value for is_na, value in zip(na_mask, column, strict=True)]
