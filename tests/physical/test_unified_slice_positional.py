"""C5b-iii acceptance: position-keyed categorical and non-deterministic REUSE Faker on the
unified full-frame route.

Plan: `docs/plans/2026-10-06-c5b-iii-unified-positional.md` rev 2, section 5. Three rules
run through the file:

- Explicit lane-off oracle. Every reference run passes `unified_slice_enabled=False` and
  asserts the unified lane did not activate; the shadow helper's own oracle runs with the
  lane default (on), so it cannot be the reference for these columns.
- Non-vacuity. The lane run poisons `PandasExecutionAdapter.run`, so a silent decline to
  the oracle cannot pass.
- Distinct keys. Every fixture keys with a secret provider, so `mask_key != job_seed` and a
  swapped key changes the output.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from decoy_engine.config import PipelineConfig
from decoy_engine.execution import run_pipeline
from decoy_engine.execution._runner import build_work_list
from decoy_engine.execution._unified_slice import QUALITY_METRICS_KEY, UnifiedSliceInvariantError
from decoy_engine.execution.native._chunked_evidence import ARROW_PYTHON
from decoy_engine.execution.native._plan import native_route_eligibility
from decoy_engine.execution.physical import _shadow_bindings
from decoy_engine.execution.physical._compiler import compile_physical_plan
from decoy_engine.execution.physical._shadow_context import ShadowContext
from decoy_engine.execution.physical._shadow_operators import OperatorCallEvidence
from decoy_engine.execution.physical._snapshot import capture_physical_plan_inputs
from decoy_engine.instrumentation.timing import StrategyTimingRecord
from tests.native._chunked_faker_support import POOL_SIZE, default_namespace, expected_values
from tests.physical.test_unified_slice_faker import (
    ENGINE_VERSION,
    NEEDS_COMPANION,
    Case,
    _key_provider,
    lane_run,
)
from tests.physical.test_unified_slice_parity import _assert_full_parity

CATEGORIES = ["alpha", "beta", "gamma", "delta"]
WEIGHTS = [0.55, 0.25, 0.15, 0.05]
CATEGORICAL_OP = "native_categorical"
FAKER_OP = "native_faker_select"
_ALT_SECRET = bytes(reversed(range(32)))


def cat_col(
    name: str = "c",
    *,
    weighted: bool = False,
    namespace: str | None = "ns_cat",
    categories: list[Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """The seeded non-deterministic categorical: no `deterministic` flag."""
    cfg: dict[str, Any] = {"categories": list(CATEGORIES if categories is None else categories)}
    if weighted:
        cfg["weights"] = list(WEIGHTS)
    col: dict[str, Any] = {"name": name, "strategy": "categorical", "provider_config": cfg}
    if namespace is not None:
        col["namespace"] = namespace
    col.update(extra)
    return col


def nd_faker(
    name: str = "c",
    *,
    namespace: str | None = "ns_faker",
    provider: str = "person_first_name",
    pool_size: int | None = POOL_SIZE,
    **extra: Any,
) -> dict[str, Any]:
    """The non-deterministic REUSE Faker: `deterministic` unset, explicit `pool_size`."""
    col: dict[str, Any] = {"name": name, "strategy": "faker", "provider": provider}
    if pool_size is not None:
        col["pool_size"] = pool_size
    if namespace is not None:
        col["namespace"] = namespace
    col.update(extra)
    return col


# (id, column builder, operator id)
VARIANTS: list[tuple[str, Callable[[], dict[str, Any]], str]] = [
    ("cat_uniform", lambda: cat_col(), CATEGORICAL_OP),
    ("cat_weighted", lambda: cat_col(weighted=True), CATEGORICAL_OP),
    ("faker_ns", lambda: nd_faker(), FAKER_OP),
    ("faker_no_ns", lambda: nd_faker(namespace=None), FAKER_OP),
    ("faker_empty_ns", lambda: nd_faker(namespace=""), FAKER_OP),
]
VARIANT_PARAMS = [pytest.param(build, op, id=vid) for vid, build, op in VARIANTS]


def str_source(
    n: int, *, mod: int = 5, null_at: Callable[[int], bool] | None = None, name: str = "c"
) -> pa.Table:
    values = [None if null_at is not None and null_at(i) else f"src_{i % mod}" for i in range(n)]
    return pa.table({name: pa.array(values, type=pa.string())})


@contextmanager
def lane_batch_rows(rows: int) -> Iterator[None]:
    """The unified lane builds its coordinator context with the default batch size; this
    narrows it so a small table spans several batches."""
    original = ShadowContext.from_key_provider.__func__  # type: ignore[attr-defined]

    def narrowed(cls: type[ShadowContext], **kwargs: Any) -> ShadowContext:
        kwargs["batch_size_rows"] = rows
        return original(cls, **kwargs)  # type: ignore[no-any-return]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ShadowContext, "from_key_provider", classmethod(narrowed))
        yield


def parity(case: Case, *, batch: int | None = None, **kwargs: Any) -> dict[str, Any]:
    """Lane-on (oracle poisoned, optional narrowed batch) equals an explicit lane-off run."""
    off = case.run(lane=False, **kwargs)
    assert QUALITY_METRICS_KEY not in off.quality_metrics
    if batch is None:
        on = lane_run(case, **kwargs)
    else:
        with lane_batch_rows(batch):
            on = lane_run(case, **kwargs)
    return _assert_full_parity(off, on)


def node_evidence(leaf: dict[str, Any], operator: str) -> dict[str, Any]:
    found = [e for e in leaf["nodes"].values() if e["operator"] == operator]
    assert len(found) == 1, leaf["nodes"]
    return found[0]


# ---------------------------------------------------------------------------
# 1. Unified parity against an explicit lane-off oracle.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("build, operator", VARIANT_PARAMS)
def test_1_multi_batch_ragged_final_batch_with_boundary_nulls(
    tmp_path: Path, build: Callable[[], dict[str, Any]], operator: str
) -> None:
    batch = 7
    # Nulls just before, at and across each batch boundary: a later non-null row must keep
    # its physical ordinal, because a null consumes one.
    source = str_source(40, null_at=lambda i: i % batch in (batch - 1, 0))
    leaf = parity(Case(tmp_path, source, [build()]), batch=batch)
    evidence = node_evidence(leaf, operator)
    assert evidence["executed"] is True
    assert evidence["compiled_kernel_executed"] is True
    assert evidence["calls"] == 6  # 5 full batches + a ragged final one


@NEEDS_COMPANION
@pytest.mark.parametrize("build, operator", VARIANT_PARAMS)
def test_1_default_batch_single_pass(
    tmp_path: Path, build: Callable[[], dict[str, Any]], operator: str
) -> None:
    source = str_source(300, mod=11, null_at=lambda i: i % 9 == 0)
    leaf = parity(Case(tmp_path, source, [build()]))
    assert node_evidence(leaf, operator)["calls"] == 1


_SHAPES: dict[str, Callable[[], pa.Table]] = {
    "all_null": lambda: pa.table({"c": pa.array([None] * 12, type=pa.string())}),
    "single_row": lambda: str_source(1),
    "zero_rows": lambda: pa.table({"c": pa.array([], type=pa.string())}),
}


@NEEDS_COMPANION
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("build, operator", VARIANT_PARAMS)
def test_1_degenerate_shapes(
    tmp_path: Path, build: Callable[[], dict[str, Any]], operator: str, shape: str
) -> None:
    leaf = parity(Case(tmp_path, _SHAPES[shape](), [build()]), batch=4)
    assert node_evidence(leaf, operator)["executed"] is True


@NEEDS_COMPANION
@pytest.mark.parametrize("build, operator", VARIANT_PARAMS)
def test_1_a_second_column_beside_the_variant(
    tmp_path: Path, build: Callable[[], dict[str, Any]], operator: str
) -> None:
    source = pa.table(
        {
            "c": pa.array([f"src_{i % 6}" for i in range(33)], type=pa.string()),
            "p": pa.array([f"keep_{i}" for i in range(33)], type=pa.string()),
        }
    )
    parity(Case(tmp_path, source, [build(), {"name": "p", "strategy": "passthrough"}]), batch=10)


# ---------------------------------------------------------------------------
# 2. Batch offset correctness.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("build, operator", VARIANT_PARAMS)
@pytest.mark.parametrize("batch", [1, 2, 7, 13, 29])
def test_2_one_source_value_draws_by_global_position_across_batches(
    tmp_path: Path, build: Callable[[], dict[str, Any]], operator: str, batch: int
) -> None:
    # Every row holds the same source value, so only the global position can vary the draw.
    source = pa.table({"c": pa.array(["same"] * 29, type=pa.string())})
    case = Case(tmp_path, source, [build()])
    parity(case, batch=batch)
    with lane_batch_rows(batch):
        split = lane_run(case).outputs["t"].column("c").to_pylist()
    whole = lane_run(case).outputs["t"].column("c").to_pylist()
    assert split == whole
    assert len(set(whole)) > 1, "fixture is degenerate: position never changes the draw"


# ---------------------------------------------------------------------------
# 3. Keys: distinct mask_key and job_seed.
# ---------------------------------------------------------------------------


def _ctx_for(case: Case) -> ShadowContext:
    inputs = capture_physical_plan_inputs(case.config, {"t": case.source}, engine_version="k")
    return ShadowContext.from_key_provider(plan=inputs.plan, key_provider=_key_provider())


def test_3_the_fixture_keys_are_distinct(tmp_path: Path) -> None:
    ctx = _ctx_for(Case(tmp_path, str_source(5), [nd_faker()]))
    assert ctx.mask_key != ctx.job_seed


@NEEDS_COMPANION
def test_3_positional_faker_keys_on_job_seed_not_mask_key(tmp_path: Path) -> None:
    source = str_source(90, mod=9, null_at=lambda i: i % 10 == 3)
    case = Case(tmp_path, source, [nd_faker()])
    base = lane_run(case).outputs["t"].column("c").to_pylist()
    # The secret feeds mask_key only: a positional Faker draw must not move with it.
    assert lane_run(case, secret=_ALT_SECRET).outputs["t"].column("c").to_pylist() == base
    # The draw is the scalar `derive_index(job_seed, namespace, encode_int(g), pool.size)`.
    want = expected_values(
        range(90), config=case.config, namespace="ns_faker", job_seed=_ctx_for(case).job_seed
    )
    valid = [v is not None for v in source.column("c").to_pylist()]
    assert base == [w if ok else None for w, ok in zip(want, valid, strict=True)]
    # job_seed governs the draw: a different job seed changes it.
    (tmp_path / "s2").mkdir()
    other = Case(tmp_path / "s2", source, [nd_faker()], seed=777)
    assert lane_run(other).outputs["t"].column("c").to_pylist() != base


@NEEDS_COMPANION
def test_3_positional_categorical_keys_on_mask_key_not_job_seed(tmp_path: Path) -> None:
    source = str_source(120, mod=7, null_at=lambda i: i % 13 == 4)
    case = Case(tmp_path, source, [cat_col()])
    base = lane_run(case).outputs["t"].column("c").to_pylist()
    # The secret feeds mask_key: the draw moves with it.
    assert lane_run(case, secret=_ALT_SECRET).outputs["t"].column("c").to_pylist() != base
    # The job seed does not feed a categorical draw.
    (tmp_path / "s2").mkdir()
    other = Case(tmp_path / "s2", source, [cat_col()], seed=777)
    assert lane_run(other).outputs["t"].column("c").to_pylist() == base


def test_3_run_operator_refuses_a_positional_faker_without_a_job_seed(tmp_path: Path) -> None:
    from decoy_engine.execution.physical._shadow_operators import run_operator
    from tests.native._chunked_faker_support import pool_of

    case = Case(tmp_path, str_source(4), [nd_faker()])
    inputs = _inputs(case)
    (node,) = [n for t in compile_physical_plan(inputs).tables for n in t.nodes]
    assert node.execution is not None
    ctx = ShadowContext(mask_key=b"\x09" * 32, job_seed=b"")
    with pytest.raises(AssertionError, match="no job_seed"):
        run_operator(
            pa.array(["a", "b"], type=pa.string()),
            binding=node.execution,
            ctx=ctx,
            evidence=OperatorCallEvidence(planned_operator=node.execution.operator_id),
            pool=pool_of(namespace="ns_faker", job_seed=inputs.plan.seed_envelope.job_seed),
            index_kernel=object(),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 4. Pool identity.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
def test_4_a_default_namespace_never_reuses_a_sibling_pool_keyed_by_the_same_string(
    tmp_path: Path,
) -> None:
    # `g` configures, as its own namespace, exactly the string `f`'s default selection
    # namespace resolves to. Pool identity stays on each column's CONFIGURED namespace, so `f`
    # builds its own pool instead of reusing `g`'s cached one.
    source = pa.table(
        {
            "g": pa.array(["x"] * 40, type=pa.string()),
            "f": pa.array(["x"] * 40, type=pa.string()),
        }
    )
    columns = [nd_faker("g", namespace=default_namespace("t", "f")), nd_faker("f", namespace=None)]
    parity(Case(tmp_path, source, columns), batch=9)


# ---------------------------------------------------------------------------
# 5. Zero-row evidence.
# ---------------------------------------------------------------------------


def _unit_node(strategy: str, operator: str) -> Any:
    return SimpleNamespace(
        node_id="n",
        strategy=strategy,
        columns=("c",),
        execution=SimpleNamespace(operator_id=operator),
    )


def _assemble_one(strategy: str, operator: str, evidence: OperatorCallEvidence) -> dict[str, Any]:
    from decoy_engine.execution._unified_slice_evidence import assemble_node_evidence

    record = StrategyTimingRecord(
        strategy_type=strategy, column="c", elapsed_ms=1.0, peak_memory_delta_kb=0
    )
    return assemble_node_evidence([_unit_node(strategy, operator)], {"n": evidence}, [record])["n"]


def _recorded(operator: str, *, compiled: bool, rows: int | None) -> OperatorCallEvidence:
    # `rows_seen` stays at its default when `rows` is None (a hand-built record).
    evidence = OperatorCallEvidence(
        planned_operator=operator,
        actual_operator=operator,
        executed=True,
        compiled_kernel_executed=compiled,
        batches_run=1,
    )
    if rows is not None:
        evidence.rows_seen = rows
    return evidence


def test_5_a_zero_row_faker_node_without_kernel_evidence_is_accepted() -> None:
    out = _assemble_one("faker", FAKER_OP, _recorded(FAKER_OP, compiled=False, rows=0))
    assert out["compiled_kernel_executed"] is False
    assert out["executed_backend"] == ARROW_PYTHON


@pytest.mark.parametrize("rows", [1, 5, None], ids=["one_row", "five_rows", "unrecorded"])
def test_5_a_non_empty_faker_node_without_kernel_evidence_still_raises(rows: int | None) -> None:
    with pytest.raises(UnifiedSliceInvariantError):
        _assemble_one("faker", FAKER_OP, _recorded(FAKER_OP, compiled=False, rows=rows))


@NEEDS_COMPANION
@pytest.mark.parametrize("build, operator", VARIANT_PARAMS)
def test_5_a_zero_row_table_completes_with_idle_evidence(
    tmp_path: Path, build: Callable[[], dict[str, Any]], operator: str
) -> None:
    source = pa.table({"c": pa.array([], type=pa.string())})
    on = lane_run(Case(tmp_path, source, [build()]))
    (entry,) = on.quality_metrics[QUALITY_METRICS_KEY]["nodes"].values()
    assert entry["operator"] == operator
    assert entry["executed"] is True
    assert entry["compiled_kernel_executed"] is False
    assert entry["executed_backend"] == ARROW_PYTHON
    assert entry["calls"] == 1


@NEEDS_COMPANION
def test_5_rows_seen_sums_across_the_batches_of_one_node(tmp_path: Path) -> None:
    from decoy_engine.execution.native._index_ext import load_compiled_index_kernel
    from decoy_engine.execution.physical._shadow_operators import run_operator

    case = Case(tmp_path, str_source(7), [cat_col()])
    (node,) = [n for t in compile_physical_plan(_inputs(case)).tables for n in t.nodes]
    binding = node.execution
    assert binding is not None
    ctx = _ctx_for(case)
    kernel = load_compiled_index_kernel()
    evidence = OperatorCallEvidence(planned_operator=binding.operator_id)
    whole = str_source(7).column("c").combine_chunks()
    parts: list[Any] = []
    for start, stop in ((0, 3), (3, 7)):
        out, _ = run_operator(
            whole.slice(start, stop - start),
            binding=binding,
            ctx=ctx,
            evidence=evidence,
            index_kernel=kernel,
            row_offset=start,
        )
        parts.extend(out.to_pylist())
    assert (evidence.rows_seen, evidence.batches_run) == (7, 2)
    one_pass = OperatorCallEvidence(planned_operator=binding.operator_id)
    out, _ = run_operator(
        whole, binding=binding, ctx=ctx, evidence=one_pass, index_kernel=kernel, row_offset=0
    )
    assert parts == out.to_pylist()


# Evidence of the deterministic operators on a zero-row table, recorded on engine main
# 3e8d259e before this change. The value is the node entry minus its operator name.
_DETERMINISTIC_ZERO_ROW: dict[str, tuple[Callable[[], dict[str, Any]], dict[str, Any]]] = {
    "det_faker": (
        lambda: nd_faker(deterministic=True),
        {
            "executed": True,
            "compiled_kernel_executed": True,
            "planned_backend": "rust_pool_select",
            "executed_backend": "rust_pool_select",
            "calls": 1,
        },
    ),
    "det_categorical": (
        lambda: cat_col(deterministic=True),
        {
            "executed": True,
            "compiled_kernel_executed": True,
            "planned_backend": "rust_companion",
            "executed_backend": "rust_companion",
            "calls": 1,
        },
    ),
    "hash": (
        lambda: {"name": "c", "strategy": "hash", "namespace": "ns_h"},
        {
            "executed": True,
            "compiled_kernel_executed": True,
            "planned_backend": "rust_companion",
            "executed_backend": "rust_companion",
            "calls": 1,
        },
    ),
}


@NEEDS_COMPANION
@pytest.mark.parametrize("name", sorted(_DETERMINISTIC_ZERO_ROW))
def test_5_deterministic_zero_row_evidence_is_unchanged(tmp_path: Path, name: str) -> None:
    build, expected = _DETERMINISTIC_ZERO_ROW[name]
    source = pa.table({"c": pa.array([], type=pa.string())})
    on = lane_run(Case(tmp_path, source, [build()]))
    (entry,) = on.quality_metrics[QUALITY_METRICS_KEY]["nodes"].values()
    assert {k: v for k, v in entry.items() if k != "operator"} == expected


# ---------------------------------------------------------------------------
# 6. Declines unchanged, with the same output as the lane-off run.
# ---------------------------------------------------------------------------


def _when(config: dict[str, Any]) -> None:
    config["tables"][0]["columns"][0]["when"] = "c == 'src_1'"


def _typed(typ: pa.DataType) -> pa.Table:
    if pa.types.is_integer(typ):
        return pa.table({"c": pa.array(list(range(30)), type=typ)})
    return pa.table({"c": pa.array([f"s{i % 5}" for i in range(30)], type=typ)})


_DECLINES: dict[str, tuple[pa.Table, list[dict[str, Any]], Callable[..., None] | None]] = {
    "faker_provider_outside_allowlist": (str_source(30), [nd_faker(provider="person_email")], None),
    "faker_no_pool_size": (str_source(30), [nd_faker(pool_size=None)], None),
    "faker_unique": (str_source(30), [nd_faker(cardinality_mode="unique")], None),
    "faker_when": (str_source(30), [nd_faker()], _when),
    "faker_vault": (str_source(30), [nd_faker(vault=True)], None),
    "faker_int_source": (_typed(pa.int64()), [nd_faker()], None),
    "faker_large_string_source": (_typed(pa.large_string()), [nd_faker()], None),
    "cat_from_profile": (
        str_source(30),
        [
            {
                "name": "c",
                "strategy": "categorical",
                "namespace": "n",
                "provider_config": {"from_profile": True},
            }
        ],
        None,
    ),
    "cat_no_namespace": (str_source(30), [cat_col(namespace=None)], None),
    "cat_numeric_categories": (str_source(30), [cat_col(categories=[1, 2, 3])], None),
    "cat_when": (str_source(30), [cat_col()], _when),
    "cat_vault": (str_source(30), [cat_col(vault=True)], None),
    "cat_int_source": (_typed(pa.int64()), [cat_col()], None),
    "cat_large_string_source": (_typed(pa.large_string()), [cat_col()], None),
    "det_faker_provider_outside_allowlist": (
        str_source(30),
        [nd_faker(provider="person_email", deterministic=True)],
        None,
    ),
    "det_categorical_numeric_categories": (
        str_source(30),
        [cat_col(categories=[1, 2, 3], deterministic=True)],
        None,
    ),
}


@pytest.mark.parametrize("name", sorted(_DECLINES))
def test_6_declines_to_the_oracle_unchanged(tmp_path: Path, name: str) -> None:
    source, columns, mutate = _DECLINES[name]
    case = Case(tmp_path, source, columns, mutate=mutate)
    try:
        off = case.run(lane=False)
    except Exception as exc:  # a config the oracle rejects must be rejected identically
        with pytest.raises(type(exc)) as info:
            case.run(lane=True)
        assert str(info.value) == str(exc)
        return
    on = case.run(lane=True)
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    assert on.outputs["t"].equals(off.outputs["t"], check_metadata=True)
    assert tuple(on.warnings) == tuple(off.warnings)


def _fk_config(
    tmp_path: Path, parent_columns: list[dict[str, Any]], child_columns: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, pa.Table]]:
    parent = pa.table(
        {
            "id": pa.array(["p1", "p2", "p3"], type=pa.string()),
            "c": pa.array(["alice", "bob", "carol"], type=pa.string()),
        }
    )
    child = pa.table(
        {
            "pid": pa.array(["p1", "p2", "p1"], type=pa.string()),
            "c": pa.array(["dave", "erin", "frank"], type=pa.string()),
        }
    )
    pq.write_table(parent, tmp_path / "parent.parquet")
    pq.write_table(child, tmp_path / "child.parquet")
    names = ("parent", "child")
    raw = {
        "version": 1,
        "global_settings": {"seed": 5},
        "sources": {
            n: {"type": "file", "format": "parquet", "path": str(tmp_path / f"{n}.parquet")}
            for n in names
        },
        "targets": {
            n: {"type": "file", "format": "parquet", "path": str(tmp_path / f"{n}.out.parquet")}
            for n in names
        },
        "tables": [
            {
                "name": "parent",
                "columns": [{"name": "id", "strategy": "passthrough"}, *parent_columns],
            },
            {
                "name": "child",
                "columns": [{"name": "pid", "strategy": "passthrough"}, *child_columns],
            },
        ],
        "relationships": [
            {
                "parent": {"table": "parent", "columns": ["id"]},
                "children": [{"table": "child", "columns": ["pid"]}],
                "orphan_policy": "preserve",
                "namespace": "fk_ns",
            }
        ],
    }
    return PipelineConfig.model_validate(raw).model_dump(), {"parent": parent, "child": child}


@pytest.mark.parametrize("variant", ["faker", "categorical"])
def test_6_an_fk_participating_table_declines(tmp_path: Path, variant: str) -> None:
    make = nd_faker if variant == "faker" else cat_col
    config, sources = _fk_config(tmp_path, [make("c")], [make("c")])
    off = run_pipeline(
        config,
        sources,
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=False,
    )
    on = run_pipeline(
        config,
        sources,
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
        unified_slice_enabled=True,
    )
    assert QUALITY_METRICS_KEY not in on.quality_metrics
    for table in ("parent", "child"):
        assert on.outputs[table].equals(off.outputs[table], check_metadata=True)


# Corpus for the verdict snapshot: the same columns, asked three questions that the unified
# binding must not change: the config-only eligibility query, the chunked compatibility veto
# and the chunked route's native admission.
_VERDICT_CORPUS: dict[str, Callable[[], list[dict[str, Any]]]] = {
    "cat_nd": lambda: [cat_col()],
    "cat_nd_weighted": lambda: [cat_col(weighted=True)],
    "cat_nd_no_namespace": lambda: [cat_col(namespace=None)],
    "cat_det": lambda: [cat_col(deterministic=True)],
    "cat_det_numeric": lambda: [cat_col(categories=[1, 2], deterministic=True)],
    "faker_nd": lambda: [nd_faker()],
    "faker_nd_no_ns": lambda: [nd_faker(namespace=None)],
    "faker_nd_no_pool": lambda: [nd_faker(pool_size=None)],
    "faker_nd_provider_outside": lambda: [nd_faker(provider="person_email")],
    "faker_det": lambda: [nd_faker(deterministic=True)],
    "faker_det_no_pool": lambda: [nd_faker(deterministic=True, pool_size=None)],
}

# (eligible, eligibility rejections, chunked veto code, chunked native admitted, chunked reroute
# reason), recorded on engine main 3e8d259e before this change.
_VERDICTS: dict[str, tuple[bool, tuple[str, ...], str | None, bool, str | None]] = {
    "cat_det": (True, (), None, True, None),
    "cat_det_numeric": (
        False,
        ("categorical_categories_not_all_string:c",),
        None,
        False,
        "fallback_policy_not_native:c:python_only",
    ),
    "cat_nd": (False, ("categorical_not_deterministic:c",), None, True, None),
    "cat_nd_no_namespace": (
        False,
        ("categorical_not_deterministic:c",),
        "categorical_nondeterministic_not_chunk_safe",
        False,
        "fallback_policy_not_native:c:python_only",
    ),
    "cat_nd_weighted": (False, ("categorical_not_deterministic:c",), None, True, None),
    "faker_det": (False, ("no_native_kernel:c:faker",), None, True, None),
    "faker_det_no_pool": (
        False,
        ("no_native_kernel:c:faker",),
        "chunked_strategy_conditions_unmet",
        False,
        "fallback_policy_not_native:c:python_only",
    ),
    "faker_nd": (False, ("no_native_kernel:c:faker",), None, True, None),
    "faker_nd_no_ns": (False, ("no_native_kernel:c:faker",), None, True, None),
    "faker_nd_no_pool": (
        False,
        ("no_native_kernel:c:faker",),
        "chunked_strategy_conditions_unmet",
        False,
        "fallback_policy_not_native:c:python_only",
    ),
    "faker_nd_provider_outside": (
        False,
        ("no_native_kernel:c:faker",),
        "chunked_strategy_conditions_unmet",
        False,
        "fallback_policy_not_native:c:python_only",
    ),
}


def _verdict(tmp_path: Path, columns: list[dict[str, Any]]) -> tuple[Any, ...]:
    from decoy_engine.execution._chunked import check_chunked_compatibility
    from decoy_engine.execution._chunked_profile import first_chunk_profile
    from decoy_engine.execution.native._dispatch import plan_native_route
    from decoy_engine.plan._errors import PlanCompileError
    from decoy_engine.providers_v2 import get_default_registry

    source = str_source(6)
    path = tmp_path / "x.parquet"
    pq.write_table(source, path)
    raw = {
        "version": 1,
        "global_settings": {"seed": 5},
        "sources": {"t": {"type": "file", "format": "parquet", "path": str(path)}},
        "targets": {
            "t": {"type": "file", "format": "parquet", "path": str(tmp_path / "o.parquet")}
        },
        "tables": [{"name": "t", "columns": columns}],
    }
    config = PipelineConfig.model_validate(raw).model_dump()
    eligibility = native_route_eligibility(config, table="t")
    registry = get_default_registry()
    try:
        check_chunked_compatibility(config, table="t", registry=registry)
        veto = None
    except PlanCompileError as exc:
        veto = exc.code
    preflight = plan_native_route(
        config,
        first_chunk_profile(source, table="t", engine_version="v"),
        table="t",
        engine_version="v",
        first_schema=source.schema,
        registry=registry,
    )
    return (
        eligibility.accepted,
        tuple(eligibility.rejections),
        veto,
        preflight.evidence.native_admitted,
        preflight.evidence.reroute_reason,
    )


@pytest.mark.parametrize("name", sorted(_VERDICT_CORPUS))
def test_6_route_verdicts_are_unchanged(tmp_path: Path, name: str) -> None:
    assert _verdict(tmp_path, _VERDICT_CORPUS[name]()) == _VERDICTS[name]


# ---------------------------------------------------------------------------
# 7. Default-on lane.
# ---------------------------------------------------------------------------


@NEEDS_COMPANION
@pytest.mark.parametrize("build, operator", VARIANT_PARAMS)
def test_8_default_flags_take_the_unified_lane(
    tmp_path: Path, build: Callable[[], dict[str, Any]], operator: str
) -> None:
    source = str_source(120, mod=8, null_at=lambda i: i % 7 == 2)
    case = Case(tmp_path, source, [build()])
    forced = case.run(lane=False)
    # No `unified_slice_enabled` argument: the shipped default.
    default = run_pipeline(
        case.config,
        {"t": case.source},
        engine_version=ENGINE_VERSION,
        key_provider=_key_provider(),
    )
    leaf = default.quality_metrics[QUALITY_METRICS_KEY]
    assert node_evidence(leaf, operator)["compiled_kernel_executed"] is True
    assert default.outputs["t"].equals(forced.outputs["t"], check_metadata=True)


# ---------------------------------------------------------------------------
# 8. Binding-boundary predicates, tested directly.
# ---------------------------------------------------------------------------


def _inputs(case: Case) -> Any:
    return capture_physical_plan_inputs(
        case.config, {"t": case.source}, engine_version=ENGINE_VERSION
    )


def _slice_of(inputs: Any, column: str = "c") -> Any:
    (node,) = [n for n in build_work_list(inputs.plan, inputs.registry) if n.columns == (column,)]
    return node.plan_slice


def _bindable(predicate: str, case: Case, *, replace: dict[str, Any] | None = None) -> bool:
    inputs = _inputs(case)
    plan_slice = _slice_of(inputs)
    if replace:
        plan_slice = dataclasses.replace(plan_slice, **replace)
    check: Callable[..., bool] = getattr(_shadow_bindings, predicate)
    return check(plan_slice=plan_slice, table="t", column="c", inputs=inputs)


def _cat_case(tmp_path: Path, column: dict[str, Any], **kw: Any) -> Case:
    mutate = kw.pop("mutate", None)
    source = kw.pop("source", str_source(20))
    return Case(tmp_path, source, [column], mutate=mutate)


_CAT_PRED = "positional_categorical_bindable"
_FAKER_PRED = "positional_faker_bindable"


def _fresh(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def test_8_the_positional_categorical_predicate_holds_for_the_admitted_shape(
    tmp_path: Path,
) -> None:
    assert _bindable(_CAT_PRED, _cat_case(_fresh(tmp_path, "u"), cat_col())) is True
    assert _bindable(_CAT_PRED, _cat_case(_fresh(tmp_path, "w"), cat_col(weighted=True))) is True


_CAT_CLAUSES: dict[str, Callable[[Path], tuple[Case, dict[str, Any] | None]]] = {
    "deterministic": lambda p: (_cat_case(p, cat_col(deterministic=True)), None),
    "config_not_preparable_numeric": lambda p: (
        _cat_case(p, cat_col(categories=[1, 2, 3])),
        None,
    ),
    "config_not_preparable_from_profile": lambda p: (
        _cat_case(
            p,
            {
                "name": "c",
                "strategy": "categorical",
                "namespace": "n",
                "provider_config": {"from_profile": True, "categories": ["a", "b"]},
            },
        ),
        None,
    ),
    "when": lambda p: (_cat_case(p, cat_col(), mutate=_when), None),
    "vault": lambda p: (_cat_case(p, cat_col(vault=True)), None),
    "int_source": lambda p: (_cat_case(p, cat_col(), source=_typed(pa.int64())), None),
    "large_string_source": lambda p: (
        _cat_case(p, cat_col(), source=_typed(pa.large_string())),
        None,
    ),
}


@pytest.mark.parametrize("clause", sorted(_CAT_CLAUSES))
def test_8_each_positional_categorical_condition_is_enforced(tmp_path: Path, clause: str) -> None:
    case, replace = _CAT_CLAUSES[clause](tmp_path)
    assert _bindable(_CAT_PRED, case, replace=replace) is False


def test_8_the_categorical_predicate_trusts_the_slice_not_the_config(tmp_path: Path) -> None:
    """The slice's own determinism decides, whatever the raw config said."""
    case = _cat_case(tmp_path, cat_col())
    assert _bindable(_CAT_PRED, case, replace={"deterministic": True}) is False


