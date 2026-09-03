"""T16 (`docs/plans/2026-09-03-p4-task7-route-seam.md` section 5): forced
reorder (the threshold lowered via the real `out_of_core_reorder_threshold_
rows` kwarg) == `_batch_join` EXACTLY, on the sink path, across the shapes
section 5 names -- orphan policies, sink + `LazySource`, `code_set_corpora`,
warning order+content, unconfigured-column projection warn AND fail,
keyed-mask, exact Arrow schema+metadata, composite + overlapping edges,
empty/null/NaN columns, every admitted payload strategy, and every admitted
parent-key strategy. Where one side raises, the other must raise the same
type+code+message and leave the sink in the same (uncommitted) state.

This is Task 7's HARD invariant (plan section 6.1): route choice changes
timing and memory, never output. The property tests reuse `test_out_of_core_
fk_parity.py`'s case generators (already draw across orphan policy x parent-
key strategy x dtype x null/orphan shape) at the SINK level, comparing the
two routes directly rather than either against the pandas oracle (that
oracle comparison is `_batch_join`'s own contract, unchanged by Task 7 and
pinned separately in `test_out_of_core_fk_parity.py`).
"""

from __future__ import annotations

import math
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import HealthCheck, given, settings

from decoy_engine.execution import ParquetTransactionalSink
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._batch_join import ChildFkBatchJoiner
from decoy_engine.execution.out_of_core._compat import (
    _INITIAL_SUPPORTED_STRATEGIES,
    SUPPORTED_STRATEGIES,
)
from decoy_engine.execution.out_of_core._source import LazySource
from decoy_engine.execution.out_of_core._stream_join import StreamFkJoiner
from decoy_engine.keyprovider import KeyProvider, SecretKeyProvider
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from tests.parity.test_out_of_core_fk_parity import (
    _chain_case,
    _fanout_case,
    _gate_admits,
    _single_edge_case,
)
from tests.parity.test_stream_driver_reorder import (
    _code_set_missing_vs_normal_fixture,
    _overlapping_remap_fixture,
)
from tests.unit.execution.test_de03_output_projection import _fk_graph, _fk_plan, _fk_sources

_REG = get_default_registry()
_BUDGET_BYTES = 1024 * 1024 * 1024  # 1 GiB
_DISK_BYTES = 200 * 1024 * 1024 * 1024

_TMP_COUNTER = [0]


def _tmp_for(label: str) -> Path:
    _TMP_COUNTER[0] += 1
    safe = "".join(c if c.isalnum() else "_" for c in label)[:80]
    return Path(tempfile.mkdtemp(prefix=f"route-seam-parity-{_TMP_COUNTER[0]}-{safe}-"))


def _run_sink(
    plan: Any,
    sources: dict[str, Any],
    graph: RelationshipGraph,
    target: Path,
    *,
    force_reorder: bool,
    key_provider: KeyProvider | None = None,
    unconfigured_column_policy: str | None = None,
) -> tuple[str, Any]:
    kwargs: dict[str, Any] = {}
    if force_reorder:
        kwargs.update(
            budget_bytes=_BUDGET_BYTES,
            temp_disk_budget_bytes=_DISK_BYTES,
            out_of_core_reorder_threshold_rows=0,
        )
    # T16 witness: a forced-reorder run must construct StreamFkJoiner and never
    # ChildFkBatchJoiner. Without this, a route-selection regression would make
    # the forced side silently run _batch_join too, and every parity case would
    # pass vacuously (batch-vs-batch). Spy the constructors around this run only.
    saw = {"batch": False, "reorder": False}
    orig_batch_init = ChildFkBatchJoiner.__init__
    orig_reorder_init = StreamFkJoiner.__init__

    def _spy_batch(self: Any, *a: Any, **k: Any) -> None:
        saw["batch"] = True
        orig_batch_init(self, *a, **k)

    def _spy_reorder(self: Any, *a: Any, **k: Any) -> None:
        saw["reorder"] = True
        orig_reorder_init(self, *a, **k)

    if force_reorder:
        ChildFkBatchJoiner.__init__ = _spy_batch  # type: ignore[method-assign]
        StreamFkJoiner.__init__ = _spy_reorder  # type: ignore[method-assign]
    try:
        result = run_fk_out_of_core(
            plan,
            sources,
            registry=_REG,
            relationship_graph=graph,
            sink=ParquetTransactionalSink(target),
            temp_dir=target.parent / f"{target.name}-work",
            key_provider=key_provider,
            unconfigured_column_policy=unconfigured_column_policy,
            **kwargs,
        )
        status: tuple[str, Any] = ("ok", result)
    except Exception as exc:
        status = ("raised", exc)
    finally:
        if force_reorder:
            ChildFkBatchJoiner.__init__ = orig_batch_init  # type: ignore[method-assign]
            StreamFkJoiner.__init__ = orig_reorder_init  # type: ignore[method-assign]
    if force_reorder:
        # Anti-vacuous witness. A route regression to _batch_join must be caught
        # even when the case raises -- otherwise the raised parity cases could
        # pass vacuously, comparing _batch_join to itself. This assertion is the
        # load-bearing one: a forced-reorder run must NEVER open a batch joiner.
        assert not saw["batch"], (
            "forced-reorder run constructed a ChildFkBatchJoiner: route selection "
            "regressed to _batch_join, making this parity case vacuous"
        )
        # It must also construct a StreamFkJoiner -- EXCEPT when the run fails
        # closed in a route-INDEPENDENT pre-dispatch gate (e.g.
        # validate_outgoing_parent_columns, or a too-small reorder budget), which
        # raises before EITHER driver opens a joiner. That is not a route
        # regression, so only require the reorder joiner on a completed run or one
        # that raised after entering the reorder driver (orphan/projection/mask).
        if status[0] == "ok" or saw["reorder"]:
            assert saw["reorder"], "forced-reorder run constructed no StreamFkJoiner"
    return status


