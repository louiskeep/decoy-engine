"""GEN-REL cardinality repair and the shared reference-pool check (AT-1, AT-3, AT-4, AT-15).

The repair is called the way both generators call it: the v2 `_reference` draw and the V1
`ColumnGenerator` thin call. Bounds are exact: no parent passes `max_per_parent` on a feasible
request, and an infeasible one is refused instead of repaired on a best-effort basis.
"""

from __future__ import annotations

import logging
import random
import subprocess
import sys
import textwrap
import tracemalloc
from collections import Counter
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest

from decoy_engine import reasons
from decoy_engine.config import value_key
from decoy_engine.generation._synthesize_derived import _reference
from decoy_engine.generation.pool import GenerationError
from decoy_engine.generators.columns import ColumnGenerator
from decoy_engine.generators.derivation import with_table
from tests.unit.generation.relational._support import family, is_safe, run_route
from tests.unit.generation.unique._gu_e2_support import table

SRC = Path(__file__).resolve().parents[4] / "src"
LIST_KEYS: list[Any] = [[1, 2], [3, 4], [5]]
DICT_KEYS: list[Any] = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}, {"a": 3, "b": "z"}]


def _relational() -> Any:
    from decoy_engine.generation import relational

    return relational


def _repair(values: list[Any], parents: list[Any], lo: int, hi: int, rng: Any, **names: Any) -> Any:
    return _relational().apply_cardinality_bounds(values, parents, lo, hi, rng, **names)


def _counts(values: list[Any]) -> Counter[Any]:
    return Counter(value_key(v) for v in values)


def _v1(
    values: list[Any], parents: list[Any], lo: int, hi: int, seed: int = 5, rng: Any = None
) -> list[Any]:
    return ColumnGenerator(seed=1)._apply_cardinality_bounds(
        list(values), parents, lo, hi, rng=rng or random.Random(seed)
    )


def _col(**options: Any) -> dict[str, Any]:
    return with_table(
        {
            "name": "pid",
            "type": "reference",
            "reference_table": "par",
            "reference_column": "k",
            **options,
        },
        "child",
    )


# ---- AT-1: the move alone changes no byte ------------------------------------------------------

FIVE = ["a", "b", "c", "d", "e"]
# (drawn, min, max, expected): recorded on the slice base through the same V1 thin call. Each
# cell frees exactly as many slots as the deficits need, so the fill step never runs and every
# shuffle of the moved repair (over-max slots, donors, the queue) is exercised.
MOVE_ONLY_WITNESSES = {
    "over_max": ("abaaaaaab", 1, 4, ["a", "b", "e", "a", "c", "a", "a", "d", "b"]),
    "donors": ("aabaaaaab", 1, 0, ["a", "a", "b", "e", "a", "a", "d", "a", "c"]),
    "over_max_and_donors": ("aaaabbbbcd", 2, 3, ["e", "a", "c", "a", "b", "e", "d", "b", "c", "d"]),
}


@pytest.mark.parametrize("name", sorted(MOVE_ONLY_WITNESSES))
def test_at1_repair_without_a_fill_is_byte_identical_to_the_base(name: str) -> None:
    drawn, lo, hi, expected = MOVE_ONLY_WITNESSES[name]
    assert _v1(list(drawn), FIVE, lo, hi) == expected


def test_at1_unbounded_reference_never_enters_the_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the repair ran for a reference with no positive bound")

    monkeypatch.setattr(ColumnGenerator, "_apply_cardinality_bounds", boom)
    pools = {"par": pa.table({"k": ["a", "b", "c"]})}
    for bounds in ({}, {"min_per_parent": 0, "max_per_parent": 0}):
        assert len(_reference(_col(**bounds), 9, 3, None, pools)) == 9


# ---- AT-3: feasibility is exact at both equality boundaries ------------------------------------


def test_at3_min_above_capacity_refuses_at_run(tmp_path: Path) -> None:
    with pytest.raises(GenerationError) as exc:
        run_route("resident", family(10, 20, min_per_parent=3), tmp_path)
    assert exc.value.code == reasons.runtime.GENERATE_PER_PARENT_INFEASIBLE
    assert exc.value.code.namespace == "runtime"
    assert is_safe(exc.value)


@pytest.mark.parametrize("route", ["resident", "pinned"])
def test_at3_equal_bounds_put_every_parent_at_exactly_that_count(
    route: str, tmp_path: Path
) -> None:
    out = run_route(route, family(10, 20, min_per_parent=2, max_per_parent=2), tmp_path)
    counts = Counter(out["kids"].column("pid").to_pylist())
    assert sorted(counts) == list(range(1, 11))
    assert set(counts.values()) == {2}


