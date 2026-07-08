"""Property + pinned parity harness: out-of-core FK route vs the pandas oracle.

The out-of-core DuckDB FK route (`run_fk_out_of_core`) and the chunked-FK gate
must faithfully replicate the full-frame pandas path's FK semantics. Three
review rounds found the same class of gap: a route that admits a config it does
not reproduce byte-for-byte. This harness pins the whole class instead of
patching cases one at a time.

THE INVARIANT
-------------
For any FK config that `check_out_of_core_compatibility` ADMITS, the output of
`run_fk_out_of_core` must be VALUE-equal to `PandasExecutionAdapter.run` (the
oracle). "Value-equal" is `to_pydict()` equality after two documented,
representational normalizations (neither changes a logical value):

  * Arrow string/binary/list WIDTH drift (`string` vs `large_string`, etc.) --
    already invisible at `to_pydict()` (both yield the same Python scalar).
  * IEEE NaN vs null in float columns -- the oracle round-trips every frame
    through pandas, and `pa.array(..., from_pandas=True)` folds NaN to null;
    the out-of-core path never touches pandas, so a passed-through / preserved
    float NaN stays NaN. Both mean "missing"; we fold NaN -> None before
    comparing. See tests/parity/SEMANTIC_DIFFERENCES.md (v2 section, row v4).
    The MASKED-value NaN fix (a NaN FK key must mask to null, not to a hashed /
    redacted / "nan" token) is a real correctness fix and is NOT hidden by this
    fold: a wrong masked token is a string, which never folds to None.

When the oracle RAISES (e.g. `float_canonicalization_unsupported` for a float
key under hash, or `orphan_fk_violation` under FAIL with orphans), the
out-of-core route must fail closed the same way -- it must never produce output
where the oracle rejects. When the oracle SUCCEEDS but the out-of-core route
cannot type an admitted config's FK output up front (e.g. a decimal child key),
it fail-closes with `out_of_core_fk_key_dtype_unsupported`; that is an accepted
"reject rather than drift" outcome (never wrong output), recorded per case.

Note on "mixed-type object columns": a single pyarrow array cannot hold mixed
Python scalar types (`pa.array([1, "a"])` raises), and both routes take
`pa.Table` sources, so a truly mixed column is not representable at this
boundary. The kernel's raw-list fallback for mixed pandas columns
(`_array_to_pylist`) is exercised by the pandas-path unit tests, not here.
"""

from __future__ import annotations

import math
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.execution._errors import ExecutionError
from decoy_engine.execution._runner import build_work_list, order_work
from decoy_engine.execution.out_of_core import run_fk_out_of_core
from decoy_engine.execution.out_of_core._compat import check_out_of_core_compatibility
from decoy_engine.plan._types import ColumnSeed, SeedEnvelope, TableSeed
from decoy_engine.providers_v2 import get_default_registry
from decoy_engine.relationships._graph import OrphanPolicy, RelationshipEdge, RelationshipGraph
from decoy_engine.relationships._namespace import NamespaceRegistry

_REG = get_default_registry()
_NS = NamespaceRegistry(bindings=())
_JOB_SEED = b"\x11" * 8

# Codes the out-of-core route raises as a clean, fail-closed rejection of an
# admitted-but-unreproducible config: no wrong output is ever emitted.
_FAIL_CLOSED_CODES = frozenset({"out_of_core_fk_key_dtype_unsupported"})


# ---------------------------------------------------------------------------
# Plan / graph / source construction
# ---------------------------------------------------------------------------


def _seed(
    strategy: str,
    *,
    namespace: str | None = None,
    provider_config: tuple[tuple[str, Any], ...] = (),
) -> ColumnSeed:
    return ColumnSeed(
        namespace=namespace,
        strategy=strategy,
        provider=strategy,
        backend_type="faker",
        backend_version="v",
        cardinality_mode="reuse",
        deterministic=namespace is not None,
        provider_config=provider_config,
        coherent_with=(),
    )


def _plan(per_table: tuple[tuple[str, TableSeed], ...]) -> Any:
    return SimpleNamespace(seed_envelope=SeedEnvelope(job_seed=_JOB_SEED, per_table=per_table))


def _run_oracle(plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph):
    return PandasExecutionAdapter().run(
        plan, sources, registry=_REG, relationship_graph=graph, namespace_registry=_NS
    )


def _run_ooc(plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph):
    return run_fk_out_of_core(plan, sources, registry=_REG, relationship_graph=graph)


def _gate_admits(plan: Any, graph: RelationshipGraph) -> bool:
    work = order_work(build_work_list(plan, _REG), graph)
    return check_out_of_core_compatibility(plan, work, graph).accepted


# ---------------------------------------------------------------------------
# Comparison (value-equal, with the two documented normalizations)
# ---------------------------------------------------------------------------


def _fold(value: object) -> object:
    """Fold IEEE NaN -> None so a preserved float NaN compares equal to null."""
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _comparable(table: pa.Table) -> dict[str, list[object]]:
    """Column -> Python values, NaN folded to None.

    `to_pydict()` already collapses Arrow width drift (string vs large_string,
    etc.) to the same Python scalar, and Python numeric equality collapses the
    documented int64 vs float64 FK narrowing (1 == 1.0). NaN is the one missing
    normalization, handled here.
    """
    return {name: [_fold(v) for v in col] for name, col in table.to_pydict().items()}