def _warning_key(warning: Any) -> tuple[object, ...]:
    return (warning.code, warning.provider, warning.column, warning.detail)


def _values_equal_folding_nan(a: object, b: object) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def _assert_tables_exactly_equal(batch: pa.Table, reorder: pa.Table, label: str) -> None:
    assert reorder.schema.equals(batch.schema, check_metadata=True), (
        f"{label}: schema diverges\n batch_join={batch.schema}\n reorder={reorder.schema}"
    )
    if reorder.equals(batch, check_metadata=True):
        return
    assert reorder.num_rows == batch.num_rows, f"{label}: row count diverges"
    for name in reorder.schema.names:
        r_col = reorder.column(name).combine_chunks()
        b_col = batch.column(name).combine_chunks()
        if r_col.equals(b_col):
            continue
        assert pa.types.is_floating(r_col.type), (
            f"{label}: non-float column {name!r} diverges\n batch={b_col}\n reorder={r_col}"
        )
        for i, (rv, bv) in enumerate(zip(r_col.to_pylist(), b_col.to_pylist(), strict=True)):
            assert _values_equal_folding_nan(rv, bv), (
                f"{label}: column {name!r}[{i}] diverges: reorder={rv!r} batch={bv!r}"
            )


def _assert_route_parity(
    plan: Any,
    sources: dict[str, Any],
    graph: RelationshipGraph,
    label: str,
    *,
    key_provider: KeyProvider | None = None,
    unconfigured_column_policy: str | None = None,
) -> None:
    if not _gate_admits(plan, graph):
        return
    root = _tmp_for(label)
    batch_dir = root / "batch"
    batch_outcome, batch_val = _run_sink(
        plan,
        sources,
        graph,
        batch_dir,
        force_reorder=False,
        key_provider=key_provider,
        unconfigured_column_policy=unconfigured_column_policy,
    )
    reorder_dir = root / "reorder"
    reorder_outcome, reorder_val = _run_sink(
        plan,
        sources,
        graph,
        reorder_dir,
        force_reorder=True,
        key_provider=key_provider,
        unconfigured_column_policy=unconfigured_column_policy,
    )

    if batch_outcome == "raised":
        assert reorder_outcome == "raised", (
            f"{label}: _batch_join raised {batch_val!r} but the reorder route produced output"
        )
        assert type(batch_val) is type(reorder_val), (
            f"{label}: exception types differ: {type(batch_val)} vs {type(reorder_val)}"
        )
        b_code = getattr(batch_val, "code", None)
        r_code = getattr(reorder_val, "code", None)
        assert b_code == r_code, f"{label}: codes differ: {b_code} vs {r_code}"
        assert str(batch_val) == str(reorder_val), (
            f"{label}: messages differ: {batch_val!r} vs {reorder_val!r}"
        )
        assert not batch_dir.exists(), f"{label}: batch_join sink committed despite raising"
        assert not reorder_dir.exists(), f"{label}: reorder sink committed despite raising"
        return

    assert reorder_outcome == "ok", (
        f"{label}: _batch_join succeeded but the reorder route raised {reorder_val!r}"
    )
    batch_files = sorted(p.name for p in batch_dir.glob("*.parquet"))
    reorder_files = sorted(p.name for p in reorder_dir.glob("*.parquet"))
    assert batch_files == reorder_files, f"{label}: output table set diverges"
    for name in batch_files:
        _assert_tables_exactly_equal(
            pq.read_table(batch_dir / name), pq.read_table(reorder_dir / name), f"{label}:{name}"
        )
    assert [_warning_key(w) for w in reorder_val.warnings] == [
        _warning_key(w) for w in batch_val.warnings
    ], f"{label}: warning order/content diverges"
    assert reorder_val.quality_metrics.get("code_set_corpora") == batch_val.quality_metrics.get(
        "code_set_corpora"
    ), f"{label}: code_set_corpora evidence diverges"


