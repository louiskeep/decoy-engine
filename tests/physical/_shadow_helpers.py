"""Shared test-only harness for the Task 4.4 shadow-vs-oracle comparison
(C3): builds the C0-extended `PhysicalPlan` the same way `_helpers.py` builds
a Task 4.2/4.3 job, runs the `ShadowCoordinator` over the resident C5
snapshot, runs the pinned pandas oracle over the SAME resident source
object, and asserts the two are identical -- coded per
`_shadow_diff_codes.py`, never a bare `AssertionError`.
"""

from __future__ import annotations

import math
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import _pipeline_finalize, run_pipeline
from decoy_engine.execution._adapter import ExecutionResult
from decoy_engine.execution._planner import AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._plan import PhysicalPlan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_coordinator import ShadowCoordinator, ShadowRunResult
from decoy_engine.execution.physical._shadow_diff_codes import (
    CELL_VALUE_DIFF,
    DIAGNOSTICS_DIFF,
    NULL_MASK_DIFF,
    OOC_FK_PARITY_DIFF,
    ROW_COUNT_DIFF,
    ROW_ORDER_DIFF,
    SCHEMA_DIFF,
    ShadowDifference,
)
from decoy_engine.execution.physical._shadow_snapshot import capture_shadow_snapshot
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.keyprovider import KeyProvider
from decoy_engine.providers_v2 import ProviderRegistry

ENGINE_VERSION = "shadow-coordinator-4.4"


def write_read_only_fixture(tmp_path: Path, table: pa.Table, name: str) -> Path:
    """Write `table` once, then chmod it read-only (C5's "the harness ALSO
    writes the fixture once, then makes it read-only for the duration, so an
    accidental in-test rewrite is impossible")."""
    path = tmp_path / f"{name}.parquet"
    pq.write_table(table, path)
    os.chmod(path, 0o444)
    return path


def build_config(
    tmp_path: Path,
    table_name: str,
    source_path: Path,
    columns: list[dict[str, Any]],
    *,
    seed: int = 20260914,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "version": 1,
        "global_settings": {"seed": seed},
        "sources": {table_name: {"type": "file", "format": "parquet", "path": str(source_path)}},
        "targets": {
            table_name: {
                "type": "file",
                "format": "parquet",
                "path": str(tmp_path / f"{table_name}.out.parquet"),
            }
        },
        "tables": [{"name": table_name, "columns": columns}],
    }
    return PipelineConfig.model_validate(raw).model_dump()


@dataclass(frozen=True)
class ShadowRun:
    plan: PhysicalPlan
    shadow: ShadowRunResult
    oracle: ExecutionResult
    # A legacy single-source run sets `shadow_identity` and leaves
    # `shadow_identities` empty; a multi-source run (Task 4.6 slice 3) sets
    # `shadow_identity=None` and populates `shadow_identities` with EVERY
    # table's identity -- the old field is never silently derived from one
    # table out of several.
    shadow_identity: str | None = None
    shadow_identities: dict[str, str] = field(default_factory=dict)


