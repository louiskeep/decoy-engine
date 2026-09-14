"""Task 4.4 C1/C2/C6: `ShadowCoordinator` -- the unified batch coordinator,
run in SHADOW mode, for the bounded slice (design doc section 8.3's actions
minus publish).

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
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow as pa

from decoy_engine.execution.physical._plan import PhysicalPlan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_diff_codes import (
    DUPLICATE_NODE_DECLARATION,
    OPERATOR_NOT_EXECUTED,
    PLANNED_VS_ACTUAL_ROUTE_DIFF,
    RESOURCE_LIMIT_BREACH,
    SCHEMA_DIFF,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence, run_operator
from decoy_engine.execution.physical._shadow_snapshot import ShadowSnapshot

__all__ = ["ShadowCoordinator", "ShadowRunResult"]

# Strategies whose masked output is a tokenized string regardless of input
# type (redact/truncate/hash); passthrough is the one type-preserving
# strategy and gets its own assembly branch below.
_TOKENIZING_STRATEGIES = frozenset({"redact", "truncate", "hash"})


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
    # `table.to_pandas()` -> `from_pandas`. Reproduce that per column with the
    # same round-trip (`pa.array(array.to_pandas())`) so EVERY admitted type
    # matches by construction -- large_string -> string, all-null bool/string
    # -> null, int+null -> float64, empty -> pandas' own inference -- instead
    # of hand-listing individual quirks, which kept missing shapes (Codex
    # final-gate: all-null bool, non-empty large_string). Verified equal to the
    # live oracle for every admitted passthrough type; values are unchanged, so
    # this reconciles only the schema, never the data.
    return pa.array(combined.to_pandas())


@dataclass
class ShadowCoordinator:
    """Runs a C0-extended `PhysicalPlan` over a resident `ShadowSnapshot`.
    Carries no sink/publisher/target dependency at all -- neither the
    constructor nor `run` accepts one.
    """

    ctx: ShadowContext

    def run(self, plan: PhysicalPlan, snapshot: ShadowSnapshot) -> ShadowRunResult:
        outputs: dict[str, pa.Table] = {}
        route_evidence: dict[str, OperatorCallEvidence] = {}
        for table in plan.tables:
            source = snapshot.tables[table.table]
            columns: dict[str, pa.Array] = {}
            for node in table.nodes:
                binding = node.execution
                if binding is None:
                    # Out of the 4.4 slice (a different strategy, or a
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

                parts: list[pa.Array] = []
                for batch in _batches(source, self.ctx.batch_size_rows):
                    if batch.num_rows > self.ctx.batch_size_rows:  # pragma: no cover
                        raise ShadowDifference(
                            code=RESOURCE_LIMIT_BREACH,
                            detail=f"node={node.node_id!r}: a batch exceeded the batch_size_rows budget",
                        )
                    array = batch.column(column)
                    parts.append(
                        run_operator(array, binding=binding, ctx=self.ctx, evidence=evidence)
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