# ---------------------------------------------------------------------------
# Property coverage: orphan policy x parent-key strategy x dtype x null/
# orphan shape, at the sink level, single-edge / chain / fanout topologies.
# ---------------------------------------------------------------------------


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_single_edge_case())
def test_single_edge_sink_route_parity(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    _assert_route_parity(plan, sources, graph, label)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_chain_case())
def test_chain_sink_route_parity(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    _assert_route_parity(plan, sources, graph, label)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_fanout_case())
def test_fanout_sink_route_parity(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    _assert_route_parity(plan, sources, graph, label)


# ---------------------------------------------------------------------------
# Targeted shapes section 5 names by name.
# ---------------------------------------------------------------------------


def test_composite_and_overlapping_edges() -> None:
    plan, sources, graph = _overlapping_remap_fixture()
    _assert_route_parity(plan, sources, graph, "composite-overlapping")


def test_code_set_corpora_evidence_and_stamp_parity() -> None:
    plan, sources, graph = _code_set_missing_vs_normal_fixture()
    _assert_route_parity(plan, sources, graph, "code-set-corpora")


def test_unconfigured_column_warn_policy_parity() -> None:
    _assert_route_parity(
        _fk_plan(),
        _fk_sources(),
        _fk_graph(),
        "unconfigured-warn",
        unconfigured_column_policy="warn",
    )


def test_unconfigured_column_error_policy_parity() -> None:
    _assert_route_parity(
        _fk_plan(),
        _fk_sources(),
        _fk_graph(),
        "unconfigured-error",
        unconfigured_column_policy="error",
    )


def test_keyed_mask_parity() -> None:
    from tests.parity.test_out_of_core_fk_parity import _build_single_edge

    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="hash",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=5,
        child_refs=[0, 1, 2, -1, None],
        parent_nan_row=None,
    )
    secret = SecretKeyProvider(b"a-strong-32B+-managed-secret-value!!", key_version="v1")
    _assert_route_parity(plan, sources, graph, "keyed-mask", key_provider=secret)


def test_lazy_source_sink_parity(tmp_path: Path) -> None:
    from tests.parity.test_out_of_core_fk_parity import _build_single_edge

    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="passthrough",
        policy=OrphanPolicy.WARN,
        parent_rows=5,
        child_refs=[0, 1, -1, 2, None],
        parent_nan_row=None,
    )
    lazy_dir = tmp_path / "lazy-sources"
    lazy_dir.mkdir()
    lazy_sources: dict[str, Any] = {}
    for name, table in sources.items():
        path = lazy_dir / f"{name}.parquet"
        pq.write_table(table, path)
        lazy_sources[name] = LazySource(path)
    _assert_route_parity(plan, lazy_sources, graph, "lazy-source")


def test_empty_and_all_null_child_parity() -> None:
    from tests.parity.test_out_of_core_fk_parity import _build_single_edge

    empty_plan, empty_sources, empty_graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="redact",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=3,
        child_refs=[],
        parent_nan_row=None,
    )
    _assert_route_parity(empty_plan, empty_sources, empty_graph, "empty-child")

    all_null_plan, all_null_sources, all_null_graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="redact",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=3,
        child_refs=[None, None, None],
        parent_nan_row=None,
    )
    _assert_route_parity(all_null_plan, all_null_sources, all_null_graph, "all-null-child")

    nan_plan, nan_sources, nan_graph = _build_single_edge(
        parent_kind="float",
        child_kind="float",
        strategy="truncate",
        policy=OrphanPolicy.PRESERVE,
        parent_rows=4,
        child_refs=[0, 1, 2, None],
        parent_nan_row=1,
    )
    _assert_route_parity(nan_plan, nan_sources, nan_graph, "nan-parent-key")


@pytest.mark.parametrize("strategy", sorted(_INITIAL_SUPPORTED_STRATEGIES))
def test_every_admitted_parent_key_strategy_parity(strategy: str) -> None:
    """Deterministic coverage of every strategy `_check_edge` admits as an FK
    parent key -- the property tests above already sample these, but only
    probabilistically; this guarantees each one runs at least once."""
    from tests.parity.test_out_of_core_fk_parity import _build_single_edge

    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy=strategy,
        policy=OrphanPolicy.PRESERVE,
        parent_rows=5,
        child_refs=[0, 1, 2, -1, None],
        parent_nan_row=None,
    )
    _assert_route_parity(plan, sources, graph, f"parent-key-strategy-{strategy}")


