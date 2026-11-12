"""GEN-REL self-reference trees: a table whose `reference` column names its own key.

AT-8 (rows below `root_count` are roots, every other parent is an earlier row, every chain ends
at a root), the capped draw of AT-9 (`max_per_parent`) and the tree cells of AT-16 (same seed,
same tree; another seed, another tree; no module-global `random`), and the self-reference
cells of AT-10: pinned compile refuses the shape and pinned execute refuses it again, with the
run-time twin, for a plan that never went through compile.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import random
from collections import Counter
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.generation import relational, synthesize
from decoy_engine.generation._admission import classify_plan
from decoy_engine.generation._plan_entry import generate_tables_streamed
from decoy_engine.generation.pinned import (
    PinnedModeError,
    PinnedPlan,
    compile_pinned_plan,
    execute_pinned,
)
from decoy_engine.generators.derivation import GenDeriveContext, with_table
from tests.unit.generation.relational._support import REF, family, is_safe, plan_of
from tests.unit.generation.unique._gu_e2_support import cfg, ref, run, seq_parent, table

ROWS = 200
SEEDS = tuple(range(11, 21))
ID = {"name": "id", "type": "sequence", "start": 1}


def _tree(rows: int = ROWS, key: dict[str, Any] | None = None, **options: Any) -> dict[str, Any]:
    return table("employees", rows, [key or ID, ref("manager_id", "employees", **options)])


def _generated(seed: int, rows: int = ROWS, **options: Any) -> tuple[list[Any], list[Any]]:
    out = run([_tree(rows, **options)], seed)["employees"]
    return out.column("id").to_pylist(), out.column("manager_id").to_pylist()


def _column(**options: Any) -> dict[str, Any]:
    return with_table(ref("manager_id", "employees", **options), "employees")


def _draw(n: int, seed: int = 7, keys: list[Any] | None = None, **options: Any) -> list[Any]:
    keys = list(range(1, n + 1)) if keys is None else keys
    return relational.self_reference(_column(**options), n, seed, None, {"id": keys})


def _assert_forest(ids: list[Any], parents: list[Any], roots: int) -> None:
    """Roots are null, each other parent is an earlier row's key, each chain ends at a root."""
    row_of = {key: i for i, key in enumerate(ids)}
    assert len(row_of) == len(ids)
    assert parents[:roots] == [None] * roots
    for i in range(roots, len(ids)):
        assert parents[i] is not None, f"row {i} has no parent"
        assert row_of[parents[i]] < i, f"row {i} points at row {row_of[parents[i]]}"
    for i in range(len(ids)):
        at, steps = i, 0
        while parents[at] is not None:
            at, steps = row_of[parents[at]], steps + 1
            assert steps <= len(ids), f"row {i} never reaches a root"
        assert at < roots


# ---- AT-8: the tree invariant -------------------------------------------------------------------


@pytest.mark.parametrize("roots", [1, 3, 17])
def test_at8_every_row_hangs_from_an_earlier_row_and_every_chain_ends_at_a_root(
    roots: int,
) -> None:
    options = {} if roots == 1 else {"root_count": roots}
    for seed in SEEDS:
        ids, parents = _generated(seed, **options)
        assert ids == list(range(1, ROWS + 1))
        _assert_forest(ids, parents, roots)


def test_at8_the_default_is_one_tree_and_a_later_row_can_be_a_parent() -> None:
    for seed in SEEDS:
        _, parents = _generated(seed)
        assert parents.count(None) == 1
        # A star (every row under row 0) is a legal tree but not a recursive one.
        assert any(parent != 1 for parent in parents[1:])


def test_at8_all_rows_can_be_roots_and_an_empty_table_is_empty() -> None:
    assert _draw(5, root_count=5) == [None] * 5
    assert _draw(0) == []
    assert run([_tree(0)])["employees"].num_rows == 0


def test_at8_a_string_key_is_copied_as_it_is() -> None:
    key = {"name": "id", "type": "sequence", "start": 1, "prefix": "E-", "pad_length": 4}
    out = run([_tree(40, key)])["employees"]
    ids, parents = out.column("id").to_pylist(), out.column("manager_id").to_pylist()
    assert ids[0] == "E-0001"
    _assert_forest(ids, parents, 1)
    assert out.schema.field("manager_id").type == out.schema.field("id").type == pa.string()


def test_at8_the_parent_column_keeps_the_key_columns_arrow_type() -> None:
    out = run([_tree(40)])["employees"]
    assert out.schema.field("manager_id").type == out.schema.field("id").type
    assert pa.types.is_integer(out.schema.field("id").type)


def test_at8_the_draw_is_uniform_over_the_earlier_rows() -> None:
    """Row `i` takes `randrange(i)` from the per-column seed: the random recursive tree."""
    col = _column()
    rng = random.Random(
        GenDeriveContext.for_column(derive_key=None, column_config=col, fallback_seed=7).base_int(
            "py"
        )
    )
    keys = [f"k{i}" for i in range(50)]
    want = [None, *(keys[rng.randrange(i)] for i in range(1, 50))]
    assert relational.self_reference(col, 50, 7, None, {"id": keys}) == want


def test_at8_a_self_reference_sits_beside_an_ordinary_reference() -> None:
    tables = [
        seq_parent("depts", 4),
        table("employees", 60, [ID, ref("dept", "depts"), ref("manager_id", "employees")]),
    ]
    out = run(tables)["employees"]
    _assert_forest(out.column("id").to_pylist(), out.column("manager_id").to_pylist(), 1)
    assert set(out.column("dept").to_pylist()) <= {1, 2, 3, 4}


# ---- AT-9: the capped draw ----------------------------------------------------------------------


