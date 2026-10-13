"""Phase D / Dcomp: composite-FK shape variants for the sequential route's acceptance cases.

Extends the `_d2b_shapes` composite builders with the axes Dcomp adds: an integer passthrough
key component beside a hashed one, caller-chosen tables (empty, all-null), golden-fixture data
with an injected orphan, and a parent whose two key columns each raise a different covered row
error. Every shape is a `Shape`, so the same plan runs through the full-frame oracle (`run_full`)
and the sequential route (`run_seq`).

Key-column operators here are `hash` unless a case says otherwise. The two row-error shapes use
`bucketize` and `code_set` on the key columns because those are the only operators that raise a
per-row error on a text key; they run on Python on a key (the native seam skips non-hash key
operators), so those shapes assert oracle parity and nothing about native compute.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from decoy_engine.execution import PandasExecutionAdapter
from decoy_engine.relationships._graph import OrphanPolicy
from tests.parity import _d2b_shapes as s
from tests.parity import _strict_compare as sc
from tests.parity._d1a_hash_data import column_seed

ALL_TRUE = dict.fromkeys(sc.DIMENSIONS, True)

# Past float64's exact-integer range (2**53), so a float64 round trip would change it.
BIG = 2**62 + 1
UBIG = 2**63 + 5

GOLDEN_KEY = ("member_id", "plan_id", "effective_date")


def composite_tables(
    parent: pa.Table, child: pa.Table, policy: OrphanPolicy = OrphanPolicy.REMAP
) -> s.Shape:
    """The `composite_shape` plan and graph over caller-chosen string-key tables."""
    base = s.composite_shape(policy)
    return s.Shape(base.plan, base.graph, {"P": parent, "C": child})


def key_table(k: list[str | None], j: list[str | None]) -> pa.Table:
    return pa.table({"k": pa.array(k, pa.string()), "j": pa.array(j, pa.string())})


def int_component_shape(
    policy: OrphanPolicy,
    *,
    parent_n: list[int | None],
    child_k: list[str | None],
    child_n: list[int | None],
    n_type: pa.DataType | None = None,
) -> s.Shape:
    """P.(k, n) -> C.(k, n): `k` is a hashed string and `n` an INTEGER passthrough key component.

    A passthrough integer key may hold nulls (only hash/truncate/categorical reject them), so this
    is the shape that shows a null-bearing integer component keeping its Arrow integer type."""
    n_type = n_type or pa.int64()
    plan = s.make_plan(
        {
            "P": s.table_seed({"k": s.hashed("ns_pk"), "n": s.passthrough()}),
            "C": s.table_seed({}, {("k", "n"): "ns_cg"}),
        }
    )
    g = s.graph(s.edge("P", ("k", "n"), "C", ("k", "n"), "ns_cg", policy))
    parent_k = ["a", "b", "c"][: len(parent_n)]
    return s.Shape(
        plan,
        g,
        {
            "P": pa.table({"k": s.strs(parent_k), "n": pa.array(parent_n, n_type)}),
            "C": pa.table({"k": s.strs(child_k), "n": pa.array(child_n, n_type)}),
        },
    )


def float_int_orphan_shape(
    policy: OrphanPolicy, *, orphan_n: float, matched_n: float = 1.0
) -> s.Shape:
    """P.(k, n) -> C.(k, n) with FLOAT `n`: one matched row and one genuine orphan.

    A whole-valued float orphan key folds to its integer for matching, so an orphan of 2**62 is
    preserved as an INTEGER next to the matched row's float, the column mix the lossless-typing
    contract has to refuse."""
    plan = s.make_plan(
        {
            "P": s.table_seed({"k": s.hashed("ns_pk"), "n": s.passthrough()}),
            "C": s.table_seed({}, {("k", "n"): "ns_cg"}),
        }
    )
    g = s.graph(s.edge("P", ("k", "n"), "C", ("k", "n"), "ns_cg", policy))
    return s.Shape(
        plan,
        g,
        {
            "P": pa.table({"k": s.strs(["a", "b"]), "n": pa.array([matched_n, 2.0], pa.float64())}),
            "C": pa.table(
                {"k": s.strs(["a", "z"]), "n": pa.array([matched_n, orphan_n], pa.float64())}
            ),
        },
    )


def float_parent_int_child_shape(policy: OrphanPolicy, *, orphan_int: int) -> s.Shape:
    """P.(k, n FLOAT) -> C.(k, n INT64) with a genuinely non-exact orphan integer.

    The matched child row joins the float parent column; the orphan preserves its own int64
    `orphan_int`. An odd value past 2**53 has no exact float64 representation, so no single
    column type holds both the matched float and the preserved int, and lossless typing refuses.
    Distinct from `float_int_orphan_shape`, whose whole-valued float orphan is exactly
    representable and so only exercises the magnitude guard, not the precision-loss case."""
    plan = s.make_plan(
        {
            "P": s.table_seed({"k": s.hashed("ns_pk"), "n": s.passthrough()}),
            "C": s.table_seed({}, {("k", "n"): "ns_cg"}),
        }
    )
    g = s.graph(s.edge("P", ("k", "n"), "C", ("k", "n"), "ns_cg", policy))
    return s.Shape(
        plan,
        g,
        {
            "P": pa.table({"k": s.strs(["a", "b"]), "n": pa.array([1.0, 2.0], pa.float64())}),
            "C": pa.table({"k": s.strs(["a", "z"]), "n": pa.array([1, orphan_int], pa.int64())}),
        },
    )


def golden_composite_shape(policy: OrphanPolicy, *, orphan: bool = False) -> s.Shape:
    """The `composite_key` golden data with every key column hashed and the child as one group.

    The fixture declares `orphan_policy: fail` and zero orphans, so FAIL here is the fixture's
    own policy. `orphan=True` appends one claim whose whole tuple no enrollment owns."""
    sources = s.golden_sources("composite_key", enrollments=GOLDEN_KEY, claims=GOLDEN_KEY)
    if orphan:
        claims = sources["claims"]
        extra = pa.Table.from_pylist(
            [
                {
                    "claim_id": "CL-ORPHAN",
                    "member_id": "M-ORPHAN",
                    "plan_id": "P-ORPHAN",
                    "effective_date": "1999-01-01",
                    "claim_amount": 1.0,
                    "diagnosis_code": "ICD-ORPHAN",
                }
            ],
            schema=claims.schema,
        )
        sources = {**sources, "claims": pa.concat_tables([claims, extra])}
    plan = s.make_plan(
        {
            "enrollments": s.table_seed({c: s.hashed(f"ns_{c}") for c in GOLDEN_KEY}),
            "claims": s.table_seed({}, {GOLDEN_KEY: "ns_enrollment"}),
        }
    )
    g = s.graph(s.edge("enrollments", GOLDEN_KEY, "claims", GOLDEN_KEY, "ns_enrollment", policy))
    return s.Shape(plan, g, sources)


def write_chapter_corpus(directory: Path) -> Path:
    """A two-chapter customer code corpus: a code outside chapters A and B has no bucket."""
    path = directory / "chapters.parquet"
    pq.write_table(
        pa.table(
            {"code": ["A01", "A02", "B01", "B02"], "chapter": ["A", "A", "B", "B"]},
        ),
        path,
    )
    return path


def errored_key_shape(
    corpus: Path, order: tuple[str, str], policy: OrphanPolicy = OrphanPolicy.FAIL
) -> s.Shape:
    """P.(k, j) -> C.(k, j) where parent row 1 raises a DIFFERENT covered error in each key column.

    `k` is bucketized (a non-numeric cell is a `format_error`) and `j` is a chapter-preserving
    code_set (a code outside the corpus is a `mask_error`). `order` is the declared key-column
    order on the edge and the child group. Child row 1 references the errored parent tuple, and
    no child row is a genuine orphan, so the only way it can resolve is the errored-parent
    cascade."""
    k_seed = column_seed("bucketize", namespace="ns_pk", provider_config=(("width", 10),))
    j_seed = column_seed(
        "code_set",
        namespace="ns_pj",
        provider_config=(
            ("code_set", "chapters"),
            ("chapter_preserve", True),
            ("corpus_source", f"customer:{corpus}"),
            ("mode", "mask"),
        ),
    )
    plan = s.make_plan(
        {
            "P": s.table_seed({"k": k_seed, "j": j_seed}),
            "C": s.table_seed({}, {order: "ns_cg"}),
        }
    )
    g = s.graph(s.edge("P", order, "C", order, "ns_cg", policy))
    return s.Shape(
        plan,
        g,
        {
            "P": key_table(["10", "bad", "30", "40"], ["A01", "U07.1", "B01", "B02"]),
            "C": key_table(["10", "bad", "30"], ["A01", "U07.1", "B01"]),
        },
    )


def run_seq_quarantined(
    shape: s.Shape,
    quarantine: dict[str, Any] | None,
    *,
    sink: Any = None,
    adapter: PandasExecutionAdapter | None = None,
) -> Any:
    """`run_sequential` with an explicit quarantine config (`_d2b_shapes.run_seq` has none)."""
    srcs = shape.sources
    return (adapter or PandasExecutionAdapter()).run_sequential(
        shape.plan,
        lambda t: srcs[t],
        registry=s.REGISTRY,
        relationship_graph=shape.graph,
        namespace_registry=s.NAMESPACES,
        quarantine_config=quarantine,
        sink=sink,
    )


def strict_report(oracle: Any, candidate: Any) -> dict[str, dict[str, bool]]:
    """Per-table strict report of `candidate`'s outputs against `oracle`'s (9 dimensions each)."""
    assert set(oracle.outputs) == set(candidate.outputs)
    return {t: sc.compare_strict(oracle.outputs[t], candidate.outputs[t]) for t in oracle.outputs}


def assert_strict_parity(oracle: Any, candidate: Any, label: str = "") -> None:
    report = strict_report(oracle, candidate)
    assert report == {t: ALL_TRUE for t in oracle.outputs}, (label, report)