def _payload_column_seed(strategy: str) -> tuple[ColumnSeed, list[str | None]]:
    """One (`ColumnSeed`, source values) pair per payload strategy `_compat.
    SUPPORTED_STRATEGIES` admits, applied to a non-FK "payload" column on the
    child table -- minimal, working `provider_config`s per strategy."""
    values = ["v0", "v1", "v2", None, "v3"]
    if strategy == "bucket_perturb":
        return (
            ColumnSeed(
                namespace="dates",
                strategy=strategy,
                provider=strategy,
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=True,
                provider_config=(("bucket", "week"), ("date_format", "%Y-%m-%d")),
                coherent_with=(),
            ),
            ["2024-01-08", "2024-02-14", "2024-03-21", None, "2024-04-30"],
        )
    if strategy == "categorical":
        return (
            ColumnSeed(
                namespace="cats",
                strategy=strategy,
                provider=strategy,
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=True,
                provider_config=(("categories", ["A", "B", "C"]),),
                coherent_with=(),
            ),
            values,
        )
    if strategy == "code_set":
        return (
            ColumnSeed(
                namespace=None,
                strategy=strategy,
                provider=strategy,
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=False,
                provider_config=(("code_set", "icd10"), ("mode", "mask")),
                coherent_with=(),
            ),
            values,
        )
    if strategy == "fpe":
        return (
            ColumnSeed(
                namespace="fpe_ns",
                strategy=strategy,
                provider=strategy,
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=True,
                provider_config=(("charset", "digits"),),
                coherent_with=(),
            ),
            ["12345", "67890", "11111", None, "22222"],
        )
    if strategy == "text_mask":
        return (
            ColumnSeed(
                namespace=None,
                strategy=strategy,
                provider=strategy,
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=False,
                provider_config=(("detectors", ("ssn",)),),
                coherent_with=(),
            ),
            [
                "SSN: 123-45-6789 on file.",
                "no pii here",
                "SSN 987-65-4321 noted",
                None,
                "clean text",
            ],
        )
    if strategy == "text_redact":
        return (
            ColumnSeed(
                namespace=None,
                strategy=strategy,
                provider=strategy,
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=False,
                provider_config=(),
                coherent_with=(),
            ),
            ["call 555-123-4567 please", "nothing here", "email a@b.com", None, "plain"],
        )
    if strategy == "truncate":
        return (
            ColumnSeed(
                namespace=None,
                strategy=strategy,
                provider=strategy,
                backend_type="faker",
                backend_version="v",
                cardinality_mode="reuse",
                deterministic=False,
                provider_config=(("length", 3),),
                coherent_with=(),
            ),
            values,
        )
    # hash, redact, passthrough: no config needed (hash still needs a namespace).
    return (
        ColumnSeed(
            namespace="ns_payload" if strategy == "hash" else None,
            strategy=strategy,
            provider=strategy,
            backend_type="faker",
            backend_version="v",
            cardinality_mode="reuse",
            deterministic=strategy == "hash",
            provider_config=(),
            coherent_with=(),
        ),
        values,
    )


@pytest.mark.parametrize("strategy", sorted(SUPPORTED_STRATEGIES))
def test_every_admitted_payload_strategy_parity(strategy: str) -> None:
    """Deterministic coverage of every PAYLOAD (non-FK) strategy `_compat.
    SUPPORTED_STRATEGIES` admits, on a table that also carries one FK edge."""
    payload_seed, payload_values = _payload_column_seed(strategy)
    key_seed = ColumnSeed(
        namespace="ns_key",
        strategy="passthrough",
        provider="passthrough",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(),
        coherent_with=(),
    )
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x33" * 8,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", key_seed),), per_group=())),
                (
                    "child",
                    TableSeed(
                        per_column=(("fk", key_seed), ("payload", payload_seed)),
                        per_group=(),
                    ),
                ),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace="ns_key",
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    parent = pa.table(
        {"pk": pa.array([f"p{i}" for i in range(len(payload_values))], type=pa.string())}
    )
    child = pa.table(
        {
            "fk": pa.array([f"p{i}" for i in range(len(payload_values))], type=pa.string()),
            "payload": pa.array(payload_values, type=pa.string()),
        }
    )
    _assert_route_parity(
        plan, {"parent": parent, "child": child}, graph, f"payload-strategy-{strategy}"
    )