def _assert_value_equal(oracle: pa.Table, ooc: pa.Table, label: str) -> None:
    got = _comparable(ooc)
    want = _comparable(oracle)
    assert set(got) == set(want), f"{label}: column mismatch {set(got)} vs {set(want)}"
    for name in want:
        assert got[name] == want[name], (
            f"{label}: column {name!r} diverges\n oracle={want[name]}\n    ooc={got[name]}"
        )


def _assert_parity_or_faithful_rejection(
    plan: Any, sources: dict[str, pa.Table], graph: RelationshipGraph, label: str
) -> str:
    """Run both routes and enforce the invariant. Returns an outcome tag."""
    oracle_exc: BaseException | None = None
    oracle_res = None
    try:
        oracle_res = _run_oracle(plan, sources, graph)
    except BaseException as exc:
        oracle_exc = exc

    if not _gate_admits(plan, graph):
        # A clean gate rejection is always allowed: the route never runs, so it
        # cannot emit wrong output. (The oracle may still succeed; full-frame
        # handles what out-of-core declines.)
        return "gate-rejected"

    ooc_exc: BaseException | None = None
    ooc_res = None
    try:
        ooc_res = _run_ooc(plan, sources, graph)
    except BaseException as exc:
        ooc_exc = exc

    if oracle_exc is not None:
        # The oracle rejected: the route MUST also fail closed, never produce
        # output where the oracle refuses to.
        assert ooc_exc is not None, (
            f"{label}: oracle raised {type(oracle_exc).__name__}"
            f"({getattr(oracle_exc, 'code', '?')}) but out-of-core produced output"
        )
        o_code = getattr(oracle_exc, "code", None)
        c_code = getattr(ooc_exc, "code", None)
        if o_code is not None and c_code is not None:
            assert o_code == c_code, (
                f"{label}: both raised but codes differ: oracle {o_code} vs ooc {c_code}"
            )
        return "both-raised"

    # Oracle succeeded.
    if ooc_exc is not None:
        code = getattr(ooc_exc, "code", None)
        assert code in _FAIL_CLOSED_CODES, (
            f"{label}: oracle succeeded but out-of-core raised {type(ooc_exc).__name__}({code})"
        )
        return "ooc-fail-closed"

    assert ooc_res is not None and oracle_res is not None
    for table in oracle_res.outputs:
        _assert_value_equal(oracle_res.outputs[table], ooc_res.outputs[table], f"{label}:{table}")
    return "parity"


# ---------------------------------------------------------------------------
# Value / array builders per dtype kind
# ---------------------------------------------------------------------------

_ARROW_TYPE = {
    "int": pa.int64(),
    "float": pa.float64(),
    "str": pa.string(),
    "decimal": pa.decimal128(20, 4),
    "bool": pa.bool_(),
}


def _key_value(kind: str, i: int) -> object:
    if kind == "int":
        return i
    if kind == "float":
        return float(i)
    if kind == "decimal":
        return Decimal(i)
    if kind == "bool":
        # Only 2 distinct bool values exist; callers cap row counts to 2 for a
        # "bool" PARENT kind so parent keys stay unique (see `_single_edge_case`).
        return bool(i % 2)
    return f"k{i}"


def _orphan_value(kind: str, i: int) -> object:
    if kind == "int":
        return 10_000 + i
    if kind == "float":
        return float(10_000 + i)
    if kind == "decimal":
        return Decimal(10_000 + i)
    if kind == "bool":
        # No 3rd bool value exists to serve as a guaranteed non-match, so the
        # generator disallows orphans whenever "bool" is the parent or child
        # kind (see `bool_involved` in `_single_edge_case`); this branch
        # existing only to fail loudly, not silently produce a wrong type
        # (a string into a bool-typed array), if that invariant is ever broken.
        raise AssertionError("bool FK keys have no orphan value; disallow via allow_orphan")
    return f"orphan{i}"


def _seed_for(strategy: str, namespace: str) -> ColumnSeed:
    if strategy == "hash":
        return _seed("hash", namespace=namespace)
    if strategy == "truncate":
        return _seed("truncate", provider_config=(("length", 3),))
    if strategy == "redact":
        return _seed("redact")
    return _seed("passthrough")


# ---------------------------------------------------------------------------
# Single-edge scenario builder
# ---------------------------------------------------------------------------