@pytest.mark.parametrize(
    ("lo", "hi", "parents", "rows", "feasible"),
    [
        (2, 0, 10, 20, True),  # min * P == N
        (2, 0, 10, 19, False),
        (0, 2, 10, 20, True),  # N == max * P
        (0, 2, 10, 21, False),
        (2, 2, 10, 20, True),
        (0, 0, 10, 99, True),  # zero means unset
        (3, 5, 4, 12, True),
        (3, 5, 4, 20, True),
        (3, 5, 4, 21, False),
        (3, 5, 4, 11, False),
    ],
)
def test_at3_check_reference_pool_feasibility_boundaries(
    lo: int, hi: int, parents: int, rows: int, feasible: bool
) -> None:
    check = _relational().check_reference_pool
    col = _col(min_per_parent=lo, max_per_parent=hi)
    keys = [f"k{i}" for i in range(parents)]
    if feasible:
        check(col, keys, rows, None, "child", None)
        return
    with pytest.raises(GenerationError) as exc:
        check(col, keys, rows, None, "child", None)
    assert exc.value.code == reasons.runtime.GENERATE_PER_PARENT_INFEASIBLE
    assert is_safe(exc.value)
    assert not any(ch.isdigit() for ch in str(exc.value))


def test_at3_an_empty_pool_skips_feasibility() -> None:
    check = _relational().check_reference_pool
    check(_col(min_per_parent=3, max_per_parent=4), [], 20, None, "child", None)
    check(_col(distribution="weighted", weights=[1, 2]), [], 20, "preserve", "child", None)


def test_weights_are_checked_against_the_distinct_key_count() -> None:
    check = _relational().check_reference_pool
    keys = ["a", "b", "c", "d"]
    check(_col(distribution="weighted", weights=[1, 2, 3, 4]), keys, 9, None, "child", None)
    check(_col(distribution="weighted"), keys, 9, None, "child", None)
    # Weights on another distribution are not read by the draw, so they are not refused.
    check(_col(distribution="random", weights=[1, 2]), keys, 9, None, "child", None)
    for weights in ([1, 2, 3], [1, 2, 3, 4, 5], []):
        with pytest.raises(GenerationError) as exc:
            check(_col(distribution="weighted", weights=weights), keys, 9, None, "child", None)
        assert exc.value.code == reasons.runtime.REFERENCE_WEIGHTS_LENGTH_MISMATCH


# ---- AT-4: the real distinct key count decides, and max is never passed ------------------------


def _three_of_four() -> tuple[dict[str, Any], int]:
    """A parent of four rows that holds three distinct keys under the returned seed."""
    for seed in range(1, 200):
        parent = table(
            "parents", 4, [{"name": "id", "type": "categorical", "categories": ["a", "b", "c"]}]
        )
        config = family(0, 20, parent=parent, seed=seed)
        tables = run_route("resident", config, Path("unused"))
        if len(set(tables["parents"].column("id").to_pylist())) == 3:
            return parent, seed
    raise AssertionError("no seed gives three distinct keys in four rows")


def test_at4_the_run_checks_distinct_keys_not_row_count(tmp_path: Path) -> None:
    parent, seed = _three_of_four()
    # 6 * 4 rows covers 20 children, so validate passes; 6 * 3 real keys does not.
    tight = family(0, 20, parent=parent, seed=seed, max_per_parent=6)
    with pytest.raises(GenerationError) as exc:
        run_route("resident", tight, tmp_path)
    assert exc.value.code == reasons.runtime.GENERATE_PER_PARENT_INFEASIBLE
    out = run_route("resident", family(0, 20, parent=parent, seed=seed, max_per_parent=7), tmp_path)
    counts = Counter(out["kids"].column("pid").to_pylist())
    assert sum(counts.values()) == 20 and max(counts.values()) <= 7


@pytest.mark.parametrize("caller", ["v1", "relational"])
def test_at4_max_holds_over_200_seeds(caller: str) -> None:
    parents = ["a", "b", "c"]
    broken = 0
    for seed in range(200):
        rng = random.Random(seed)
        drawn = [rng.choice(parents) for _ in range(20)]
        if caller == "v1":
            out = _v1(drawn, parents, 0, 7, rng=rng)
        else:
            out = _repair(drawn, parents, 0, 7, rng)
        assert len(out) == 20 and set(out) <= set(parents)
        broken += sorted(Counter(out).values())[-1] not in range(8)
    assert broken == 0


def test_at4_min_and_max_hold_together_over_200_seeds() -> None:
    parents = [f"p{i}" for i in range(6)]
    for seed in range(200):
        rng = random.Random(seed)
        drawn = [rng.choice(parents) for _ in range(30)]
        counts = Counter(_repair(drawn, parents, 4, 6, rng))
        assert all(counts[p] in (4, 5, 6) for p in parents), (seed, dict(counts))


def test_at4_a_very_large_max_allocates_nothing_proportional_to_it() -> None:
    """A repair that listed every slot up front would need 3 * 10**12 entries and never return."""
    parents = ["a", "b", "c"]
    rng = random.Random(3)
    drawn = [rng.choice(parents) for _ in range(20)]
    out = _repair(drawn, parents, 6, 10**12, rng)
    assert sorted(Counter(out).values())[0] in (6, 7)