# ---------------------------------------------------------------------------
# Codex-final route-dependent EXCEPTION-parity gaps (docs/plans/2026-09-03-
# p4-task7-route-seam.md): an undeclared output column and an unmaskable fpe
# value can both fire on the same table, and the two routes used to resolve
# that race differently. The output-projection hoist in `_stream_driver.py`
# (projection enforced before phase-1 masking, matching the batch route's
# always-projection-first order) fixes the projection x masking case; the
# orphan-FAIL x masking case is a Cam-approved narrowed-contract carve-out
# (both fail closed, codes MAY differ), not a bug this hoist can close -- the
# reorder route is single-read, so its own FAIL precount cannot run before
# phase-1 masking the way the batch route's total-orphan prepass can.
# ---------------------------------------------------------------------------

_UNDECLARED_OUTPUT_CODE = "undeclared_output_columns"
_ORPHAN_FAIL_CODE = "orphan_fk_violation"
_FPE_UNENCRYPTABLE_CODE = "fpe_unencryptable_value"


def _passthrough_fk_seed(namespace: str) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy="passthrough",
        provider="passthrough",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=False,
        provider_config=(),
        coherent_with=(),
    )


def _fpe_seed(charset: str = "digits") -> ColumnSeed:
    return ColumnSeed(
        namespace="fpe_ns",
        strategy="fpe",
        provider="fpe",
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=True,
        provider_config=(("charset", charset),),
        coherent_with=(),
    )


def test_projection_before_masking_wins_on_both_routes() -> None:
    """Codex-final gap 1: `child` carries an output column no strategy
    declares (`undeclared_col`, under `unconfigured_column_policy="error"`)
    AND an fpe column whose one value ('abc') has no character in its
    digits-only charset (unmaskable -- see `_strategies/_fpe.py`). Post-hoist,
    the reorder route now enforces output projection BEFORE phase-1 masking,
    matching the batch route's order, so BOTH routes raise the SAME
    undeclared_output_columns error. Without the hoist, the reorder side
    would instead run `mask_batch` first and raise fpe_unencryptable_value --
    diverging from the batch side's projection error; this test pins the fix
    (see the sibling carve-out test below for the one case this hoist does
    NOT unify).
    """
    namespace = "ns_proj_mask"
    fk_seed = _passthrough_fk_seed(namespace)
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x44" * 8,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", fk_seed),), per_group=())),
                # `undeclared_col` is deliberately NOT declared -> undeclared output.
                (
                    "child",
                    TableSeed(
                        per_column=(("fk", fk_seed), ("fpe_col", _fpe_seed())),
                        per_group=(),
                    ),
                ),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace=namespace,
        orphan_policy=OrphanPolicy.PRESERVE,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    parent = pa.table({"pk": pa.array(["p0", "p1", "p2"], type=pa.string())})
    child = pa.table(
        {
            "fk": pa.array(["p0", "p1", "p2"], type=pa.string()),
            "fpe_col": pa.array(["12345", "abc", "67890"], type=pa.string()),
            "undeclared_col": pa.array(["u0", "u1", "u2"], type=pa.string()),
        }
    )
    sources = {"parent": parent, "child": child}
    assert _gate_admits(plan, graph)

    root = _tmp_for("projection-x-masking")
    batch_dir = root / "batch"
    batch_outcome, batch_val = _run_sink(
        plan,
        sources,
        graph,
        batch_dir,
        force_reorder=False,
        unconfigured_column_policy="error",
    )
    reorder_dir = root / "reorder"
    reorder_outcome, reorder_val = _run_sink(
        plan,
        sources,
        graph,
        reorder_dir,
        force_reorder=True,
        unconfigured_column_policy="error",
    )

    assert batch_outcome == "raised", f"batch route did not raise: {batch_val!r}"
    assert reorder_outcome == "raised", f"reorder route did not raise: {reorder_val!r}"
    assert type(batch_val) is type(reorder_val), (
        f"exception types differ: {type(batch_val)} vs {type(reorder_val)}"
    )
    assert getattr(batch_val, "code", None) == _UNDECLARED_OUTPUT_CODE
    # The hoist: projection wins on BOTH routes now, not just the batch route.
    assert getattr(reorder_val, "code", None) == _UNDECLARED_OUTPUT_CODE
    assert not batch_dir.exists(), f"batch_join sink committed despite raising: {batch_val!r}"
    assert not reorder_dir.exists(), f"reorder sink committed despite raising: {reorder_val!r}"