def run_shadow_and_oracle(
    config: dict[str, Any],
    table_name: str | None = None,
    source: pa.Table | None = None,
    *,
    sources: Mapping[str, pa.Table] | None = None,
    key_provider: KeyProvider | None = None,
    batch_size_rows: int = 50_000,
    registry: ProviderRegistry | None = None,
    auto_chunk: bool = False,
    auto_chunk_threshold_rows: int | None = None,
    chunk_size_rows: int | None = None,
    out_of_core_threshold_rows: int | None = None,
    use_byte_estimate_routing: bool = True,
) -> ShadowRun:
    """Run the shadow coordinator and the pinned oracle over the SAME
    resident source object(s) (C5's same-input proof: both sides are handed
    the identical `pa.Table` instance(s)).

    Two mutually exclusive calling modes (Task 4.6 slice 3 widens this
    additively; the original single-source shape is unchanged for existing
    callers): the legacy positional PAIR `(table_name, source)`, both
    present, for a one-table job; or `sources=` alone, a `{table: pa.Table}`
    mapping, for a multi-table job (an FK parent+child set). Passing both
    modes, neither, or one half of the legacy pair without the other all
    raise `ValueError` -- there is no silent "pick one" fallback.

    `registry` (Task 4.6 slice 1), when given, is threaded to BOTH sides --
    `capture_physical_plan_inputs` (so the compiled plan's faker admission
    and `ShadowCoordinator.registry` see it) and the oracle `run_pipeline`
    call -- so a faker parity case can prove agreement under a NON-default
    registry, not only the module singleton. `None` (the default) leaves
    both sides resolving their own default registry, which is the same
    singleton object either way.

    `auto_chunk` / `auto_chunk_threshold_rows` / `chunk_size_rows` (Task 4.6
    slice 2) drive the CHUNKED disposition. ONE `chunk_size_rows` value feeds
    all three places that must cross IDENTICAL chunk boundaries for the
    parity claim to mean anything: `capture_physical_plan_inputs` (so the
    compiler stamps `DriverId.CHUNKED`), `ShadowContext.batch_size_rows` (so
    the coordinator's own resource-bounded batching lines up with the
    oracle's chunk width instead of the unrelated `batch_size_rows` knob),
    and the oracle's own `run_pipeline(auto_chunk=...)` call. `auto_chunk=
    False` (the default) keeps every existing caller full_frame and
    `batch_size_rows` in its old, chunking-unrelated role (the batch-size x
    row-order matrix). One nuance: this now passes `auto_chunk=False` to the
    capture layer, whose own default was True. That is inert for every current
    fixture (all sub-threshold, so `classify_job` returns full_frame either
    way), but a hypothetical at-or-above-threshold, chunk-stable fixture that
    omitted `auto_chunk` would have compiled CHUNKED under the old default and
    now compiles FULL_FRAME.

    `out_of_core_threshold_rows` / `use_byte_estimate_routing` (Task 4.6
    slice 3) drive the OUT_OF_CORE disposition, forwarded only to
    `capture_physical_plan_inputs` -- never to the oracle's `run_pipeline`
    call, which always passes `execution_mode="full_frame"` and so returns
    before OOC selection runs regardless of these knobs (the oracle stays
    the pandas full-frame path no matter how the shadow side routes).
    `out_of_core_threshold_rows=None` (the default) leaves
    `capture_physical_plan_inputs`'s own default in effect.
    """
    legacy_pair_given = table_name is not None or source is not None
    if legacy_pair_given and (table_name is None or source is None):
        raise ValueError(
            "table_name and source must both be given together (the legacy pair), or both omitted"
        )
    if legacy_pair_given and sources is not None:
        raise ValueError("pass either the legacy (table_name, source) pair or sources=, not both")
    if not legacy_pair_given and sources is None:
        raise ValueError("must pass either the legacy (table_name, source) pair or sources=")

    resolved_sources: dict[str, pa.Table] = (
        dict(sources) if sources is not None else {table_name: source}  # type: ignore[dict-item]
    )

    if auto_chunk and chunk_size_rows is None:
        raise ValueError("auto_chunk=True requires an explicit chunk_size_rows")

    resolved_chunk_size = (
        chunk_size_rows
        if chunk_size_rows is not None
        else _pipeline_finalize.CHUNK_SIZE_ROWS_DEFAULT
    )
    resolved_threshold = (
        auto_chunk_threshold_rows
        if auto_chunk_threshold_rows is not None
        else AUTO_CHUNK_THRESHOLD_ROWS_DEFAULT
    )
    effective_batch_size_rows = resolved_chunk_size if auto_chunk else batch_size_rows

    capture_kwargs: dict[str, Any] = dict(
        engine_version=ENGINE_VERSION,
        registry=registry,
        auto_chunk=auto_chunk,
        chunk_size_rows=resolved_chunk_size,
        auto_chunk_threshold_rows=resolved_threshold,
        use_byte_estimate_routing=use_byte_estimate_routing,
    )
    if out_of_core_threshold_rows is not None:
        capture_kwargs["out_of_core_threshold_rows"] = out_of_core_threshold_rows

    inputs = capture_physical_plan_inputs(config, resolved_sources, **capture_kwargs)
    plan = compile_physical_plan(inputs)

    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan,
        key_provider=key_provider,
        batch_size_rows=effective_batch_size_rows,
        relationship_graph=inputs.graph,
    )
    snapshot = capture_shadow_snapshot(resolved_sources)
    shadow_result = ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)

    oracle_result = run_pipeline(
        config,
        resolved_sources,
        engine_version=ENGINE_VERSION,
        substrate="pandas",
        execution_mode="full_frame",
        auto_chunk=auto_chunk,
        chunk_size_rows=resolved_chunk_size,
        auto_chunk_threshold_rows=resolved_threshold,
        native_route_enabled=False,
        key_provider=key_provider,
        registry=registry,
        sink=None,
    )

    if legacy_pair_given:
        assert table_name is not None  # narrowed by the validation above
        return ShadowRun(
            plan=plan,
            shadow=shadow_result,
            oracle=oracle_result,
            shadow_identity=snapshot.identity(table_name),
        )
    return ShadowRun(
        plan=plan,
        shadow=shadow_result,
        oracle=oracle_result,
        shadow_identity=None,
        shadow_identities={name: snapshot.identity(name) for name in resolved_sources},
    )


