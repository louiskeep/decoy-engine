"""F2 row-error channel: bucketize and top_code on the out-of-core route.

These are row-local in VALUE but emit per-value `format_error` RowErrors. They
run on the out-of-core route through the per-batch oracle-handler bridge
(`out_of_core/_row_error_bridge.py`), so their masked output must be
byte-identical to the full-frame pandas oracle, and an uncoercible cell must
fail the job CLOSED (the chunked route's H1 contract; neither non-full-frame
lane quarantines row errors), with the reported row index ABSOLUTE in the whole
source, never batch-local.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.errors import RowErrorsFailedError
from decoy_engine.execution import PandasExecutionAdapter, ParquetTransactionalSink
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._compat import (
    OutOfCoreCompatibility,
    check_out_of_core_compatibility,
)
from decoy_engine.execution.out_of_core._row_error_bridge import (
    bridge_own_column,
    row_error_bridge_batch_stable,
)
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_MIB = 1024 * 1024
_REORDER_BUDGET = 1024 * _MIB  # generous: a tiny fixture stays below the key ceiling
_DISK = 200 * 1024**3

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_JOB_SEED = b"\x11" * 8


def _seed(strategy: str, *, provider_config: tuple[tuple[str, Any], ...] = ()) -> ColumnSeed:
    return ColumnSeed(
        namespace=None,
        strategy=strategy,
        provider=strategy,
        backend_type="decoy_native",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=provider_config,
        coherent_with=(),
    )


def _key_seed() -> ColumnSeed:
    return ColumnSeed(
        namespace="kns",
        strategy="hash",
        provider="hash",
        backend_type="decoy_native",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(),
        coherent_with=(),
    )


def _job(
    payload_seed: ColumnSeed, parent_vals: list[str | None], child_vals: list[str | None]
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    key = _key_seed()
    parent = pa.table(
        {
            "pk": pa.array([f"p{i}" for i in range(len(parent_vals))], type=pa.string()),
            "pay": pa.array(parent_vals, type=pa.string()),
        }
    )
    child = pa.table(
        {
            "fk": pa.array([f"p{i}" for i in range(len(child_vals))], type=pa.string()),
            "cpay": pa.array(child_vals, type=pa.string()),
        }
    )
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                (
                    "parent",
                    TableSeed(per_column=(("pk", key), ("pay", payload_seed)), per_group=()),
                ),
                (
                    "child",
                    TableSeed(per_column=(("fk", key), ("cpay", payload_seed)), per_group=()),
                ),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


def _bool_job(
    payload_seed: ColumnSeed, parent_vals: list[bool | None], child_vals: list[bool | None]
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    """Same shape as `_job` but with BOOLEAN payload columns, so the batch-vs-
    whole-column pyarrow dtype flip (bool with no nulls -> numpy `bool`; with a
    null -> `object`) is exercised."""
    key = _key_seed()
    parent = pa.table(
        {
            "pk": pa.array([f"p{i}" for i in range(len(parent_vals))], type=pa.string()),
            "pay": pa.array(parent_vals, type=pa.bool_()),
        }
    )
    child = pa.table(
        {
            "fk": pa.array([f"p{i}" for i in range(len(child_vals))], type=pa.string()),
            "cpay": pa.array(child_vals, type=pa.bool_()),
        }
    )
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                (
                    "parent",
                    TableSeed(per_column=(("pk", key), ("pay", payload_seed)), per_group=()),
                ),
                (
                    "child",
                    TableSeed(per_column=(("fk", key), ("cpay", payload_seed)), per_group=()),
                ),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


def _ext_job(
    payload_seed: ColumnSeed,
    parent_vals: list[Any],
    child_vals: list[Any],
    pandas_dtype: str,
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    """Same shape as `_job`, but the payload columns are built from a pandas
    extension-dtype Series via `pa.Table.from_pandas`, so the source Arrow table
    carries the pandas schema metadata (`numpy_type`) the whole-frame oracle
    reads to restore the extension dtype. A nullable `boolean`/`Int64`/`Float64`
    column keeps that dtype under the oracle regardless of null content, so the
    per-batch bridge must force each batch to the SAME dtype rather than the
    metadata-less Arrow->pandas default (which would drop a null-bearing boolean
    to `object` and misread each bool cell as a format error)."""
    key = _key_seed()
    parent = pa.Table.from_pandas(
        pd.DataFrame(
            {
                "pk": pd.array([f"p{i}" for i in range(len(parent_vals))], dtype="string"),
                "pay": pd.array(parent_vals, dtype=pandas_dtype),
            }
        )
    )
    child = pa.Table.from_pandas(
        pd.DataFrame(
            {
                "fk": pd.array([f"p{i}" for i in range(len(child_vals))], dtype="string"),
                "cpay": pd.array(child_vals, dtype=pandas_dtype),
            }
        )
    )
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                (
                    "parent",
                    TableSeed(per_column=(("pk", key), ("pay", payload_seed)), per_group=()),
                ),
                (
                    "child",
                    TableSeed(per_column=(("fk", key), ("cpay", payload_seed)), per_group=()),
                ),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


_DTYPE_DECLINE_CODE = "out_of_core_row_error_dtype_unsupported"


def _gate_verdict(
    plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph
) -> OutOfCoreCompatibility:
    """The compat verdict WITH the real source schemas, so the row-error-bridge
    dtype decline is exercised (the gate skips it when no schemas are passed)."""
    work = order_work(build_work_list(plan, _REG), graph)
    schemas = {name: table.schema for name, table in sources.items()}
    return check_out_of_core_compatibility(plan, work, graph, source_schemas=schemas)


def _gate_admits(plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph) -> bool:
    return _gate_verdict(plan, sources, graph).accepted


def _assert_value_equal(oracle: pa.Table, ooc: pa.Table, label: str) -> None:
    got, want = ooc.to_pydict(), oracle.to_pydict()
    assert set(got) == set(want), f"{label}: columns {set(got)} vs {set(want)}"
    for name in want:
        assert got[name] == want[name], (
            f"{label}: {name!r} diverges\n oracle={want[name]}\n    ooc={got[name]}"
        )


# STRING payload fixtures: only top_code admits a plain string column onto the
# bridge (it parses each object cell exactly; see `_row_error_bridge.py`). The
# bucketize string case is NOT here because bucketize numerically coerces a
# string column batch-dependently and is declined off OOC
# (`test_bucketize_string_declines_oracle_correct`); bucketize's own clean-parity
# coverage runs on a plain float64 column
# (`test_plain_float64_bucketize_admitted_and_matches_oracle`).
_CLEAN = {
    "top_code": (
        _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+"))),
        ["25", "95", "40", None, "10", "72"],
    ),
    "top_code_preset": (
        _seed("top_code", provider_config=(("preset", "hipaa_age"),)),
        ["3", "90", "89", "91", None, "40"],
    ),
}


@pytest.mark.parametrize("kind", list(_CLEAN))
@pytest.mark.parametrize("batch_rows", [None, 3, 2, 1])
def test_row_error_bridge_payload_parity(kind: str, batch_rows: int | None) -> None:
    """A clean (fully coercible) top_code string payload on parent + child is
    byte-identical to the oracle, at every batch size (a batch boundary must not
    change a canonical rendering -- the CHUNK_SAFE property these handlers have)."""
    payload_seed, vals = _CLEAN[kind]
    plan, sources, graph = _job(payload_seed, vals, list(reversed(vals)))
    assert _gate_admits(plan, sources, graph), f"{kind}: expected gate to admit"
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, batch_rows=batch_rows
    )
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], ooc.outputs[table], f"{kind}/batch={batch_rows}")


def test_row_error_fails_closed() -> None:
    """An uncoercible cell on an ADMITTED bridge column fails the job closed with
    RowErrorsFailedError rather than silently keeping the raw value, mirroring the
    chunked route. top_code is the only bridged strategy that can reach this path
    on OOC: it admits a string column (parsed exactly), so an uncoercible cell is
    a bridge-collected RowError. bucketize's only row-error shape is a string
    column too, but bucketize DECLINES strings off OOC
    (`test_bucketize_string_declines_oracle_correct`), so its uncoercible-cell
    fail-closed rides the full-frame oracle after reroute, not this bridge; an
    admitted bucketize column is plain float64, which has no uncoercible cell."""
    seed = _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    plan, sources, graph = _job(seed, ["25", "nope", "40"], ["25", "40", "10"])
    assert _gate_admits(plan, sources, graph)
    with pytest.raises(RowErrorsFailedError) as exc:
        run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    triggers = {r.trigger for r in exc.value.records}
    assert triggers == {"format_error"}, triggers
    assert {r.column for r in exc.value.records} == {"pay"}


@pytest.mark.parametrize("batch_rows", [None, 2, 1])
def test_row_error_index_is_absolute(batch_rows: int | None) -> None:
    """The reported row index is the uncoercible cell's position in the WHOLE
    source, not batch-local: the bad cell at parent row 4 must report row_index 4
    even when it lands in a later batch (the durable Phase-1 row offset). Exercised
    on top_code (a string column, bridge-admitted); bucketize declines strings off
    OOC, so its absolute-index reporting runs through the full-frame oracle, which
    already indexes in the whole source."""
    seed = _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    parent_vals = ["10", "20", "30", "40", "bad", "60"]  # uncoercible at index 4
    plan, sources, graph = _job(seed, parent_vals, ["10", "20", "30", "40", "50", "60"])
    with pytest.raises(RowErrorsFailedError) as exc:
        run_fk_out_of_core(
            plan, sources, registry=_REG, relationship_graph=graph, batch_rows=batch_rows
        )
    pay_errors = [r for r in exc.value.records if r.column == "pay"]
    assert [r.row_index for r in pay_errors] == [4], [r.row_index for r in pay_errors]
    assert pay_errors[0].table == "parent"


# ---------------------------------------------------------------------------
# Dtype decline (Option B): a row-error-bridge column whose per-batch pandas
# conversion is not provably identical to the oracle's whole-frame conversion is
# DECLINED off the OOC route (coded `out_of_core_row_error_dtype_unsupported`)
# and runs on the full-frame oracle, which is correct. The route's own preflight
# refuses such a plan too, so a bare per-batch conversion can never run and emit
# a value that differs from the oracle. Admitting only the provably-stable set
# replaces the round-2/3 per-batch dtype-forcing machinery.
# ---------------------------------------------------------------------------


def _typed_job(
    payload_seed: ColumnSeed, parent_vals: list[Any], child_vals: list[Any], arrow_type: pa.DataType
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    """`_job` with PLAIN (no pandas metadata) payload columns of `arrow_type`."""
    key = _key_seed()
    parent = pa.table(
        {
            "pk": pa.array([f"p{i}" for i in range(len(parent_vals))], type=pa.string()),
            "pay": pa.array(parent_vals, type=arrow_type),
        }
    )
    child = pa.table(
        {
            "fk": pa.array([f"p{i}" for i in range(len(child_vals))], type=pa.string()),
            "cpay": pa.array(child_vals, type=arrow_type),
        }
    )
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=_JOB_SEED,
            per_table=(
                (
                    "parent",
                    TableSeed(per_column=(("pk", key), ("pay", payload_seed)), per_group=()),
                ),
                (
                    "child",
                    TableSeed(per_column=(("fk", key), ("cpay", payload_seed)), per_group=()),
                ),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace="kns",
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child}, graph


def _assert_declines_and_preflight_refuses(
    plan: Any, sources: dict[str, Any], graph: RelationshipGraph
) -> None:
    """The gate declines the job with the dtype code, and `run_fk_out_of_core`'s
    own preflight refuses the same plan (never a bare per-batch run)."""
    verdict = _gate_verdict(plan, sources, graph)
    assert not verdict.accepted, "gate must decline a non-batch-stable row-error column"
    assert _DTYPE_DECLINE_CODE in {r.code for r in verdict.rejections}, [
        r.code for r in verdict.rejections
    ]
    with pytest.raises(ExecutionError) as exc:
        run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)
    assert exc.value.code == _DTYPE_DECLINE_CODE, exc.value.code


def test_plain_arrow_boolean_top_code_declines_oracle_correct() -> None:
    """A PLAIN Arrow boolean top_code column is null-UNSTABLE (numpy `bool` with
    no nulls, `object` with any null), so a batch split could change the handler
    branch: a null-free sub-batch infers numpy `bool` and takes top_code's numeric
    branch (True/False -> "1"/"0"), while the null-bearing whole column is `object`
    and the oracle passes the raw booleans through. That divergence is exactly why
    the column is declined off OOC. The full-frame oracle's whole-column result
    (`[True, None, False]`, passed through) is the correct one, and the preflight
    refuses the OOC route so the batch-split coercion can never run."""
    payload_seed = _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    plan, sources, graph = _bool_job(payload_seed, [True, None, False], [None, True, False])
    _assert_declines_and_preflight_refuses(plan, sources, graph)
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    assert oracle.outputs["parent"].to_pydict()["pay"] == [True, None, False]


def test_top_code_nullable_boolean_declines_oracle_correct() -> None:
    """Round-2 case, now DECLINED: a pandas-nullable `boolean` source carries the
    schema metadata the oracle restores to `BooleanDtype` whole-frame but a bare
    per-batch array drops to `object`, so per-batch parity cannot be proven. The
    job declines off OOC and runs on the full-frame oracle, which coerces
    True/None/False to `["1", None, "0"]` with no row errors."""
    payload_seed = _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    plan, sources, graph = _ext_job(
        payload_seed, [True, None, False], [None, True, False], "boolean"
    )
    _assert_declines_and_preflight_refuses(plan, sources, graph)
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    assert oracle.outputs["parent"].to_pydict()["pay"] == ["1", None, "0"]


def test_top_code_nullable_boolean_parquet_declines(tmp_path: Path) -> None:
    """The same nullable-`boolean` decline, sourced from a pandas-produced Parquet
    file whose footer carries the pandas metadata, so the LazySource schema path
    (used by the gate and preflight) is covered alongside the resident one."""
    payload_seed = _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    plan, sources, graph = _ext_job(
        payload_seed, [True, None, False], [None, True, False], "boolean"
    )
    lazy_sources: dict[str, Any] = {}
    for name, table in sources.items():
        path = tmp_path / f"{name}.parquet"
        pq.write_table(table, path)
        lazy_sources[name] = LazySource(str(path))
    _assert_declines_and_preflight_refuses(plan, lazy_sources, graph)


@pytest.mark.parametrize(
    ("pandas_dtype", "vals"),
    [
        ("Int64", [25, None, 95]),
        ("Float64", [25.0, None, 95.0]),
    ],
)
def test_top_code_nullable_numeric_declines_oracle_correct(
    pandas_dtype: str, vals: list[Any]
) -> None:
    """Nullable Int/Float extension sources carry restoring metadata too, so they
    are declined off OOC (the Arrow->pandas dtype surface is not proven batch
    stable). The full-frame oracle produces the correct result (25 in range, 95
    over the cap, null passthrough)."""
    payload_seed = _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    plan, sources, graph = _ext_job(payload_seed, vals, list(reversed(vals)), pandas_dtype)
    _assert_declines_and_preflight_refuses(plan, sources, graph)
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    assert oracle.outputs["parent"].to_pydict()["pay"] == ["25", None, "90+"]


@pytest.mark.parametrize("arrow_type", [pa.int64(), pa.float64()])
@pytest.mark.parametrize("batch_rows", [None, 3, 2, 1])
def test_plain_numeric_top_code_admitted_and_matches_oracle(
    arrow_type: pa.DataType, batch_rows: int | None
) -> None:
    """The common path must STAY on OOC: a PLAIN int64/float64 top_code column
    (no pandas metadata) is null-stable under top_code's lossless nullable-int
    conversion (int64 -> Int64, float64 -> float64 on every batch), so the gate
    ADMITS it and the OOC route byte-matches the oracle at every batch size. This
    guards against over-declining the fast streaming route."""
    payload_seed = _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    vals: list[Any] = [25, None, 95, 10, 90, 40]
    typed_vals = [
        None if v is None else (float(v) if arrow_type == pa.float64() else v) for v in vals
    ]
    plan, sources, graph = _typed_job(
        payload_seed, typed_vals, list(reversed(typed_vals)), arrow_type
    )
    assert _gate_admits(plan, sources, graph), f"{arrow_type}: expected gate to admit plain numeric"
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, batch_rows=batch_rows
    )
    for table in oracle.outputs:
        _assert_value_equal(
            oracle.outputs[table], ooc.outputs[table], f"{arrow_type}/b={batch_rows}"
        )


def test_dtype_decline_is_a_reroute_signal_not_fatal() -> None:
    """The decline reaches the router as `out_of_core_compatible=False` with the
    coded reason (via `out_of_core_admission`, exactly the signal the auto router
    turns into a fall-back to full-frame/sequential), NOT as an exception. A
    plain-numeric column stays `compatible=True`, so the router keeps it on OOC.
    This guards the reroute contract: a regression that treated the dtype code as
    fatal at routing would flip `compatible` without raising here."""
    from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission

    seed = _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    declined_plan, declined_sources, declined_graph = _ext_job(
        seed, [25, None, 95], [95, None, 25], "Int64"
    )
    compatible, code = out_of_core_admission(
        declined_plan, registry=_REG, graph=declined_graph, caller_sources=declined_sources
    )
    assert compatible is False and code == _DTYPE_DECLINE_CODE, (compatible, code)

    admitted_plan, admitted_sources, admitted_graph = _typed_job(
        seed, [25, None, 95], [95, None, 25], pa.int64()
    )
    assert out_of_core_admission(
        admitted_plan, registry=_REG, graph=admitted_graph, caller_sources=admitted_sources
    ) == (True, None)


@pytest.mark.parametrize("batch_rows", [None, 3, 2, 1])
def test_plain_float64_bucketize_admitted_and_matches_oracle(batch_rows: int | None) -> None:
    """The common bucketize path must STAY on OOC: a PLAIN float64 bucketize
    column (already numeric, and float64 does not null-widen -- it stays float64
    with or without nulls) is the one shape whose `pd.to_numeric` is a
    batch-independent identity, so the gate ADMITS it and the OOC route byte-
    matches the oracle at every batch size. Guards against the string-admit
    tightening over-declining bucketize to nothing."""
    payload_seed = _seed("bucketize", provider_config=(("width", 10),))
    vals: list[Any] = [25.0, 95.0, 40.0, None, 10.0, 72.0]
    plan, sources, graph = _typed_job(payload_seed, vals, list(reversed(vals)), pa.float64())
    assert _gate_admits(plan, sources, graph), "expected gate to admit plain float64 bucketize"
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    ooc = run_fk_out_of_core(
        plan, sources, registry=_REG, relationship_graph=graph, batch_rows=batch_rows
    )
    for table in oracle.outputs:
        _assert_value_equal(oracle.outputs[table], ooc.outputs[table], f"float64/b={batch_rows}")


# ---------------------------------------------------------------------------
# bucketize string admit tightening (Codex counterexample). A plain string /
# large_string column passes the Arrow->pandas batch-stability check (string
# always converts to `object`), but bucketize REINTERPRETS that object column
# through `pd.to_numeric`, whose int-vs-float choice depends on the batch's own
# contents, so the masked VALUE can differ across a batch split. top_code reads
# an object column by exact per-cell parse and stays batch-stable, so only
# bucketize declines string/large_string; `top_code` admission is unchanged.
# ---------------------------------------------------------------------------

_BUCKETIZE_COUNTEREXAMPLE = ["18014398509481993", "0.5"]  # Codex: width=1, splits diverge


@pytest.mark.parametrize("arrow_type", [pa.string(), pa.large_string()])
def test_bucketize_string_batch_split_diverges_counterexample(arrow_type: pa.DataType) -> None:
    """Codex counterexample proving a string column is NOT batch-stable for
    bucketize (the reason it must be declined). The per-batch bridge handler runs
    the column through `pd.to_numeric`, whose int-vs-float choice depends on the
    batch's contents: the whole batch `["18014398509481993", "0.5"]` coerces to
    float64 (the "0.5" forces it) and rounds the first value to 18014398509481996,
    while a lone `["18014398509481993"]` batch stays int64 and renders the exact
    18014398509481992 -- the SAME value, a different masked output across a batch
    boundary. The divergence is a property of the handler + data (it exists
    regardless of the gate); the gate's job is to keep such a column OFF the
    bridge, which `test_bucketize_string_declines_oracle_correct` asserts."""
    seed = _seed("bucketize", provider_config=(("width", 1),))
    masked_whole, _ = bridge_own_column(
        "t",
        "c",
        seed,
        pa.array(_BUCKETIZE_COUNTEREXAMPLE, type=arrow_type),
        registry=_REG,
        job_seed=_JOB_SEED,
        mask_key=b"\x22" * 8,
        row_offset=0,
    )
    split: list[Any] = []
    for i, value in enumerate(_BUCKETIZE_COUNTEREXAMPLE):
        masked, _ = bridge_own_column(
            "t",
            "c",
            seed,
            pa.array([value], type=arrow_type),
            registry=_REG,
            job_seed=_JOB_SEED,
            mask_key=b"\x22" * 8,
            row_offset=i,
        )
        split.extend(masked.to_pylist())
    assert masked_whole.to_pylist() != split, (
        "expected the string bucketize column to diverge across a batch split "
        f"(the counterexample): whole={masked_whole.to_pylist()} split={split}"
    )


@pytest.mark.parametrize("arrow_type", [pa.string(), pa.large_string()])
def test_bucketize_string_declines_oracle_correct(arrow_type: pa.DataType) -> None:
    """The fix: a string/large_string bucketize column is DECLINED off OOC (the
    gate rejects it with the dtype code and `run_fk_out_of_core`'s preflight
    refuses the plan), so the batch-split divergence proven above can never run on
    OOC. The job reroutes to the full-frame oracle, which converts the whole
    column at once and yields the single correct result at ANY batch size. Fails
    pre-fix (the gate admitted strings for bucketize and OOC diverged by batch
    size); passes post-fix."""
    payload_seed = _seed("bucketize", provider_config=(("width", 1),))
    vals = _BUCKETIZE_COUNTEREXAMPLE
    plan, sources, graph = _typed_job(payload_seed, vals, list(reversed(vals)), arrow_type)

    # Admit rule: string/large_string is no longer batch-stable for bucketize.
    assert not row_error_bridge_batch_stable(sources["parent"].schema, "pay", "bucketize")

    # Job level: declines with the dtype code; the route's preflight refuses it.
    _assert_declines_and_preflight_refuses(plan, sources, graph)

    # The decline is a reroute signal (fall back to full-frame), not fatal.
    from decoy_engine.execution._pipeline_routing_signals import out_of_core_admission

    compatible, code = out_of_core_admission(
        plan, registry=_REG, graph=graph, caller_sources=sources
    )
    assert compatible is False and code == _DTYPE_DECLINE_CODE, (compatible, code)

    # The full-frame oracle (what the reroute runs) gives the one correct result,
    # independent of any batch size.
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    assert oracle.outputs["parent"].to_pydict()["pay"] == ["18014398509481996", "0"]
    assert oracle.outputs["child"].to_pydict()["cpay"] == ["0", "18014398509481996"]


# ---------------------------------------------------------------------------
# Reorder driver (sink path): the child's payload masks through the reorder
# driver's own Phase-1 bridge, the parent's through the batch driver.
# ---------------------------------------------------------------------------


def _run_sink(plan: Any, sources: Any, graph: Any, target: Path) -> dict[str, bool]:
    saw = {"batch": False, "reorder": False}
    orig_batch, orig_reorder = ChildFkBatchJoiner.__init__, StreamFkJoiner.__init__

    def spy_batch(self: Any, *a: Any, **k: Any) -> None:
        saw["batch"] = True
        orig_batch(self, *a, **k)

    def spy_reorder(self: Any, *a: Any, **k: Any) -> None:
        saw["reorder"] = True
        orig_reorder(self, *a, **k)

    ChildFkBatchJoiner.__init__ = spy_batch  # type: ignore[method-assign]
    StreamFkJoiner.__init__ = spy_reorder  # type: ignore[method-assign]
    try:
        run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=ParquetTransactionalSink(target),
            temp_dir=target.parent / f"{target.name}-work",
            budget_bytes=_REORDER_BUDGET,
            temp_disk_budget_bytes=_DISK,
            out_of_core_reorder_threshold_rows=0,
        )
    finally:
        ChildFkBatchJoiner.__init__ = orig_batch  # type: ignore[method-assign]
        StreamFkJoiner.__init__ = orig_reorder  # type: ignore[method-assign]
    return saw


@pytest.mark.parametrize("kind", ["bucketize_float64", "top_code"])
def test_row_error_bridge_reorder_driver_parity(kind: str, tmp_path: Path) -> None:
    """The child payload masks through the REORDER driver's Phase-1 bridge
    (threshold_rows=0 forces it); its sink output stays byte-identical to the
    oracle, and the parent's payload (batch driver) does too. bucketize runs on a
    plain float64 column (its only OOC-admitted shape); top_code on a string
    column (bridge-admitted by exact parse)."""
    if kind == "bucketize_float64":
        payload_seed = _seed("bucketize", provider_config=(("width", 10),))
        fvals: list[Any] = [25.0, 95.0, 40.0, None, 10.0, 72.0]
        plan, sources, graph = _typed_job(payload_seed, fvals, list(reversed(fvals)), pa.float64())
    else:
        payload_seed, vals = _CLEAN["top_code"]
        plan, sources, graph = _job(payload_seed, vals, list(reversed(vals)))
    oracle = PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )
    out = tmp_path / "out"
    saw = _run_sink(plan, sources, graph, out)
    assert saw["reorder"], "threshold_rows=0 should route the child to the reorder driver"
    for name in ("parent", "child"):
        written = pq.read_table(out / f"{name}.parquet")
        _assert_value_equal(oracle.outputs[name], written, f"reorder/{kind}:{name}")


def test_row_error_bridge_reorder_driver_fails_closed(tmp_path: Path) -> None:
    """A bad cell in the CHILD payload fails closed on the reorder driver too.
    top_code on a string column (bridge-admitted); bucketize declines strings off
    OOC, so a bad bucketize cell cannot reach the bridge's fail-closed path."""
    seed = _seed("top_code", provider_config=(("cap", 89), ("over_label", "90+")))
    plan, sources, graph = _job(seed, ["10", "20", "30"], ["10", "nope", "30"])
    with pytest.raises(RowErrorsFailedError) as exc:
        _run_sink(plan, sources, graph, tmp_path / "out")
    assert {r.trigger for r in exc.value.records} == {"format_error"}
    assert {r.column for r in exc.value.records} == {"cpay"}