@pytest.mark.serial
def test_at4_the_fill_allocates_by_rows_and_parents_not_by_max_times_parents() -> None:
    """15_000 freed slots over 2_000 parents: a list of every free slot would hold 10**7 keys."""
    parents = list(range(2_000))
    drawn = [0] * 20_000
    tracemalloc.start()
    try:
        out = _repair(drawn, parents, 0, 5_000, random.Random(4))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert Counter(out)[0] == 5_000 and len(out) == 20_000
    assert peak < 8_000_000


@pytest.mark.parametrize("keys", [LIST_KEYS, DICT_KEYS], ids=["list", "dict"])
def test_at4_v1_caller_repairs_unhashable_keys_to_exact_bounds(keys: list[Any]) -> None:
    drawn = [keys[0]] * 5 + [keys[1]]
    out = _v1(drawn, keys, 2, 2)
    assert _counts(out) == {value_key(k): 2 for k in keys}
    assert all(any(v is k for k in keys) for v in out), "output holds the original parent objects"


@pytest.mark.parametrize(
    ("keys", "arrow_type"),
    [
        (LIST_KEYS, pa.list_(pa.int64())),
        (DICT_KEYS, pa.struct([("a", pa.int64()), ("b", pa.string())])),
    ],
    ids=["list", "dict"],
)
def test_at4_v2_caller_repairs_unhashable_keys_to_exact_bounds(
    keys: list[Any], arrow_type: pa.DataType
) -> None:
    pools = {"par": pa.table({"k": pa.array(keys, type=arrow_type)})}
    for seed in range(20):
        out = _reference(_col(min_per_parent=2, max_per_parent=2), 6, seed, None, pools)
        assert _counts(out) == {value_key(k): 2 for k in keys}
        assert all(type(v) is type(keys[0]) for v in out)


def test_at4_the_final_check_is_an_explicit_branch_not_a_python_assert() -> None:
    """Under `python -O` a write that lands on the wrong parent must still be refused."""
    script = textwrap.dedent(
        """
        import random
        from decoy_engine.generation.pool import GenerationError
        from decoy_engine.generation.relational import apply_cardinality_bounds

        assert False, "this line must not run: the interpreter was started without -O"

        class Corrupt(list):
            # Every repaired slot lands on one parent, whatever the repair chose.
            def __setitem__(self, index, value):
                super().__setitem__(index, "a")

        drawn = Corrupt(["a", "a", "a", "a", "b", "c"])
        try:
            apply_cardinality_bounds(drawn, ["a", "b", "c"], 2, 2, random.Random(1))
        except GenerationError as exc:
            print("CODE", exc.code)
        else:
            print("NOT REFUSED")
        """
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, test-authored script
        [sys.executable, "-O", "-c", script],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(SRC), "PATH": ""},
        check=False,
    )
    assert done.stdout.strip() == "CODE reference_bounds_unsatisfied", done.stderr[-2000:]


# ---- the postcondition and value-free refusals (AT-15) -----------------------------------------


@pytest.mark.parametrize(
    ("drawn", "lo", "hi"),
    [
        (["a", "a", "b"], 2, 0),  # min * P > N: a deficit no slot can pay
        (["a", "a", "a", "b", "b", "b", "c"], 0, 2),  # N > max * P: no parent has room
    ],
)
def test_infeasible_bounds_that_reach_the_repair_are_refused_not_patched(
    drawn: list[str], lo: int, hi: int, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG), pytest.raises(GenerationError) as exc:
        _repair(drawn, ["a", "b", "c"], lo, hi, random.Random(2), table="child", column="pid")
    assert exc.value.code == reasons.runtime.REFERENCE_BOUNDS_UNSATISFIED
    assert is_safe(exc.value)
    assert str(exc.value) == (
        "[reference_bounds_unsatisfied] "
        f"{reasons.get('runtime', 'reference_bounds_unsatisfied').summary}"
        " Table 'child', column 'pid'."
    )
    assert caplog.records == []


def test_the_repair_is_a_function_of_the_seed() -> None:
    parents = [f"p{i}" for i in range(8)]

    def run(seed: int) -> list[str]:
        rng = random.Random(seed)
        drawn = [rng.choice(parents) for _ in range(64)]
        return list(_repair(drawn, parents, 6, 10, rng))

    assert run(11) == run(11)
    assert run(11) != run(12)


def test_empty_parent_policy_tokens() -> None:
    check = _relational().check_reference_pool
    seen: list[Any] = []
    with pytest.raises(GenerationError) as exc:
        check(_col(), [], 5, "fail", "child", seen.append)
    assert exc.value.code == reasons.runtime.REFERENCE_PARENT_POOL_EMPTY
    assert seen == []
    check(_col(), [], 5, "warn", "child", seen.append)
    assert [w.code for w in seen] == ["reference_parent_pool_empty"]
    for policy in ("preserve", "remap", None):
        check(_col(), [], 5, policy, "child", seen.append)
    check(_col(), [], 0, "fail", "child", seen.append)
    check(_col(), ["a"], 5, "fail", "child", seen.append)
    assert len(seen) == 1