def _canonicalize(value: Any) -> Any:
    """Recursively turn `value` into a hashable, order-appropriate shape so a
    diagnostic record's (possibly nested) fields can serve as a `Counter`
    key (Task 4.6 slice 3 fix: the OOC WARN-orphan payload nests a `dict`
    with `list` values -- `QualityWarning.detail` -- which the old
    `tuple(sorted(vars(item).items()))` key could not hash at all).

    `str`/`bytes` are scalar leaves (never iterated character-by-character).
    A `Mapping`'s items and any `set`/`frozenset` become a `frozenset`: both
    are unordered by definition, so two logically-equal-but-differently-
    ordered instances must produce the same key. A `list`/`tuple` keeps its
    position order (a sequence's order IS part of its value) as a plain
    `tuple`. Every branch is wrapped in a type-tag tuple so a `dict` and a
    same-shaped `set`/`list` never collide on the same canonical value.
    Never sorts a value's own contents directly -- only the outer field
    dict, keyed by field NAME (always a unique string), which sorts without
    ever comparing two heterogeneous values against each other.
    """
    if isinstance(value, (str, bytes)):
        return value
    if isinstance(value, Mapping):
        return ("dict", frozenset((_canonicalize(k), _canonicalize(v)) for k, v in value.items()))
    if isinstance(value, (set, frozenset)):
        return ("set", frozenset(_canonicalize(v) for v in value))
    if isinstance(value, (list, tuple)):
        return ("seq", tuple(_canonicalize(v) for v in value))
    return value


def _diag_key(item: Any) -> tuple[Any, ...]:
    # Order-independent multiset key for a warning or row-error record: every
    # field except a wall-clock timing one (this slice's zero-diagnostic
    # strategies never emit either, but the key stays generic on purpose).
    # Sorted by field NAME (a unique string per record), so the sort never
    # needs to compare two canonicalized values against each other.
    if not hasattr(item, "__dict__"):
        return (repr(item),)
    return tuple(sorted((name, _canonicalize(val)) for name, val in vars(item).items()))


def assert_diagnostics_multisets_equal(
    shadow_items: tuple[Any, ...], oracle_items: tuple[Any, ...], label: str
) -> None:
    got, want = (
        Counter(_diag_key(i) for i in shadow_items),
        Counter(_diag_key(i) for i in oracle_items),
    )
    if got != want:
        raise ShadowDifference(
            code=DIAGNOSTICS_DIFF,
            detail=f"{label}: missing={list((want - got).elements())} extra={list((got - want).elements())}",
        )