def test_orphan_fail_x_masking_both_fail_closed_codes_may_differ() -> None:
    """Codex-final gap 2 -- Cam's narrowed-contract carve-out (2026-09-03):
    `child` has orphan_policy=FAIL with a real orphan row AND an unmaskable
    fpe value, with every output column declared (no undeclared_col, so
    projection cannot fire and mask the ordering this test pins). The batch
    route's own FAIL precount (`_raise_on_total_orphans`) runs before ANY
    masking and raises orphan_fk_violation; the reorder route is single-read,
    so its FAIL precount runs in phase 2, AFTER phase-1 masking has already
    streamed this table's one batch and raised fpe_unencryptable_value. Both
    routes still fail closed -- neither ever commits output -- but the codes
    legitimately differ, so unlike every other case in this file, this test
    does NOT assert same-code parity.
    """
    namespace = "ns_orphan_mask"
    fk_seed = _passthrough_fk_seed(namespace)
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x55" * 8,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", fk_seed),), per_group=())),
                (
                    "child",
                    TableSeed(
                        per_column=(("fk", fk_seed), ("fpe_col", _fpe_seed())),
                        per_group=(),
                    ),
                ),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace=namespace,
        orphan_policy=OrphanPolicy.FAIL,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    parent = pa.table({"pk": pa.array(["p0", "p1", "p2"], type=pa.string())})
    child = pa.table(
        {
            # "missing_parent" matches no parent pk -> a FAIL-policy orphan.
            "fk": pa.array(["p0", "p1", "missing_parent"], type=pa.string()),
            "fpe_col": pa.array(["12345", "abc", "67890"], type=pa.string()),
        }
    )
    sources = {"parent": parent, "child": child}
    assert _gate_admits(plan, graph)

    root = _tmp_for("orphan-x-masking")
    batch_dir = root / "batch"
    batch_outcome, batch_val = _run_sink(plan, sources, graph, batch_dir, force_reorder=False)
    reorder_dir = root / "reorder"
    reorder_outcome, reorder_val = _run_sink(plan, sources, graph, reorder_dir, force_reorder=True)

    assert batch_outcome == "raised", f"batch route did not fail closed: {batch_val!r}"
    assert reorder_outcome == "raised", f"reorder route did not fail closed: {reorder_val!r}"
    assert not batch_dir.exists(), f"batch_join sink committed despite raising: {batch_val!r}"
    assert not reorder_dir.exists(), f"reorder sink committed despite raising: {reorder_val!r}"

    batch_code = getattr(batch_val, "code", None)
    reorder_code = getattr(reorder_val, "code", None)
    # The carve-out: both codes must be one of the two expected fail-closed
    # codes, but they are NOT asserted equal -- that is the point of this test.
    assert batch_code in {_ORPHAN_FAIL_CODE, _FPE_UNENCRYPTABLE_CODE}, batch_code
    assert reorder_code in {_ORPHAN_FAIL_CODE, _FPE_UNENCRYPTABLE_CODE}, reorder_code
    # Pin the actual per-route ordering the carve-out narrative depends on,
    # not just "some fail-closed code": batch's orphan precount always wins
    # before masking; reorder's masking always wins before its orphan check.
    assert batch_code == _ORPHAN_FAIL_CODE, f"batch route raised {batch_code!r}, expected orphan"
    assert reorder_code == _FPE_UNENCRYPTABLE_CODE, (
        f"reorder route raised {reorder_code!r}, expected fpe (masks before its own FAIL check)"
    )


def test_orphan_fail_x_undeclared_column_both_fail_closed_codes_may_differ() -> None:
    """dennis delta HIGH -- the SECOND arm of the same carve-out (no masking
    failure needed). `child` has orphan_policy=FAIL with a real orphan row AND
    an undeclared output column, under unconfigured_column_policy="error". The
    batch route precounts FAIL orphans BEFORE projection and raises
    orphan_fk_violation; the reorder route enforces output projection (hoisted
    before phase-1 masking) BEFORE its phase-2 orphan precount and raises
    undeclared_output_columns first. Same single-read root cause as the fpe
    case: any reorder-side fail-closed error detected before phase 2 preempts
    the orphan error batch surfaces first. Both fail closed, sink uncommitted --
    OUTPUT parity preserved -- so the codes legitimately differ (plan section
    6.1 carve-out).
    """
    namespace = "ns_orphan_proj"
    fk_seed = _passthrough_fk_seed(namespace)
    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x66" * 8,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", fk_seed),), per_group=())),
                # `undeclared_col` deliberately NOT declared -> undeclared output.
                ("child", TableSeed(per_column=(("fk", fk_seed),), per_group=())),
            ),
        )
    )
    edge = RelationshipEdge(
        parent_table="parent",
        parent_columns=("pk",),
        child_table="child",
        child_columns=("fk",),
        namespace=namespace,
        orphan_policy=OrphanPolicy.FAIL,
    )
    graph = RelationshipGraph(edges=(edge,), ordering=())
    parent = pa.table({"pk": pa.array(["p0", "p1", "p2"], type=pa.string())})
    child = pa.table(
        {
            # "missing_parent" matches no parent pk -> a FAIL-policy orphan.
            "fk": pa.array(["p0", "p1", "missing_parent"], type=pa.string()),
            "undeclared_col": pa.array(["u0", "u1", "u2"], type=pa.string()),
        }
    )
    sources = {"parent": parent, "child": child}
    assert _gate_admits(plan, graph)

    root = _tmp_for("orphan-x-undeclared")
    batch_dir = root / "batch"
    batch_outcome, batch_val = _run_sink(
        plan, sources, graph, batch_dir, force_reorder=False, unconfigured_column_policy="error"
    )
    reorder_dir = root / "reorder"
    reorder_outcome, reorder_val = _run_sink(
        plan, sources, graph, reorder_dir, force_reorder=True, unconfigured_column_policy="error"
    )

    assert batch_outcome == "raised", f"batch route did not fail closed: {batch_val!r}"
    assert reorder_outcome == "raised", f"reorder route did not fail closed: {reorder_val!r}"
    assert not batch_dir.exists(), f"batch_join sink committed despite raising: {batch_val!r}"
    assert not reorder_dir.exists(), f"reorder sink committed despite raising: {reorder_val!r}"

    batch_code = getattr(batch_val, "code", None)
    reorder_code = getattr(reorder_val, "code", None)
    assert batch_code in {_ORPHAN_FAIL_CODE, _UNDECLARED_OUTPUT_CODE}, batch_code
    assert reorder_code in {_ORPHAN_FAIL_CODE, _UNDECLARED_OUTPUT_CODE}, reorder_code
    # Pin the per-route ordering: batch's orphan precount wins before projection;
    # reorder's hoisted projection wins before its phase-2 orphan check.
    assert batch_code == _ORPHAN_FAIL_CODE, f"batch route raised {batch_code!r}, expected orphan"
    assert reorder_code == _UNDECLARED_OUTPUT_CODE, (
        f"reorder route raised {reorder_code!r}, expected undeclared (projection precedes orphan)"
    )


