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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

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
from decoy_engine.generation import _plan_entry as _plan_entry_module
from decoy_engine.keyprovider import KeyProvider
from decoy_engine.providers_v2 import ProviderRegistry

if TYPE_CHECKING:
    from decoy_engine.execution._transactional_sink import TransactionalSink

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
    derive_key: Any = None,
    instance_default_locale: str | None = None,
    sink: TransactionalSink | None = None,
    source_loader: Callable[[str], pa.Table] | None = None,
    vault_writer: Any = None,
    fidelity_report: bool = False,
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

    `derive_key` / `instance_default_locale` / `sink` / `source_loader` /
    `vault_writer` / `fidelity_report` (Task 4.6 slice 5a) are threaded
    IDENTICALLY to `capture_physical_plan_inputs`, `ShadowContext.from_key_
    provider` (which turns `sink`/`source_loader`/`vault_writer` into
    presence-only booleans for the generation admission gate), and the
    oracle `run_pipeline` call, so a generate-only caller's shadow and
    oracle sides always see the SAME settings. All six default to the value
    every pre-existing (mask/OOC) caller already got implicitly (`None`/
    `False`), so this is additive.
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
        sink=sink,
        source_loader=source_loader,
        vault_writer=vault_writer,
        fidelity_report=fidelity_report,
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
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
        sink=sink,
        source_loader=source_loader,
        vault_writer=vault_writer,
        fidelity_report=fidelity_report,
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
        sink=sink,
        source_loader=source_loader,
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
        vault_writer=vault_writer,
        fidelity_report=fidelity_report,
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


# ---------------------------------------------------------------------------
# Task 4.6 slice 5a: pure-generate phase-bound differential parity harness.
#
# The mask/OOC comparators above let a coordinator-side exception propagate
# straight out of `run_shadow_and_oracle` (the oracle call never even runs).
# The generation admission gate's whole point is different: it admits a
# malformed-but-in-shape job on PURPOSE and requires BOTH sides to fail with
# the SAME fingerprint, each proven (by a phase-entry spy) to have failed
# INSIDE `generate_tables`, not before it. That needs each side run and
# caught independently, which the linear shadow-then-oracle helper above
# cannot express without changing behavior for its ~100 existing (mask/OOC)
# callers -- so this is a dedicated entry point, not a new mode of that one.
# ---------------------------------------------------------------------------


def _arrow_ipc_stream_bytes(table: pa.Table) -> bytes:
    """`table.combine_chunks()` written as one Arrow IPC stream, for an
    exact-bytes comparison that covers field order, schema types, null
    positions, row order, and every value in one check.

    The `pandas` schema-metadata key is dropped first (Task 4.6 slice 5b-i).
    Any table an oracle mask/echo path produces round-trips through
    `pa.Table.from_pandas`/`to_pandas` (`_pandas_adapter.py`), which embeds a
    `pandas` index-metadata blob the shadow's native-kernel path structurally
    never produces. That blob carries no row data, and slices 1-4's own
    mask-parity comparator (`assert_shadow_matches_oracle`) already never
    compared it (schema TYPE equality, not raw metadata bytes). In a mixed
    job the oracle even echoes the GENERATE outputs back through that same
    pandas boundary (`_pipeline.py` merged_sources -> Step-3 mask-wins-tie),
    so a generate table's final oracle bytes also carry the blob while the
    shadow's raw output does not -- dropping it keeps this byte comparator
    generalizable to the mixed output union without re-litigating settled
    parity over an artifact of the oracle's pandas boundary. Only the
    `pandas` key is removed, not all schema metadata: any other (semantic)
    schema metadata a future output attaches stays compared. The round-trip
    is byte-stable for the shapes the mixed gate admits (see
    `_shadow_mixed._require_roundtrip_stable_generate_outputs`, which inspects
    the materialized generate output and declines any null, floating NaN, or
    nested column); those unstable shapes are declined on the produced tables,
    not papered over here.
    """
    combined = table.combine_chunks()
    metadata = combined.schema.metadata
    if metadata and b"pandas" in metadata:
        trimmed = {k: v for k, v in metadata.items() if k != b"pandas"}
        combined = combined.replace_schema_metadata(trimmed or None)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, combined.schema) as writer:
        writer.write_table(combined)
    return sink.getvalue().to_pybytes()