def assert_shadow_matches_oracle(run: ShadowRun) -> None:
    """The C3 exit-gate comparison: value/null/order/row-count/schema hard
    failures, diagnostics as order-independent multisets, and (already
    enforced inside `ShadowCoordinator.run` itself) planned==actual operator
    per node. Raises the first coded `ShadowDifference` found.
    """
    # Same-input is a property of the fixture, not a cross-check here: the
    # harness hands the IDENTICAL resident `pa.Table` object to both the shadow
    # and the oracle, and the fixture file is made read-only (see
    # `run_shadow_and_oracle` / the read-only fixture test). Re-hashing that one
    # `source` for "both sides" would be tautological (dennis MEDIUM-2), so the
    # snapshot digest is recorded (`run.shadow_identity`) but not cross-asserted
    # here; the value/null/order/row-count/schema hard-compare below is what
    # actually proves the two masked identical bytes. `SNAPSHOT_IDENTITY_DIFF`
    # stays a catalog code for the Task 4.5 single-open production reader, where
    # the two sides read the source independently and the check is non-circular.
    assert_diagnostics_multisets_equal(run.shadow.warnings, tuple(run.oracle.warnings), "warnings")
    assert_diagnostics_multisets_equal(
        run.shadow.row_errors, tuple(run.oracle.row_errors), "row_errors"
    )

    shadow_tables, oracle_tables = set(run.shadow.outputs), set(run.oracle.outputs)
    if shadow_tables != oracle_tables:
        raise ShadowDifference(
            code=SCHEMA_DIFF,
            detail=f"output table set differs: shadow={sorted(shadow_tables)} oracle={sorted(oracle_tables)}",
        )

    for table in sorted(oracle_tables):
        candidate = run.shadow.outputs[table]
        oracle = run.oracle.outputs[table]
        if candidate.column_names != oracle.column_names:
            raise ShadowDifference(
                code=SCHEMA_DIFF,
                detail=f"{table}: column names/order differ: shadow={candidate.column_names} oracle={oracle.column_names}",
            )
        if candidate.num_rows != oracle.num_rows:
            raise ShadowDifference(
                code=ROW_COUNT_DIFF,
                detail=f"{table}: shadow={candidate.num_rows} oracle={oracle.num_rows}",
            )
        for name in oracle.column_names:
            oracle_type = oracle.schema.field(name).type
            candidate_type = candidate.schema.field(name).type
            if not oracle_type.equals(candidate_type):
                raise ShadowDifference(
                    code=SCHEMA_DIFF,
                    detail=f"{table}.{name}: shadow={candidate_type} oracle={oracle_type}",
                )
            oracle_values = oracle.column(name).to_pylist()
            candidate_values = candidate.column(name).to_pylist()
            if candidate_values == oracle_values:
                continue
            oracle_null = [v is None for v in oracle_values]
            candidate_null = [v is None for v in candidate_values]
            if candidate_null != oracle_null:
                raise ShadowDifference(
                    code=NULL_MASK_DIFF, detail=f"{table}.{name}: null positions differ"
                )
            if sorted(map(repr, candidate_values)) == sorted(map(repr, oracle_values)):
                raise ShadowDifference(
                    code=ROW_ORDER_DIFF, detail=f"{table}.{name}: same values, different order"
                )
            raise ShadowDifference(code=CELL_VALUE_DIFF, detail=f"{table}.{name}: values differ")


def _fold_nan(value: object) -> object:
    """Fold IEEE NaN -> None, mirroring `test_out_of_core_fk_parity.py`'s own
    documented normalization: the oracle round-trips every frame through
    pandas (folding NaN to null), while the out-of-core route never touches
    pandas and can leave a genuine float NaN in place. Both mean "missing"."""
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _comparable_ooc(table: pa.Table) -> dict[str, list[object]]:
    """Column -> Python values, NaN folded to None. `to_pydict()` already
    collapses Arrow width drift (`string` vs `large_string`, etc.) to the
    same Python scalar, so nothing else needs normalizing here."""
    return {name: [_fold_nan(v) for v in col] for name, col in table.to_pydict().items()}


# The route-invariant quality_metrics subset (Task 4.6 slice 4, HIGH-2): the
# only key the OOC route and the pandas full_frame oracle are asserted to
# agree on. `code_set_corpora` is corpus-provenance evidence (which corpus,
# how many rows) that both routes compute from the SAME masked output, so it
# must match; no other quality_metrics key is currently populated by a
# Group B/C strategy reachable through this slice's fixtures, so there is
# nothing else in scope to compare.
_ROUTE_INVARIANT_QUALITY_METRIC_KEYS = ("code_set_corpora",)