# ---------------------------------------------------------------------------
# Codex-final round 2: a MIDDLE table (child on an incoming edge, forced
# reorder-routed, AND parent on an outgoing edge) whose outgoing edge names a
# parent-key column its own source schema lacks. Pre-fix, the reorder driver
# dereferenced that column building its raw-parent projection and raised a
# bare Arrow KeyError, while the batch route raised the coded
# `out_of_core_parent_column_missing` later in its relation build -- a
# route-dependent divergence in both exception TYPE and message. FIX:
# `validate_outgoing_parent_columns` (`_route_policy.py`) now runs
# route-independently in `run_fk_out_of_core` BEFORE either driver dispatches,
# so both routes raise the identical coded error at the identical point,
# pre-empting whatever error the table's OTHER conditions (an undeclared
# output column, an incoming FAIL orphan) would otherwise have raised.
# ---------------------------------------------------------------------------

_PARENT_COLUMN_MISSING_CODE = "out_of_core_parent_column_missing"


def _missing_outgoing_key_chain(
    *,
    incoming_orphan_policy: OrphanPolicy,
    middle_refs: list[int | None],
    include_undeclared_col: bool,
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    """parent -> middle -> grandchild, where `middle` is both a child (incoming
    edge from `parent`) and a parent (outgoing edge to `grandchild`). The
    outgoing edge's parent_columns names `mk`, which the PLAN declares a seed
    for on `middle` (so `_check_edge`'s gate check, which only consults the
    plan's seed envelope, admits the config) but which `middle`'s ACTUAL
    source table omits -- the schema-drift shape `validate_outgoing_parent_
    columns` exists to catch fail-closed instead of letting either driver
    dereference a column that is not there.
    """
    ns_in, ns_out = "ns_missing_in", "ns_missing_out"
    in_seed = _passthrough_fk_seed(ns_in)
    out_seed = _passthrough_fk_seed(ns_out)

    n_parent = 4
    parent = pa.table({"pk": pa.array([f"p{i}" for i in range(n_parent)], type=pa.string())})

    middle_fk = [None if r is None else (f"orphan{r}" if r == -1 else f"p{r}") for r in middle_refs]
    middle_columns: dict[str, pa.Array] = {"pfk": pa.array(middle_fk, type=pa.string())}
    middle_per_column: list[tuple[str, ColumnSeed]] = [("pfk", in_seed), ("mk", out_seed)]
    if include_undeclared_col:
        middle_columns["undeclared_col"] = pa.array(
            [f"u{i}" for i in range(len(middle_refs))], type=pa.string()
        )
    # Deliberately no "mk" column here -- the missing outgoing parent key.
    middle = pa.table(middle_columns)

    grandchild = pa.table(
        {"gcfk": pa.array([f"m{i}" for i in range(len(middle_refs))], type=pa.string())}
    )

    plan = SimpleNamespace(
        seed_envelope=SeedEnvelope(
            job_seed=b"\x77" * 8,
            per_table=(
                ("parent", TableSeed(per_column=(("pk", in_seed),), per_group=())),
                ("middle", TableSeed(per_column=tuple(middle_per_column), per_group=())),
                ("grandchild", TableSeed(per_column=(("gcfk", out_seed),), per_group=())),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="middle",
                child_columns=("pfk",),
                namespace=ns_in,
                orphan_policy=incoming_orphan_policy,
            ),
            RelationshipEdge(
                parent_table="middle",
                parent_columns=("mk",),  # missing from `middle`'s actual schema
                child_table="grandchild",
                child_columns=("gcfk",),
                namespace=ns_out,
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "middle": middle, "grandchild": grandchild}, graph


def test_missing_outgoing_key_preempts_undeclared_column_on_both_routes() -> None:
    """Counterexample 1: no orphan (incoming edge PRESERVE) but `middle` also
    carries an undeclared output column under `unconfigured_column_policy=
    "error"`. Pre-fix this raced with the undeclared-column check on the batch
    route and a bare KeyError on the reorder route; post-fix the route-
    independent gate fires first on BOTH routes, so this is a SAME-error case
    (not a carve-out like the undeclared/orphan races above).
    """
    plan, sources, graph = _missing_outgoing_key_chain(
        incoming_orphan_policy=OrphanPolicy.PRESERVE,
        middle_refs=[0, 1, 2],
        include_undeclared_col=True,
    )
    assert _gate_admits(plan, graph)

    root = _tmp_for("missing-outgoing-key-undeclared")
    batch_dir = root / "batch"
    batch_outcome, batch_val = _run_sink(
        plan, sources, graph, batch_dir, force_reorder=False, unconfigured_column_policy="error"
    )
    reorder_dir = root / "reorder"
    reorder_outcome, reorder_val = _run_sink(
        plan, sources, graph, reorder_dir, force_reorder=True, unconfigured_column_policy="error"
    )

    assert batch_outcome == "raised", f"batch route did not raise: {batch_val!r}"
    assert reorder_outcome == "raised", f"reorder route did not raise: {reorder_val!r}"
    assert type(batch_val) is type(reorder_val), (
        f"exception types differ: {type(batch_val)} vs {type(reorder_val)}"
    )
    assert getattr(batch_val, "code", None) == _PARENT_COLUMN_MISSING_CODE
    assert getattr(reorder_val, "code", None) == _PARENT_COLUMN_MISSING_CODE
    assert str(batch_val) == str(reorder_val), f"messages differ: {batch_val!r} vs {reorder_val!r}"
    assert not batch_dir.exists(), f"batch_join sink committed despite raising: {batch_val!r}"
    assert not reorder_dir.exists(), f"reorder sink committed despite raising: {reorder_val!r}"


def test_missing_outgoing_key_preempts_incoming_fail_orphan_on_both_routes() -> None:
    """Counterexample 2: `middle`'s incoming edge is FAIL with a real orphan
    row (`"orphan-1"` matches no parent `pk`), and every output column is
    declared (no undeclared-column race). Pre-fix this raced with `orphan_fk_
    violation` on both routes' own orphan handling; post-fix the route-
    independent gate fires before either route ever reaches its orphan check,
    so this is also a SAME-error case, not the orphan-vs-masking carve-out.
    """
    plan, sources, graph = _missing_outgoing_key_chain(
        incoming_orphan_policy=OrphanPolicy.FAIL,
        middle_refs=[0, 1, -1],
        include_undeclared_col=False,
    )
    assert _gate_admits(plan, graph)

    root = _tmp_for("missing-outgoing-key-orphan-fail")
    batch_dir = root / "batch"
    batch_outcome, batch_val = _run_sink(plan, sources, graph, batch_dir, force_reorder=False)
    reorder_dir = root / "reorder"
    reorder_outcome, reorder_val = _run_sink(plan, sources, graph, reorder_dir, force_reorder=True)

    assert batch_outcome == "raised", f"batch route did not raise: {batch_val!r}"
    assert reorder_outcome == "raised", f"reorder route did not raise: {reorder_val!r}"
    assert type(batch_val) is type(reorder_val), (
        f"exception types differ: {type(batch_val)} vs {type(reorder_val)}"
    )
    assert getattr(batch_val, "code", None) == _PARENT_COLUMN_MISSING_CODE
    assert getattr(reorder_val, "code", None) == _PARENT_COLUMN_MISSING_CODE
    assert str(batch_val) == str(reorder_val), f"messages differ: {batch_val!r} vs {reorder_val!r}"
    assert not batch_dir.exists(), f"batch_join sink committed despite raising: {batch_val!r}"
    assert not reorder_dir.exists(), f"reorder sink committed despite raising: {reorder_val!r}"


__all__: list[str] = []