def test_8_the_positional_faker_predicate_holds_for_the_admitted_shape(tmp_path: Path) -> None:
    for index, namespace in enumerate(("ns_faker", None, "")):
        case = _cat_case(_fresh(tmp_path, f"n{index}"), nd_faker(namespace=namespace))
        assert _bindable(_FAKER_PRED, case) is True


_FAKER_CLAUSES: dict[str, Callable[[Path], tuple[Case, dict[str, Any] | None]]] = {
    "deterministic": lambda p: (_cat_case(p, nd_faker(deterministic=True)), None),
    "slice_deterministic": lambda p: (_cat_case(p, nd_faker()), {"deterministic": True}),
    "not_reuse": lambda p: (_cat_case(p, nd_faker(cardinality_mode="unique")), None),
    "slice_not_reuse": lambda p: (_cat_case(p, nd_faker()), {"cardinality_mode": "unique"}),
    "no_pool_size": lambda p: (_cat_case(p, nd_faker(pool_size=None)), None),
    "provider_outside_allowlist": lambda p: (_cat_case(p, nd_faker(provider="person_email")), None),
    "when": lambda p: (_cat_case(p, nd_faker(), mutate=_when), None),
    "vault": lambda p: (_cat_case(p, nd_faker(vault=True)), None),
    "int_source": lambda p: (_cat_case(p, nd_faker(), source=_typed(pa.int64())), None),
}