def _build_single_edge(
    *,
    parent_kind: str,
    child_kind: str,
    strategy: str,
    policy: OrphanPolicy,
    parent_rows: int,
    child_refs: list[int | None],  # index into parent, or -1 orphan, or None null
    parent_nan_row: int | None,
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    namespace = "ns_p"
    pseed = _seed_for(strategy, namespace)

    parent_keys: list[object] = [_key_value(parent_kind, i) for i in range(parent_rows)]
    if parent_nan_row is not None and parent_kind == "float":
        parent_keys[parent_nan_row] = float("nan")
    parent_tbl = pa.table(
        {
            "pk": pa.array(parent_keys, type=_ARROW_TYPE[parent_kind]),
            "payload": pa.array([f"pp{i}" for i in range(parent_rows)], type=pa.string()),
        }
    )

    child_keys: list[object] = []
    orphan_ct = 0
    for ref in child_refs:
        if ref is None:
            child_keys.append(None)
        elif ref == -1:
            child_keys.append(_orphan_value(child_kind, orphan_ct))
            orphan_ct += 1
        else:
            child_keys.append(_key_value(child_kind, ref))
    child_tbl = pa.table(
        {
            "fk": pa.array(child_keys, type=_ARROW_TYPE[child_kind]),
            "payload": pa.array([f"cp{i}" for i in range(len(child_refs))], type=pa.string()),
        }
    )

    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", pseed),), per_group=())),
            ("child", TableSeed(per_column=(("fk", pseed),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace=namespace,
                orphan_policy=policy,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent_tbl, "child": child_tbl}, graph


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@st.composite
def _single_edge_case(draw: st.DrawFn) -> tuple[Any, dict[str, pa.Table], RelationshipGraph, str]:
    strategy = draw(st.sampled_from(["hash", "redact", "truncate", "passthrough"]))
    # A float PARENT key under hash canonicalizes to a hard error on both paths
    # (float_canonicalization_unsupported); allow it (both-raised is asserted),
    # but bias toward parent kinds that yield real parity so most examples
    # exercise value equality rather than the shared raise. "bool" canonicalizes
    # fine under hash (derive-input canonicalization has a dedicated bool byte
    # rule), so it is offered under every strategy.
    parent_kinds = ["int", "str", "bool"] if strategy == "hash" else ["int", "float", "str", "bool"]
    parent_kind = draw(st.sampled_from(parent_kinds))
    # Child kind: same family, the normalize-equal int<->float split, or (Codex
    # round-4 Finding A) the normalize-equal bool<->int split (True/False vs 1/0).
    if parent_kind == "int":
        child_kind = draw(st.sampled_from(["int", "float", "bool"]))
    elif parent_kind == "float":
        child_kind = draw(st.sampled_from(["float", "int"]))
    elif parent_kind == "bool":
        child_kind = draw(st.sampled_from(["bool", "int"]))
    else:
        child_kind = "str"
    policy = draw(st.sampled_from(list(OrphanPolicy)))

    parent_rows = draw(st.integers(min_value=2, max_value=8))
    if parent_kind == "bool":
        # Only 2 distinct bool values exist; more rows would duplicate parent
        # keys, a different (untested-here) scenario from the dtype-collision
        # this case targets.
        parent_rows = 2
    child_rows = draw(st.integers(min_value=1, max_value=14))

    # A NaN in a masked float PARENT key row (unreferenceable) exercises the
    # kernel NaN-as-missing fix. Only meaningful for float parents under a
    # non-hash strategy (float+hash raises before masking).
    parent_nan_row: int | None = None
    if parent_kind == "float" and strategy != "hash" and draw(st.booleans()):
        parent_nan_row = draw(st.integers(min_value=0, max_value=parent_rows - 1))

    bool_involved = parent_kind == "bool" or child_kind == "bool"
    allow_null = child_kind in ("str", "float", "int", "bool")
    # This generator's bool parent is always the 2-row, both-values-present
    # shape (`_key_value("bool", i) = bool(i % 2)` for i in 0, 1), so no 3rd
    # bool value exists here to serve as a guaranteed non-match: an orphan is
    # unrepresentable whenever "bool" is on either side of THIS shape's edge.
    # The single-VALUE bool-parent shape (Codex round-5 Finding B: a parent
    # covering only one of True/False, so the OTHER value is a genuine
    # orphan) is a structurally different parent construction than this
    # generator's uniform `_key_value`/`_orphan_value` protocol supports, and
    # is covered instead by dedicated pinned regressions:
    # `test_bool_orphan_publishes_int_not_bool` and
    # `test_bool_orphan_and_match_cross_batch_casts_to_int`
    # (tests/unit/execution/test_out_of_core_runner_streaming.py).
    allow_orphan = (policy is not OrphanPolicy.FAIL or draw(st.booleans())) and not bool_involved
    choices = list(range(parent_rows))
    child_refs: list[int | None] = []
    for _ in range(child_rows):
        r = draw(st.integers(min_value=0, max_value=3))
        if r == 0 and allow_null:
            child_refs.append(None)
        elif r == 1 and allow_orphan:
            child_refs.append(-1)
        else:
            child_refs.append(draw(st.sampled_from(choices)))

    plan, sources, graph = _build_single_edge(
        parent_kind=parent_kind,
        child_kind=child_kind,
        strategy=strategy,
        policy=policy,
        parent_rows=parent_rows,
        child_refs=child_refs,
        parent_nan_row=parent_nan_row,
    )
    label = (
        f"{strategy}/{parent_kind}->{child_kind}/{policy.name}"
        f"/nan={parent_nan_row}/refs={child_refs}"
    )
    return plan, sources, graph, label


@settings(
    max_examples=250,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_single_edge_case())
def test_single_edge_fk_parity(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    _assert_parity_or_faithful_rejection(plan, sources, graph, label)


# ---------------------------------------------------------------------------
# Chained parent -> child -> grandchild property test
# ---------------------------------------------------------------------------


def _build_chain(
    *, strategy: str, policy: OrphanPolicy, child_refs: list[int | None], gc_refs: list[int | None]
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    ns_p, ns_c = "ns_p", "ns_c"
    pseed = _seed_for(strategy, ns_p)
    cseed = _seed_for(strategy, ns_c)
    n_parent = 4
    n_child = len(child_refs)

    parent = pa.table({"pk": pa.array([f"p{i}" for i in range(n_parent)], type=pa.string())})
    child_fk = [None if r is None else (f"orphan{r}" if r == -1 else f"p{r}") for r in child_refs]
    child = pa.table(
        {
            "ck": pa.array([f"c{i}" for i in range(n_child)], type=pa.string()),
            "pfk": pa.array(child_fk, type=pa.string()),
        }
    )
    gc_fk = [None if r is None else (f"orphanc{r}" if r == -1 else f"c{r}") for r in gc_refs]
    grandchild = pa.table({"cfk": pa.array(gc_fk, type=pa.string())})

    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", pseed),), per_group=())),
            ("child", TableSeed(per_column=(("ck", cseed), ("pfk", pseed)), per_group=())),
            ("grandchild", TableSeed(per_column=(("cfk", cseed),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("pfk",),
                namespace=ns_p,
                orphan_policy=policy,
            ),
            RelationshipEdge(
                parent_table="child",
                parent_columns=("ck",),
                child_table="grandchild",
                child_columns=("cfk",),
                namespace=ns_c,
                orphan_policy=policy,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child": child, "grandchild": grandchild}, graph


@st.composite
def _chain_case(draw: st.DrawFn) -> tuple[Any, dict[str, pa.Table], RelationshipGraph, str]:
    strategy = draw(st.sampled_from(["hash", "redact", "truncate", "passthrough"]))
    policy = draw(st.sampled_from(list(OrphanPolicy)))
    allow_orphan = policy is not OrphanPolicy.FAIL or draw(st.booleans())

    def _refs(n: int, hi: int) -> list[int | None]:
        out: list[int | None] = []
        for _ in range(n):
            r = draw(st.integers(min_value=0, max_value=3))
            if r == 0:
                out.append(None)
            elif r == 1 and allow_orphan:
                out.append(-1)
            else:
                out.append(draw(st.integers(min_value=0, max_value=hi)))
        return out

    child_refs = _refs(draw(st.integers(2, 8)), 3)
    gc_refs = _refs(draw(st.integers(2, 8)), max(0, len(child_refs) - 1))
    plan, sources, graph = _build_chain(
        strategy=strategy, policy=policy, child_refs=child_refs, gc_refs=gc_refs
    )
    return plan, sources, graph, f"chain/{strategy}/{policy.name}"


@settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_chain_case())
def test_chain_fk_parity(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    _assert_parity_or_faithful_rejection(plan, sources, graph, label)


# ---------------------------------------------------------------------------
# Deeper chain (4-5 levels) property test -- widening beyond the fixed
# parent -> child -> grandchild depth above.
# ---------------------------------------------------------------------------


def _build_chain_n(
    *, strategy: str, policy: OrphanPolicy, depth: int, refs: list[list[int | None]]
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    """A `depth`-level parent -> child -> ... -> leaf FK chain, generalizing
    `_build_chain` past 3 levels. `refs[i]` is the child-row-reference list for
    level `i + 1` against level `i` (same -1/None/index convention as
    `_build_chain`); `len(refs) == depth - 1`.
    """
    assert depth >= 2
    tables = [f"t{i}" for i in range(depth)]
    namespaces = [f"ns{i}" for i in range(depth)]
    seeds = [_seed_for(strategy, namespaces[i]) for i in range(depth)]

    n0 = 4
    frames: dict[str, pa.Table] = {
        tables[0]: pa.table({"k": pa.array([f"t0_{j}" for j in range(n0)], type=pa.string())})
    }
    per_table: list[tuple[str, TableSeed]] = [
        (tables[0], TableSeed(per_column=(("k", seeds[0]),), per_group=()))
    ]
    edges: list[RelationshipEdge] = []

    for i in range(1, depth):
        this_refs = refs[i - 1]
        fk_vals: list[str | None] = []
        orphan_ct = 0
        for r in this_refs:
            if r is None:
                fk_vals.append(None)
            elif r == -1:
                fk_vals.append(f"orphan{tables[i]}_{orphan_ct}")
                orphan_ct += 1
            else:
                fk_vals.append(f"{tables[i - 1]}_{r}")
        frames[tables[i]] = pa.table(
            {
                "k": pa.array(
                    [f"{tables[i]}_{j}" for j in range(len(this_refs))], type=pa.string()
                ),
                "fk": pa.array(fk_vals, type=pa.string()),
            }
        )
        per_table.append(
            (
                tables[i],
                TableSeed(per_column=(("k", seeds[i]), ("fk", seeds[i - 1])), per_group=()),
            )
        )
        edges.append(
            RelationshipEdge(
                parent_table=tables[i - 1],
                parent_columns=("k",),
                child_table=tables[i],
                child_columns=("fk",),
                namespace=namespaces[i - 1],
                orphan_policy=policy,
            )
        )

    plan = _plan(tuple(per_table))
    graph = RelationshipGraph(edges=tuple(edges), ordering=())
    return plan, frames, graph


@st.composite
def _deep_chain_case(draw: st.DrawFn) -> tuple[Any, dict[str, pa.Table], RelationshipGraph, str]:
    strategy = draw(st.sampled_from(["hash", "redact", "truncate", "passthrough"]))
    policy = draw(st.sampled_from(list(OrphanPolicy)))
    allow_orphan = policy is not OrphanPolicy.FAIL or draw(st.booleans())
    depth = draw(st.integers(min_value=4, max_value=5))  # deeper than the 3-level chain above

    def _refs(n: int, hi: int) -> list[int | None]:
        out: list[int | None] = []
        for _ in range(n):
            r = draw(st.integers(min_value=0, max_value=3))
            if r == 0:
                out.append(None)
            elif r == 1 and allow_orphan:
                out.append(-1)
            else:
                out.append(draw(st.integers(min_value=0, max_value=hi)))
        return out

    refs: list[list[int | None]] = []
    prev_n = 4  # root row count (n0 in _build_chain_n)
    for _ in range(depth - 1):
        n = draw(st.integers(2, 5))  # keep per-example sizes small
        refs.append(_refs(n, max(0, prev_n - 1)))
        prev_n = n

    plan, sources, graph = _build_chain_n(strategy=strategy, policy=policy, depth=depth, refs=refs)
    return plan, sources, graph, f"deepchain/{strategy}/{policy.name}/depth={depth}"


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_deep_chain_case())
def test_deep_chain_fk_parity(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    _assert_parity_or_faithful_rejection(plan, sources, graph, label)


# ---------------------------------------------------------------------------
# Fan-out (multiple FK edges to one parent) property test
# ---------------------------------------------------------------------------


def _build_fanout(
    *,
    strategy: str,
    policy: OrphanPolicy,
    parent_rows: int,
    refs_a: list[int | None],
    refs_b: list[int | None],
) -> tuple[Any, dict[str, pa.Table], RelationshipGraph]:
    """One parent, two DISTINCT child tables each with their own FK edge to it
    (not the `out_of_core_multi_parent_child_unsupported` shape, which is two
    parents for one child)."""
    ns = "ns_fanout"
    seed = _seed_for(strategy, ns)
    parent = pa.table({"pk": pa.array([f"p{i}" for i in range(parent_rows)], type=pa.string())})

    def _child_frame(name: str, refs: list[int | None]) -> pa.Table:
        vals: list[str | None] = []
        orphan_ct = 0
        for r in refs:
            if r is None:
                vals.append(None)
            elif r == -1:
                vals.append(f"orphan_{name}_{orphan_ct}")
                orphan_ct += 1
            else:
                vals.append(f"p{r}")
        return pa.table({"fk": pa.array(vals, type=pa.string())})

    child_a = _child_frame("a", refs_a)
    child_b = _child_frame("b", refs_b)

    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", seed),), per_group=())),
            ("child_a", TableSeed(per_column=(("fk", seed),), per_group=())),
            ("child_b", TableSeed(per_column=(("fk", seed),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child_a",
                child_columns=("fk",),
                namespace=ns,
                orphan_policy=policy,
            ),
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child_b",
                child_columns=("fk",),
                namespace=ns,
                orphan_policy=policy,
            ),
        ),
        ordering=(),
    )
    return plan, {"parent": parent, "child_a": child_a, "child_b": child_b}, graph


@st.composite
def _fanout_case(draw: st.DrawFn) -> tuple[Any, dict[str, pa.Table], RelationshipGraph, str]:
    strategy = draw(st.sampled_from(["hash", "redact", "truncate", "passthrough"]))
    policy = draw(st.sampled_from(list(OrphanPolicy)))
    allow_orphan = policy is not OrphanPolicy.FAIL or draw(st.booleans())
    parent_rows = draw(st.integers(min_value=2, max_value=6))

    def _refs(n: int) -> list[int | None]:
        out: list[int | None] = []
        for _ in range(n):
            r = draw(st.integers(min_value=0, max_value=3))
            if r == 0:
                out.append(None)
            elif r == 1 and allow_orphan:
                out.append(-1)
            else:
                out.append(draw(st.integers(min_value=0, max_value=parent_rows - 1)))
        return out

    refs_a = _refs(draw(st.integers(1, 6)))
    refs_b = _refs(draw(st.integers(1, 6)))
    plan, sources, graph = _build_fanout(
        strategy=strategy, policy=policy, parent_rows=parent_rows, refs_a=refs_a, refs_b=refs_b
    )
    return plan, sources, graph, f"fanout/{strategy}/{policy.name}"


@settings(
    max_examples=80,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(_fanout_case())
def test_fanout_multi_child_fk_parity(
    case: tuple[Any, dict[str, pa.Table], RelationshipGraph, str],
) -> None:
    plan, sources, graph, label = case
    _assert_parity_or_faithful_rejection(plan, sources, graph, label)


# ---------------------------------------------------------------------------
# Pinned regression tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["redact", "truncate", "passthrough"])
def test_nan_parent_key_preserved_as_missing(strategy: str) -> None:
    """A NaN in a masked float PARENT key row must mask to null, exactly like the
    full-frame path (pa.array(from_pandas=True) folds NaN to null before the
    kernel). Before the kernel fix, redact -> 'REDACTED', truncate -> 'nan',
    which this pins against. The NaN row is unreferenceable, so it never affects
    the join; only the parent's own masked output must read null there.
    """
    # The NaN sits on the LAST parent row, which no child references, so it is
    # unreferenceable and never turns a child into a type-mixing orphan; the
    # children match rows 0..2 exactly.
    # FAIL with only matched (and one null) children keeps the FK output type to
    # the single masked type, so a redact/truncate string output does not clash
    # with the float child key type (which PRESERVE/WARN would add as a
    # candidate and fail-close on). No non-null child is an orphan, so FAIL does
    # not raise.
    plan, sources, graph = _build_single_edge(
        parent_kind="float",
        child_kind="float",
        strategy=strategy,
        policy=OrphanPolicy.FAIL,
        parent_rows=4,
        child_refs=[0, 1, 2, None],
        parent_nan_row=3,
    )
    oracle = _run_oracle(plan, sources, graph)
    ooc = _run_ooc(plan, sources, graph)
    parent_pk = ooc.outputs["parent"].column("pk").to_pylist()
    if strategy != "passthrough":
        # redact/truncate route the NaN through the kernel, which must emit null
        # (not 'REDACTED' / 'nan'). passthrough keeps the raw NaN, which the
        # value-parity check folds to null (the pandas-round-trip artifact).
        assert parent_pk[3] is None, (
            f"{strategy}: NaN parent key masked to {parent_pk[3]!r}, not null"
        )
    _assert_value_equal(oracle.outputs["parent"], ooc.outputs["parent"], f"nan-{strategy}-parent")
    _assert_value_equal(oracle.outputs["child"], ooc.outputs["child"], f"nan-{strategy}-child")


@pytest.mark.parametrize("child_kind", ["int", "float", "decimal"])
def test_int_float_decimal_normalize_equal_match(child_kind: str) -> None:
    """int 1, float 1.0, and Decimal('1') are one logical FK key: a child of any
    of the three must resolve to the parent's masked key. FAIL/no-orphans keeps
    the decimal child inside the route's typeable surface (a decimal child under
    PRESERVE/WARN/REMAP fail-closes on the FK output type instead)."""
    plan, sources, graph = _build_single_edge(
        parent_kind="int",
        child_kind=child_kind,
        strategy="hash",
        policy=OrphanPolicy.FAIL,
        parent_rows=3,
        child_refs=[0, 1, 2, 0],
        parent_nan_row=None,
    )
    oracle = _run_oracle(plan, sources, graph)
    ooc = _run_ooc(plan, sources, graph)
    parent_masked = oracle.outputs["parent"].column("pk").to_pylist()
    child_masked = ooc.outputs["child"].column("fk").to_pylist()
    # Every child row matched a parent, so its masked FK is the parent's token.
    assert child_masked == [parent_masked[i] for i in (0, 1, 2, 0)]
    _assert_value_equal(oracle.outputs["child"], ooc.outputs["child"], f"normeq-{child_kind}")


@pytest.mark.parametrize("strategy", ["hash", "redact", "truncate"])
def test_decimal_fractional_scale_mismatch_matches_oracle(strategy: str) -> None:
    """Codex round-5 Finding A: a parent Decimal('1.20') (decimal128 scale 2)
    and a child Decimal('1.2') (decimal128 scale 1) are ONE logical FK key --
    Python's Decimal equality/hash fold exponent-only (trailing-zero)
    differences, so the pandas oracle's plain-dict parent_map already
    resolves them as a match. Before the fix, `fk_join_key` encoded the raw
    Decimal's `repr()`, which differs by scale, so the out-of-core route
    minted different DuckDB join tokens for the two sides and misreported
    every matching child as an orphan (FAIL raised `orphan_fk_violation`
    where the oracle succeeds).

    hash/redact/truncate (not passthrough) are required to reach the bug: a
    passthrough parent key stays decimal-typed end to end, which the
    fixed-schema output-type resolver (`_batch_join.py::_resolve_output_types`)
    already rejects fail-closed for ANY orphan policy (a decimal masked type
    has no fixed Python round-trip image) -- a real, unrelated, pre-existing
    fail-closed path, not this bug. hash/redact/truncate mask the parent
    DECIMAL key to a STRING, sidestepping that reject, so FAIL/no-orphans
    reaches the actual join-token comparison this test pins.
    """
    ns = "ns_dec_scale"
    seed = _seed_for(strategy, ns)
    parent = pa.table(
        {"pk": pa.array([Decimal("1.20"), Decimal("2.20")], type=pa.decimal128(20, 2))}
    )
    child = pa.table(
        {
            "fk": pa.array(
                [Decimal("1.2"), Decimal("2.2"), Decimal("1.2")], type=pa.decimal128(20, 1)
            )
        }
    )
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", seed),), per_group=())),
            ("child", TableSeed(per_column=(("fk", seed),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace=ns,
                orphan_policy=OrphanPolicy.FAIL,
            ),
        ),
        ordering=(),
    )
    sources = {"parent": parent, "child": child}
    outcome = _assert_parity_or_faithful_rejection(
        plan, sources, graph, f"decimal-scale-mismatch-{strategy}"
    )
    assert outcome == "parity"


def test_decimal_fractional_scale_mismatch_still_detects_real_orphans() -> None:
    """The scale-fold fix must not paper over a GENUINE mismatch: a child
    Decimal('1.30') (scale 2) that equals no parent key is still a real
    orphan on both routes -- both must raise the same `orphan_fk_violation`
    under FAIL, never a false match."""
    ns = "ns_dec_scale_orphan"
    seed = _seed_for("hash", ns)
    parent = pa.table(
        {"pk": pa.array([Decimal("1.20"), Decimal("2.20")], type=pa.decimal128(20, 2))}
    )
    child = pa.table({"fk": pa.array([Decimal("1.2"), Decimal("1.3")], type=pa.decimal128(20, 1))})
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", seed),), per_group=())),
            ("child", TableSeed(per_column=(("fk", seed),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace=ns,
                orphan_policy=OrphanPolicy.FAIL,
            ),
        ),
        ordering=(),
    )
    sources = {"parent": parent, "child": child}
    outcome = _assert_parity_or_faithful_rejection(
        plan, sources, graph, "decimal-scale-real-orphan"
    )
    assert outcome == "both-raised"


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN])
def test_large_whole_float_orphan_int_parent_matches_oracle(policy: OrphanPolicy) -> None:
    """int/float sink-parity boundary (SC1 round-6 P2): an int PARENT
    (passthrough int64) with a float CHILD whose orphan keys are whole numbers
    beyond +/-2**53. `fk_key_value` normalizes each whole float to int, so the
    FK output column is all-integral -- the pandas oracle types it int64 and
    keeps the exact value. The out-of-core route's FK output type is float64
    (a fractional float was possible), and before the fix `chunk.cast(float64)`
    used pyarrow's SAFE cast, which rejects every integer beyond +/-2**53 even
    when it IS exactly representable, crashing (ArrowInvalid) on a config the
    oracle accepts. The guarded cast now takes the lossless unsafe cast for a
    round-trip-representable integer, so both routes agree on the value
    (9007199254740994 == 9007199254740994.0).
    """
    ns = "ns_big_whole_float"
    seed = _seed("passthrough")
    parent = pa.table({"pk": pa.array([1, 2], type=pa.int64())})
    child = pa.table(
        {
            "fk": pa.array(
                # 9007199254740994.0 = 2**53 + 2: an even integer beyond the safe
                # cast range but exactly representable as a double; an orphan
                # (no int parent equals it) that fk_key_value folds to int.
                [1.0, 9007199254740994.0, 8.0],
                type=pa.float64(),
            )
        }
    )
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", seed),), per_group=())),
            ("child", TableSeed(per_column=(("fk", seed),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace=ns,
                orphan_policy=policy,
            ),
        ),
        ordering=(),
    )
    sources = {"parent": parent, "child": child}
    outcome = _assert_parity_or_faithful_rejection(
        plan, sources, graph, f"big-whole-float-{policy.name}"
    )
    assert outcome == "parity"


@pytest.mark.parametrize("policy", [OrphanPolicy.PRESERVE, OrphanPolicy.WARN])
def test_non_representable_int_orphan_float_parent_fails_closed(policy: OrphanPolicy) -> None:
    """Companion to the whole-float boundary: a float PARENT (passthrough
    float64) with an int CHILD whose orphan keys are odd integers beyond
    +/-2**53 -- NOT exactly representable as a double. With every child row an
    orphan, the pandas oracle types the column int64 and keeps the exact value,
    but the out-of-core FK output type is float64 (the parent side is float), so
    rounding the key into float would drift from the oracle's exact int. The
    route cannot narrow a streamed float column after the fact, so it must fail
    closed with `out_of_core_fk_key_dtype_unsupported` (reject rather than
    publish a rounded key), never crash and never emit the drifted value.
    """
    ns = "ns_nonrep_int"
    seed = _seed("passthrough")
    parent = pa.table({"pk": pa.array([1.0, 2.0], type=pa.float64())})
    child = pa.table({"fk": pa.array([9007199254740993, 9007199254740995], type=pa.int64())})
    plan = _plan(
        (
            ("parent", TableSeed(per_column=(("pk", seed),), per_group=())),
            ("child", TableSeed(per_column=(("fk", seed),), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("pk",),
                child_table="child",
                child_columns=("fk",),
                namespace=ns,
                orphan_policy=policy,
            ),
        ),
        ordering=(),
    )
    sources = {"parent": parent, "child": child}
    outcome = _assert_parity_or_faithful_rejection(
        plan, sources, graph, f"nonrep-int-{policy.name}"
    )
    assert outcome == "ooc-fail-closed"


def test_underscore_column_staged_path_non_collision() -> None:
    """Two composite FK edges from the SAME parent whose parent-column tuples
    underscore-collide -- ('a_b','c') and ('a','b_c') both render 'a_b_c' -- must
    stage to DISTINCT parquet relations. The old `'_'.join(columns)` path shared
    one file, so the second relation clobbered the first and the joins crossed.
    Truncate keys make a cross-wired join produce visibly wrong values.
    """
    ns = "ns_key"
    kseed = _seed("truncate", provider_config=(("length", 5),))
    # Parent columns carry distinct, distinguishable values per column so a
    # cross-wired relation would resolve children to the wrong masked token.
    parent = pa.table(
        {
            "a_b": pa.array(["AB0zzz", "AB1zzz"], type=pa.string()),
            "c": pa.array(["C0yyy", "C1yyy"], type=pa.string()),
            "a": pa.array(["A0xxx", "A1xxx"], type=pa.string()),
            "b_c": pa.array(["BC0www", "BC1www"], type=pa.string()),
        }
    )
    child1 = pa.table(
        {
            "x": pa.array(["AB0zzz", "AB1zzz"], type=pa.string()),
            "y": pa.array(["C0yyy", "C1yyy"], type=pa.string()),
        }
    )
    child2 = pa.table(
        {
            "m": pa.array(["A0xxx", "A1xxx"], type=pa.string()),
            "n": pa.array(["BC0www", "BC1www"], type=pa.string()),
        }
    )
    plan = _plan(
        (
            (
                "parent",
                TableSeed(
                    per_column=(("a_b", kseed), ("c", kseed), ("a", kseed), ("b_c", kseed)),
                    per_group=(),
                ),
            ),
            ("child1", TableSeed(per_column=(("x", kseed), ("y", kseed)), per_group=())),
            ("child2", TableSeed(per_column=(("m", kseed), ("n", kseed)), per_group=())),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("a_b", "c"),
                child_table="child1",
                child_columns=("x", "y"),
                namespace=ns,
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
            RelationshipEdge(
                parent_table="parent",
                parent_columns=("a", "b_c"),
                child_table="child2",
                child_columns=("m", "n"),
                namespace=ns,
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    sources = {"parent": parent, "child1": child1, "child2": child2}
    _assert_value_equal(
        _run_oracle(plan, sources, graph).outputs["child1"],
        _run_ooc(plan, sources, graph).outputs["child1"],
        "collision-child1",
    )
    _assert_value_equal(
        _run_oracle(plan, sources, graph).outputs["child2"],
        _run_ooc(plan, sources, graph).outputs["child2"],
        "collision-child2",
    )


@pytest.mark.parametrize("policy", list(OrphanPolicy))
def test_orphan_policies_parity(policy: OrphanPolicy) -> None:
    """Each orphan policy matches the oracle (or, for FAIL with orphans, BOTH
    raise the same orphan error)."""
    plan, sources, graph = _build_single_edge(
        parent_kind="str",
        child_kind="str",
        strategy="hash",
        policy=policy,
        parent_rows=3,
        child_refs=[0, -1, 1, None, 2, -1],
        parent_nan_row=None,
    )
    outcome = _assert_parity_or_faithful_rejection(plan, sources, graph, f"orphan-{policy.name}")
    if policy is OrphanPolicy.FAIL:
        assert outcome == "both-raised"
    else:
        assert outcome == "parity"


def test_fail_policy_both_raise_same_code() -> None:
    """FAIL with orphans: oracle and out-of-core both raise orphan_fk_violation."""
    plan, sources, graph = _build_single_edge(
        parent_kind="int",
        child_kind="int",
        strategy="hash",
        policy=OrphanPolicy.FAIL,
        parent_rows=3,
        child_refs=[0, -1, 1],
        parent_nan_row=None,
    )
    with pytest.raises(ExecutionError) as oracle_exc:
        _run_oracle(plan, sources, graph)
    with pytest.raises(ExecutionError) as ooc_exc:
        _run_ooc(plan, sources, graph)
    assert oracle_exc.value.code == "orphan_fk_violation"
    assert ooc_exc.value.code == "orphan_fk_violation"


def test_self_referential_fk_gate_rejects() -> None:
    """Codex round-4 Finding B regression test.

    A self-referential FK edge (same table both ends, e.g. employees.id ->
    employees.manager_id) is a config the full-frame oracle handles natively
    (its work-node ordering is per-COLUMN, so `id` and `manager_id` are
    different, acyclic nodes) but the out-of-core route cannot: its
    `_table_order` sequences whole TABLES, so a self-referential edge makes a
    table its own dependency and always raised `out_of_core_relationship_cycle`
    before this fix, on a config the gate had admitted. The gate must reject it
    fail-closed instead, which `_assert_parity_or_faithful_rejection` accepts
    as a "gate-rejected" outcome (the route never runs, so it can never emit
    wrong output; the job falls back to full-frame).
    """
    ns = "ns_self"
    id_seed = _seed_for("hash", ns)
    mgr_seed = _seed_for("passthrough", ns)
    plan = _plan(
        (
            (
                "employees",
                TableSeed(per_column=(("id", id_seed), ("manager_id", mgr_seed)), per_group=()),
            ),
        )
    )
    graph = RelationshipGraph(
        edges=(
            RelationshipEdge(
                parent_table="employees",
                parent_columns=("id",),
                child_table="employees",
                child_columns=("manager_id",),
                namespace=ns,
                orphan_policy=OrphanPolicy.PRESERVE,
            ),
        ),
        ordering=(),
    )
    sources = {
        "employees": pa.table(
            {
                "id": pa.array(["e1", "e2", "e3"], type=pa.string()),
                "manager_id": pa.array([None, "e1", "e1"], type=pa.string()),
            }
        )
    }
    outcome = _assert_parity_or_faithful_rejection(plan, sources, graph, "self-referential")
    assert outcome == "gate-rejected"