def assert_generation_tables_arrow_ipc_equal(
    shadow_outputs: dict[str, pa.Table], oracle_outputs: dict[str, pa.Table]
) -> None:
    """The success/success comparator (Task 4.6 slice 5a §3): table-key sets
    match, and each table's Arrow IPC stream bytes (after `combine_chunks()`)
    are byte-identical. Shared by the primary positive matrix and the bounded
    fuzz sweeps so both enforce byte equality, not `Table.equals()` (which can
    miss schema-metadata / field-order differences)."""
    shadow_tables, oracle_tables = set(shadow_outputs), set(oracle_outputs)
    if shadow_tables != oracle_tables:
        raise ShadowDifference(
            code=SCHEMA_DIFF,
            detail=f"output table set differs: shadow={sorted(shadow_tables)} oracle={sorted(oracle_tables)}",
        )
    for table in sorted(oracle_tables):
        if _arrow_ipc_stream_bytes(shadow_outputs[table]) != _arrow_ipc_stream_bytes(
            oracle_outputs[table]
        ):
            raise ShadowDifference(
                code=CELL_VALUE_DIFF, detail=f"{table}: Arrow IPC stream bytes differ"
            )


def assert_generation_outputs_arrow_ipc_equal(run: ShadowRun) -> None:
    """Success/success comparator over a `ShadowRun` (delegates to
    `assert_generation_tables_arrow_ipc_equal`)."""
    assert_generation_tables_arrow_ipc_equal(run.shadow.outputs, run.oracle.outputs)


@dataclass(frozen=True)
class GenerationDifferentialRun:
    """One side's outcome is either `*_tables` (success) or `*_exception`
    (failure), never both -- `run_generation_shadow_and_oracle` guarantees
    this. `*_entered_generate` is the phase-entry spy's verdict: did this
    side's call reach `synthesize._generate_tables_from_config` (the one
    function both `SynthesisStageAdapter` and the oracle's direct
    `generate_tables` call funnel through) before it returned or raised.
    """

    plan: PhysicalPlan
    shadow_tables: dict[str, pa.Table] | None
    shadow_exception: Exception | None
    shadow_entered_generate: bool
    oracle_tables: dict[str, pa.Table] | None
    oracle_exception: Exception | None
    oracle_entered_generate: bool