@pytest.mark.parametrize("clause", sorted(_FAKER_CLAUSES))
def test_8_each_positional_faker_condition_is_enforced(tmp_path: Path, clause: str) -> None:
    case, replace = _FAKER_CLAUSES[clause](tmp_path)
    assert _bindable(_FAKER_PRED, case, replace=replace) is False


@pytest.mark.parametrize("variant", ["faker", "categorical"])
def test_8_the_fk_exclusion_holds_on_the_multi_table_binding_path(
    tmp_path: Path, variant: str
) -> None:
    """The shadow compiler binds every table of a multi-table job, without production's
    single-table admission in front of it. FK parent and child columns stay unbound."""
    make = nd_faker if variant == "faker" else cat_col
    config, sources = _fk_config(tmp_path, [make("c")], [make("c")])
    inputs = capture_physical_plan_inputs(config, sources, engine_version=ENGINE_VERSION)
    plan = compile_physical_plan(inputs)
    strategy = "faker" if variant == "faker" else "categorical"
    nodes = [n for t in plan.tables for n in t.nodes if n.strategy == strategy]
    assert len(nodes) == 2
    assert all(n.execution is None for n in nodes)


@pytest.mark.parametrize("variant", ["faker", "categorical"])
def test_8_the_when_exclusion_holds_on_the_binding_path(tmp_path: Path, variant: str) -> None:
    make = nd_faker if variant == "faker" else cat_col
    case = Case(tmp_path, str_source(10), [make()], mutate=_when)
    plan = compile_physical_plan(_inputs(case))
    strategy = "faker" if variant == "faker" else "categorical"
    nodes = [n for t in plan.tables for n in t.nodes if n.strategy == strategy]
    assert nodes and all(n.execution is None for n in nodes)


@pytest.mark.parametrize("variant", ["faker", "categorical"])
def test_8_an_admitted_node_binds_with_the_variant_parameters(tmp_path: Path, variant: str) -> None:
    from decoy_engine.execution.native._operator_params import CategoricalParams, FakerParams

    column = nd_faker(namespace=None) if variant == "faker" else cat_col()
    plan = compile_physical_plan(_inputs(Case(tmp_path, str_source(10), [column])))
    (node,) = [n for t in plan.tables for n in t.nodes]
    binding = node.execution
    assert binding is not None
    if variant == "faker":
        assert isinstance(binding.params, FakerParams)
        assert binding.params.positional is True
        assert binding.params.namespace is None
        assert binding.params.selection_namespace == default_namespace("t", "c")
        assert binding.key_binding is not None
        assert binding.key_binding.key_source == "job_seed"
        assert binding.key_binding.namespace == default_namespace("t", "c")
        assert binding.pool_binding is not None
    else:
        assert isinstance(binding.params, CategoricalParams)
        assert binding.params.prepared.positional is True
        assert binding.key_binding is not None
        assert binding.key_binding.key_source == "mask_key"
        assert binding.key_binding.namespace == "ns_cat"
    assert binding.needs_index_kernel is True