@pytest.mark.parametrize("cap", [1, 2, 5])
@pytest.mark.parametrize("roots", [1, 4])
def test_at9_no_key_gets_more_children_than_max_per_parent(cap: int, roots: int) -> None:
    options: dict[str, Any] = {"max_per_parent": cap}
    if roots > 1:
        options["root_count"] = roots
    for seed in SEEDS:
        ids, parents = _generated(seed, **options)
        _assert_forest(ids, parents, roots)
        counts = Counter(p for p in parents if p is not None)
        assert max(counts.values()) <= cap
        # Rows added after the roots must be able to take children, or the draw runs dry.
        assert any(p not in ids[:roots] for p in parents[roots:])


def test_at9_a_cap_of_one_is_a_set_of_chains() -> None:
    ids, parents = _generated(3, 50, max_per_parent=1)
    assert parents == [None, *ids[:-1]]


def test_at9_a_zero_cap_is_no_cap() -> None:
    assert _draw(80, max_per_parent=0) != _draw(80, max_per_parent=1)
    counts = Counter(p for p in _draw(80, max_per_parent=0) if p is not None)
    assert max(counts.values()) > 1


def test_at9_the_capped_draw_allocates_by_rows_not_by_the_cap() -> None:
    assert len(_draw(300, max_per_parent=10**12)) == 300


# ---- AT-16: seed determinism ---------------------------------------------------------------------


@pytest.mark.parametrize("options", [{}, {"max_per_parent": 2}, {"root_count": 5}])
def test_at16_the_same_seed_gives_the_same_tree_and_another_seed_another(
    options: dict[str, Any],
) -> None:
    first = _generated(4242, **options)[1]
    random.seed(99)  # a draw from module-global random would follow this state
    assert _generated(4242, **options)[1] == first
    second = _generated(4243, **options)[1]
    assert sum(a != b for a, b in zip(first, second, strict=True)) >= 1
    assert first.count(None) == second.count(None)


def test_at16_the_draw_leaves_module_global_random_alone() -> None:
    random.seed(5)
    before = random.getstate()
    _draw(120)
    _draw(120, max_per_parent=2)
    assert random.getstate() == before


def test_at16_the_seed_is_per_column() -> None:
    tables = [
        table("employees", 120, [ID, ref("manager_id", "employees"), ref("mentor_id", "employees")])
    ]
    out = run(tables)["employees"]
    assert out.column("manager_id").to_pylist() != out.column("mentor_id").to_pylist()


# ---- routes: a self-reference runs in memory -----------------------------------------------------


def test_the_streaming_admission_declines_a_self_reference_and_the_run_stays_resident() -> None:
    config = cfg([_tree(30)])
    verdict = classify_plan(config)
    assert not verdict.eligible
    assert [(d.column, d.reason) for d in verdict.declines] == [
        ("manager_id", "reference_to_own_table")
    ]
    result = generate_tables_streamed(plan_of(config), sink=None)
    assert result.mode == "resident" and result.tables is not None
    whole = run([_tree(30)])["employees"]
    assert result.tables["employees"].equals(whole)


# ---- AT-10: pinned mode refuses the shape at compile and again at execute -----------------------


@pytest.mark.parametrize("options", [{}, {"max_per_parent": 2}, {"root_count": 3}])
def test_at10_pinned_compile_refuses_a_self_reference(options: dict[str, Any]) -> None:
    with pytest.raises(PinnedModeError) as exc:
        compile_pinned_plan(cfg([_tree(9, **options)]), REF)
    assert exc.value.code == reasons.plan.PINNED_RELATIONAL_OPTION_UNSUPPORTED
    assert exc.value.code.namespace == "plan"
    assert is_safe(exc.value)


def _self_referencing(plan: PinnedPlan) -> PinnedPlan:
    """`kids.pid` rewritten to point at `kids.cid`, as a plan that skipped compile could."""
    kids = next(t for t in plan.tables if t.name == "kids")
    column = json.loads(kids.columns[1].config_json)
    column.update(reference_table="kids", reference_column="cid")
    rewritten = dataclasses.replace(
        kids,
        depends_on=(),
        columns=(
            kids.columns[0],
            dataclasses.replace(kids.columns[1], config_json=json.dumps(column, sort_keys=True)),
        ),
    )
    return dataclasses.replace(
        plan, tables=tuple(rewritten if t.name == "kids" else t for t in plan.tables)
    )


def _no_draw(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a column was drawn from a plan execute must refuse")

    monkeypatch.setattr(synthesize, "_generate_column", boom)


@pytest.mark.parametrize("deserialized", [False, True], ids=["hand-built", "deserialized"])
def test_at10_pinned_execute_refuses_a_self_reference_before_any_draw(
    deserialized: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_draw(monkeypatch)
    bad = _self_referencing(compile_pinned_plan(copy.deepcopy(family(5, 9)), REF))
    if deserialized:
        bad = PinnedPlan.from_dict(json.loads(json.dumps(bad.to_dict())))
    with pytest.raises(PinnedModeError) as exc:
        execute_pinned(bad, 1)
    assert exc.value.code == reasons.runtime.PINNED_RELATIONAL_OPTION_UNSUPPORTED
    assert exc.value.code.namespace == "runtime"
    assert reasons.Phase.GENERATE in {rule.phase for rule in exc.value.code.spec.rules}
    assert is_safe(exc.value)


def test_at10_the_hand_built_plan_is_otherwise_executable() -> None:
    """Non-vacuity: the same plan with its ordinary reference back runs."""
    plan = compile_pinned_plan(copy.deepcopy(family(5, 9)), REF)
    assert execute_pinned(plan, 1)["kids"].num_rows == 9
    assert _self_referencing(plan) != plan