def _call_with_generate_phase_spy(
    fn: Callable[[], dict[str, pa.Table]],
) -> tuple[dict[str, pa.Table] | None, Exception | None, bool]:
    """Run `fn` (a zero-arg call into either dispatch path) with a spy on the
    ONE function both paths funnel through, so a caught failure can be
    proven to have happened INSIDE generation rather than before it. Catches
    `Exception`, never `BaseException` (Task 4.6 slice 5a §3): an interrupt
    or other process-control signal is not a parity outcome.

    Patches `_plan_entry._generate_tables_from_config` specifically -- the
    NAME `generation._plan_entry.generate_tables` actually calls (bound at
    `_plan_entry` import time via `from ...synthesize import
    _generate_tables_from_config`), not `synthesize`'s own module
    attribute, which `_plan_entry`'s call site never looks up through.
    """
    entered = False
    original = _plan_entry_module._generate_tables_from_config

    def _spy(*args: Any, **kwargs: Any) -> dict[str, pa.Table]:
        nonlocal entered
        entered = True
        return original(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(_plan_entry_module, "_generate_tables_from_config", _spy)
        try:
            result = fn()
        except Exception as exc:  # captured for a differential comparison, never swallowed silently
            return None, exc, entered
    return result, None, entered


def run_generation_shadow_and_oracle(
    config: dict[str, Any],
    *,
    key_provider: KeyProvider | None = None,
    registry: ProviderRegistry | None = None,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
) -> GenerationDifferentialRun:
    """The generate-only differential entry (Task 4.6 slice 5a): builds the
    physical plan + `ShadowContext` exactly as `run_shadow_and_oracle` does
    for a mask job (no sources; `capture_physical_plan_inputs(config, {})`
    gives `tables == ()` and a non-null `synthesis`), then runs the
    coordinator's synthesis dispatch and the public `run_pipeline` oracle
    UNDER phase-entry spies, each side's exception (if any) caught
    independently -- the phase-bound differential parity proof (plan §0/§3).
    """
    inputs = capture_physical_plan_inputs(
        config,
        {},
        engine_version=ENGINE_VERSION,
        registry=registry,
        execution_mode="full_frame",
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan,
        key_provider=key_provider,
        relationship_graph=inputs.graph,
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
    )
    snapshot = capture_shadow_snapshot({})

    def _run_shadow() -> dict[str, pa.Table]:
        result = ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
        return dict(result.outputs)

    def _run_oracle() -> dict[str, pa.Table]:
        oracle_result = run_pipeline(
            config,
            {},
            engine_version=ENGINE_VERSION,
            substrate="pandas",
            execution_mode="full_frame",
            derive_key=derive_key,
            instance_default_locale=instance_default_locale,
            key_provider=key_provider,
            registry=registry,
            sink=None,
        )
        return dict(oracle_result.outputs)

    shadow_tables, shadow_exc, shadow_entered = _call_with_generate_phase_spy(_run_shadow)
    oracle_tables, oracle_exc, oracle_entered = _call_with_generate_phase_spy(_run_oracle)

    return GenerationDifferentialRun(
        plan=plan,
        shadow_tables=shadow_tables,
        shadow_exception=shadow_exc,
        shadow_entered_generate=shadow_entered,
        oracle_tables=oracle_tables,
        oracle_exception=oracle_exc,
        oracle_entered_generate=oracle_entered,
    )


def _failure_fingerprint(exc: BaseException) -> tuple[tuple[str, Any, Any, str], ...]:
    """A recursive identity of one exception chain (Task 4.6 slice 5a §3):
    fully-qualified class, `.code` / `.path` when present, exact `str(exc)`,
    and the same recursively down `__cause__` -- so "different wrapper, same
    cause" is a real mismatch (the oracle never wraps), never accepted as a
    faithful rejection."""
    chain: list[tuple[str, Any, Any, str]] = []
    current: BaseException | None = exc
    while current is not None:
        cls = type(current)
        chain.append(
            (
                f"{cls.__module__}.{cls.__qualname__}",
                getattr(current, "code", None),
                getattr(current, "path", None),
                str(current),
            )
        )
        current = current.__cause__
    return tuple(chain)


def assert_generation_failures_match(run: GenerationDifferentialRun) -> None:
    """The failure/failure comparator (Task 4.6 slice 5a §3): an asymmetric
    outcome (one side succeeded, the other failed) is an unconditional
    parity failure; a symmetric failure must carry matching fingerprints AND
    both phase-entry spies must have fired (never a false faithful-rejection
    from two different phases)."""
    shadow_failed = run.shadow_exception is not None
    oracle_failed = run.oracle_exception is not None
    assert shadow_failed == oracle_failed, (
        f"asymmetric outcome: shadow_exception={run.shadow_exception!r} "
        f"oracle_exception={run.oracle_exception!r}"
    )
    shadow_exc, oracle_exc = run.shadow_exception, run.oracle_exception
    assert shadow_exc is not None and oracle_exc is not None, (
        "expected both sides to fail; both succeeded"
    )
    assert run.shadow_entered_generate, "shadow side failed before entering generate_tables"
    assert run.oracle_entered_generate, "oracle side failed before entering generate_tables"
    shadow_fingerprint = _failure_fingerprint(shadow_exc)
    oracle_fingerprint = _failure_fingerprint(oracle_exc)
    assert shadow_fingerprint == oracle_fingerprint, (
        f"failure fingerprints differ: shadow={shadow_fingerprint} oracle={oracle_fingerprint}"
    )


# ---------------------------------------------------------------------------
# Task 4.6 slice 5b-i: independent-mixed (generate + mask) differential
# harness.
#
# Positive parity for a mixed job reuses `run_shadow_and_oracle` +
# `assert_generation_tables_arrow_ipc_equal` unchanged -- both are already
# generic over an arbitrary output-table union, not generation-specific, so
# no new comparator is needed for the success/success case (plan §7 "the
# byte comparator, generalized to the mixed output union").
#
# The failure/failure case needs more than the pure-generate harness above:
# a malformed generate leaf still funnels through the ONE shared
# `_generate_tables_from_config` call (Codex #5's "RAISED, not merely
# entered" -- the spy below now distinguishes the two), but a malformed MASK
# leaf has no equivalent shared function -- the shadow's native operators
# (`_shadow_coordinator.run_operator`) and the oracle's pandas strategy
# handlers are two independent implementations by design (that
# independence is the whole point of the slices 1-4 parity proof). So
# "did the mask stage raise" is proven by ELIMINATION plus a dispatch
# counter, not a shared-function spy: the oracle is generate-first
# (`_pipeline.py:481` then `505`), so a job that fails without the
# generate-phase spy ever raising, AND with at least one mask dispatch
# observed, is attributable to MASK; a job that fails with the generate
# spy raising, before any mask dispatch, is attributable to GENERATE. The
# dispatch counter also proves the "ZERO mask dispatches" half of the
# dual-fault requirement (plan §3) directly, rather than assuming it from
# the oracle's linear control flow alone.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MixedDifferentialRun:
    """One side's outcome is either `*_tables` (success) or `*_exception`
    (failure), never both. `*_stage` is `"generate"` / `"mask"` / `None`
    (never raised) -- see the section docstring above for how it is
    attributed. `*_mask_dispatch_count` is how many times that side's mask
    implementation was actually invoked (0 for a GENERATE-stage failure,
    which proves the dual-fault "zero mask dispatches" requirement rather
    than assuming it).
    """

    plan: PhysicalPlan
    shadow_tables: dict[str, pa.Table] | None
    shadow_exception: Exception | None
    shadow_stage: str | None
    shadow_mask_dispatch_count: int
    oracle_tables: dict[str, pa.Table] | None
    oracle_exception: Exception | None
    oracle_stage: str | None
    oracle_mask_dispatch_count: int


def _arm_generate_raised_spy(patcher: pytest.MonkeyPatch) -> Callable[[], bool]:
    """Arms the shared generate-phase entry point with a spy that records
    whether THIS call RAISED (the exception propagated through it), not
    merely whether it was entered -- a job that fails in the MASK phase,
    after generation already returned successfully, must not be
    misattributed to GENERATE just because generation ran earlier in the
    same call. Returns a zero-arg accessor for the recorded verdict."""
    raised = False
    original = _plan_entry_module._generate_tables_from_config

    def _spy(*args: Any, **kwargs: Any) -> dict[str, pa.Table]:
        nonlocal raised
        try:
            return original(*args, **kwargs)
        except Exception:
            raised = True
            raise

    patcher.setattr(_plan_entry_module, "_generate_tables_from_config", _spy)
    return lambda: raised


def _arm_shadow_mask_dispatch_counter(patcher: pytest.MonkeyPatch) -> Callable[[], int]:
    """Counts calls to the shadow coordinator's own mask-operator dispatch
    point (`_shadow_coordinator.run_operator`, the bound name the
    per-node loop actually calls -- patching the origin module
    `_shadow_operators` would miss it, same lesson as the generate spy's
    own module-binding note)."""
    from decoy_engine.execution.physical import _shadow_coordinator as _coordinator_module

    count = 0
    original = _coordinator_module.run_operator  # type: ignore[attr-defined]

    def _spy(*args: Any, **kwargs: Any) -> pa.Array:
        nonlocal count
        count += 1
        return original(*args, **kwargs)

    patcher.setattr(_coordinator_module, "run_operator", _spy)
    return lambda: count


def _arm_oracle_mask_dispatch_counter(patcher: pytest.MonkeyPatch) -> Callable[[], int]:
    """Counts calls to the oracle's full_frame mask entrypoint
    (`PandasExecutionAdapter.run`, what `_pipeline.py`'s Step 2 calls as
    `adapter.run(...)` when `substrate="pandas"` -- every mixed test in this
    harness always passes that substrate)."""
    from decoy_engine.execution._pandas_adapter import PandasExecutionAdapter

    count = 0
    original = PandasExecutionAdapter.run

    def _spy(self: PandasExecutionAdapter, *args: Any, **kwargs: Any) -> Any:
        nonlocal count
        count += 1
        return original(self, *args, **kwargs)

    patcher.setattr(PandasExecutionAdapter, "run", _spy)
    return lambda: count


def _run_mixed_side(
    fn: Callable[[], dict[str, pa.Table]],
    arm_mask_counter: Callable[[pytest.MonkeyPatch], Callable[[], int]],
) -> tuple[dict[str, pa.Table] | None, Exception | None, str | None, int]:
    """Run `fn` under the generate-raised spy plus the given side's own
    mask-dispatch counter, and classify the outcome. Catches `Exception`,
    never `BaseException`: an interrupt or other process-control signal is
    not a parity outcome."""
    with pytest.MonkeyPatch.context() as patcher:
        generate_raised = _arm_generate_raised_spy(patcher)
        mask_dispatch_count = arm_mask_counter(patcher)
        try:
            result = fn()
        except Exception as exc:
            count = mask_dispatch_count()
            stage = "generate" if generate_raised() else ("mask" if count > 0 else None)
            return None, exc, stage, count
        return result, None, None, mask_dispatch_count()


def run_mixed_shadow_and_oracle(
    config: dict[str, Any],
    sources: Mapping[str, pa.Table],
    *,
    key_provider: KeyProvider | None = None,
    registry: ProviderRegistry | None = None,
    derive_key: Any = None,
    instance_default_locale: str | None = None,
) -> MixedDifferentialRun:
    """The independent-mixed differential entry (Task 4.6 slice 5b-i):
    builds the physical plan + `ShadowContext` the same way `run_shadow_
    and_oracle` does for a mask job (`sources` covers the mask-kind tables
    only; the generate-kind tables need none), then runs the coordinator's
    mixed dispatch and the public `run_pipeline` oracle UNDER the stage-
    raised spies above, each side's outcome classified independently.
    """
    resolved_sources = dict(sources)
    inputs = capture_physical_plan_inputs(
        config,
        resolved_sources,
        engine_version=ENGINE_VERSION,
        registry=registry,
        execution_mode="full_frame",
    )
    plan = compile_physical_plan(inputs)
    ctx = ShadowContext.from_key_provider(
        plan=inputs.plan,
        key_provider=key_provider,
        relationship_graph=inputs.graph,
        derive_key=derive_key,
        instance_default_locale=instance_default_locale,
    )
    snapshot = capture_shadow_snapshot(resolved_sources)

    def _run_shadow() -> dict[str, pa.Table]:
        result = ShadowCoordinator(ctx=ctx, registry=inputs.registry).run(plan, snapshot)
        return dict(result.outputs)

    def _run_oracle() -> dict[str, pa.Table]:
        oracle_result = run_pipeline(
            config,
            resolved_sources,
            engine_version=ENGINE_VERSION,
            substrate="pandas",
            execution_mode="full_frame",
            derive_key=derive_key,
            instance_default_locale=instance_default_locale,
            key_provider=key_provider,
            registry=registry,
            sink=None,
        )
        return dict(oracle_result.outputs)

    shadow_tables, shadow_exc, shadow_stage, shadow_mask_count = _run_mixed_side(
        _run_shadow, _arm_shadow_mask_dispatch_counter
    )
    oracle_tables, oracle_exc, oracle_stage, oracle_mask_count = _run_mixed_side(
        _run_oracle, _arm_oracle_mask_dispatch_counter
    )

    return MixedDifferentialRun(
        plan=plan,
        shadow_tables=shadow_tables,
        shadow_exception=shadow_exc,
        shadow_stage=shadow_stage,
        shadow_mask_dispatch_count=shadow_mask_count,
        oracle_tables=oracle_tables,
        oracle_exception=oracle_exc,
        oracle_stage=oracle_stage,
        oracle_mask_dispatch_count=oracle_mask_count,
    )


def assert_mixed_failures_match(run: MixedDifferentialRun) -> None:
    """The failure/failure comparator for a mixed job: an asymmetric
    outcome is an unconditional parity failure; a symmetric failure must
    have BOTH sides attributed to the SAME stage (never a silent
    cross-stage "faithful" rejection -- Codex #5), and, for a GENERATE-
    stage failure specifically, zero mask dispatches on both sides (the
    dual-fault requirement); the failure fingerprints must match."""
    shadow_failed = run.shadow_exception is not None
    oracle_failed = run.oracle_exception is not None
    assert shadow_failed == oracle_failed, (
        f"asymmetric outcome: shadow_exception={run.shadow_exception!r} "
        f"oracle_exception={run.oracle_exception!r}"
    )
    shadow_exc, oracle_exc = run.shadow_exception, run.oracle_exception
    assert shadow_exc is not None and oracle_exc is not None, (
        "expected both sides to fail; both succeeded"
    )
    assert run.shadow_stage is not None, (
        f"shadow side failed but no stage raised it (mask_dispatch_count="
        f"{run.shadow_mask_dispatch_count}); the failure happened outside "
        "both spied phases"
    )
    assert run.oracle_stage is not None, (
        f"oracle side failed but no stage raised it (mask_dispatch_count="
        f"{run.oracle_mask_dispatch_count}); the failure happened outside "
        "both spied phases"
    )
    assert run.shadow_stage == run.oracle_stage, (
        f"stage mismatch: shadow={run.shadow_stage!r} oracle={run.oracle_stage!r}"
    )
    if run.shadow_stage == "generate":
        assert run.shadow_mask_dispatch_count == 0, (
            "shadow dispatched mask work despite a GENERATE-stage failure"
        )
        assert run.oracle_mask_dispatch_count == 0, (
            "oracle dispatched mask work despite a GENERATE-stage failure"
        )
    shadow_fingerprint = _failure_fingerprint(shadow_exc)
    oracle_fingerprint = _failure_fingerprint(oracle_exc)
    assert shadow_fingerprint == oracle_fingerprint, (
        f"failure fingerprints differ: shadow={shadow_fingerprint} oracle={oracle_fingerprint}"
    )