def assert_ooc_shadow_matches_oracle(run: ShadowRun) -> None:
    """The OUT_OF_CORE-specific parity comparator (Task 4.6 slice 3):
    value-equal vs the full_frame oracle under the two representational
    normalizations `test_out_of_core_fk_parity.py` documents (Arrow width
    drift is already invisible at `to_pydict()`; NaN folds to None), plus the
    diagnostic-multiset check `assert_shadow_matches_oracle` also runs, plus
    (Task 4.6 slice 4) route-invariant `quality_metrics` parity.

    Deliberately NOT a reuse of `assert_shadow_matches_oracle`: that
    comparator demands the oracle's EXACT Arrow schema (`schema.field(...)
    .type.equals(...)`), which OOC's documented, benign normalizations
    legitimately diverge from -- reusing it unchanged would fail a genuinely
    parity-equal OOC result on a representational difference, not a real one.
    """
    assert_diagnostics_multisets_equal(run.shadow.warnings, tuple(run.oracle.warnings), "warnings")
    assert_diagnostics_multisets_equal(
        run.shadow.row_errors, tuple(run.oracle.row_errors), "row_errors"
    )
    for key in _ROUTE_INVARIANT_QUALITY_METRIC_KEYS:
        # `code_set_corpora` is a list of per-(table, column) evidence dicts;
        # `test_out_of_core_group_c_parity.py` compares it order-independently
        # (both routes build it by iterating their own work list, which need
        # not agree on order), so fold each entry through `_canonicalize` (the
        # same order-independent canonicalization diagnostics use above)
        # rather than a raw list `!=`, which would fail on a same-content
        # reorder.
        shadow_entries = Counter(
            _canonicalize(e) for e in (run.shadow.quality_metrics.get(key) or ())
        )
        oracle_entries = Counter(
            _canonicalize(e) for e in (run.oracle.quality_metrics.get(key) or ())
        )
        if shadow_entries != oracle_entries:
            raise ShadowDifference(
                code=DIAGNOSTICS_DIFF,
                detail=(
                    f"quality_metrics[{key!r}]: "
                    f"missing={list((oracle_entries - shadow_entries).elements())} "
                    f"extra={list((shadow_entries - oracle_entries).elements())}"
                ),
            )

    shadow_tables, oracle_tables = set(run.shadow.outputs), set(run.oracle.outputs)
    if shadow_tables != oracle_tables:
        raise ShadowDifference(
            code=SCHEMA_DIFF,
            detail=f"output table set differs: shadow={sorted(shadow_tables)} oracle={sorted(oracle_tables)}",
        )

    for table in sorted(oracle_tables):
        shadow_values = _comparable_ooc(run.shadow.outputs[table])
        oracle_values = _comparable_ooc(run.oracle.outputs[table])
        column_diff = set(shadow_values) ^ set(oracle_values)
        if column_diff:
            raise ShadowDifference(
                code=OOC_FK_PARITY_DIFF, detail=f"{table}: {len(column_diff)} column(s) differ"
            )
        for name, oracle_column in oracle_values.items():
            shadow_column = shadow_values[name]
            if shadow_column == oracle_column:
                continue
            if len(shadow_column) != len(oracle_column):
                raise ShadowDifference(
                    code=OOC_FK_PARITY_DIFF, detail=f"{table}.{name}: row count differs"
                )
            mismatched = sum(1 for a, b in zip(shadow_column, oracle_column, strict=True) if a != b)
            raise ShadowDifference(
                code=OOC_FK_PARITY_DIFF, detail=f"{table}.{name}: {mismatched} row(s) differ"
            )


def assert_route_evidence_matches_plan(run: ShadowRun) -> None:
    """Planned==actual operator per node (C3), asserted independently of the
    coordinator's own internal check, directly against the frozen plan."""
    for table in run.plan.tables:
        for node in table.nodes:
            if node.execution is None:
                continue
            evidence = run.shadow.route_evidence[node.node_id]
            assert evidence.planned_operator == node.execution.operator_id
            assert evidence.actual_operator == node.execution.operator_id
            assert evidence.executed is True
            if node.execution.operator_id in ("native_keyed_hash", "native_faker_select"):
                # Positive compiled-kernel-call evidence (C2; Task 4.6 slice
                # 1 extends it to faker's derive_index_batch call): the
                # node must show the compiled kernel actually ran, never
                # inferred from success alone.
                assert evidence.compiled_kernel_executed is True


def assert_every_node_bound(plan: PhysicalPlan) -> None:
    """Every configured slice-strategy node must have reached native
    admission (`node.execution is not None`); an admission miss would let
    the coordinator silently skip a column instead of exercising it, making
    the corpus's parity claim vacuous for that column."""
    for table in plan.tables:
        for node in table.nodes:
            assert node.execution is not None, (
                f"{node.node_id!r} (strategy={node.strategy!r}) did not reach native "
                "admission; the acceptance corpus must only use native-admissible configs"
            )
