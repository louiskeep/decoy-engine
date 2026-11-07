"""Guard: every hierarchy kind goes through Hierarchy.generalize and keeps the level contract.

The kinds are found by walking the package (every module imported, then every
subclass of Hierarchy defined in it), not from a hand-written list.
"""

from __future__ import annotations

import datetime
import importlib
import inspect
import pkgutil
import sys
import typing
from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

import decoy_engine.anonymize.hierarchies as package
from decoy_engine.anonymize.hierarchies import SUPPRESSED, Hierarchy, check_hierarchy

from ._cols import labels, obj

# One sample per builder: the hierarchy, covered values (with a null) and one uncovered value.
SAMPLES: dict[str, tuple[Callable[[], Hierarchy], list[Any], Any]] = {
    "interval_hierarchy": (
        lambda: package.interval_hierarchy((5, 10)), [3, 12, 27, None, 3], "x"),
    "band_hierarchy": (
        lambda: package.band_hierarchy(((0, 10, 20), (10,))), [3, 12, 27, None, -4], "x"),
    "prefix_hierarchy": (
        lambda: package.prefix_hierarchy(3), ["abc", "abd", "xyz", None, "abc"], "ab"),
    "date_hierarchy": (
        lambda: package.date_hierarchy(),
        [datetime.date(2024, 1, 5), datetime.date(2024, 7, 1), None, datetime.date(2023, 2, 2)],
        "2024-01-01"),
    "category_hierarchy": (
        lambda: package.category_hierarchy([("a", "g1", "t"), ("b", "g1", "t"), ("c", "g2", "t")]),
        ["a", "b", "c", None, "a"], "z"),
}  # fmt: skip


def _kinds() -> set[type]:
    for module in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module.name}")
    found: set[type] = set()
    stack = list(Hierarchy.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        # dataclass(slots=True) replaces the class it decorates; the discarded
        # original can linger in __subclasses__, so keep the module's binding.
        home = sys.modules.get(cls.__module__)
        if (
            cls.__module__.startswith(package.__name__)
            and getattr(home, cls.__name__, None) is cls
            and not getattr(cls, "__abstractmethods__", None)
        ):
            found.add(cls)
    return found


def _builds_a_hierarchy(obj: object) -> bool:
    """A public function whose declared return type is a Hierarchy kind."""
    if not inspect.isfunction(obj):
        return False
    returned = typing.get_type_hints(obj).get("return")
    return isinstance(returned, type) and issubclass(returned, Hierarchy)


def overriders(classes: Sequence[type]) -> list[type]:
    """Kinds that define their own generalize (bypassing the choke point)."""
    return [cls for cls in classes if "generalize" in vars(cls)]


def test_walk_sees_every_kind_and_each_has_a_builder() -> None:
    kinds = _kinds()
    builders = sorted(n for n in package.__all__ if _builds_a_hierarchy(getattr(package, n)))
    assert builders == sorted(SAMPLES)
    built = {type(SAMPLES[name][0]()) for name in builders}
    assert kinds == built
    assert len(kinds) == 5


def test_no_kind_overrides_generalize() -> None:
    assert overriders(sorted(_kinds(), key=lambda c: c.__name__)) == []


def test_guard_sees_a_seeded_bypass() -> None:
    class Bypass(package.IntervalHierarchy):
        def generalize(self, column: Any, level: int) -> Any:
            return column

    assert overriders([Bypass]) == [Bypass]


def _arrow(values: list[Any]) -> pa.Array:
    return pa.array(values)


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_level_contract(name: str) -> None:
    make, values, uncovered = SAMPLES[name]
    h = make()
    column = obj(values)
    n = len(values)
    nulls = [v is None for v in values]
    assert labels(h.generalize(column, 0)) == values
    assert labels(h.generalize(column, h.height)) == [SUPPRESSED] * n
    for level in range(1, h.height):
        out = labels(h.generalize(column, level))
        assert [v is None for v in out] == nulls
        assert all(isinstance(v, str) and v != SUPPRESSED for v in out if v is not None)
    for level in range(h.height + 1):
        with pytest.raises(ValueError, match="1 rows"):
            h.generalize(obj([*values, uncovered]), level)
    report = check_hierarchy(h, column)
    assert report.monotone and report.covers_column and report.classes_per_level[-1] == 1


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_order_chunk_and_route_invariance(name: str) -> None:
    make, values, _ = SAMPLES[name]
    h = make()
    for level in range(h.height + 1):
        whole = labels(h.generalize(obj(values), level))
        reversed_ = labels(h.generalize(obj(values[::-1]), level))
        assert reversed_ == whole[::-1]
        parts = labels(h.generalize(obj(values[:2]), level)) + labels(
            h.generalize(obj(values[2:]), level)
        )
        assert parts == whole
        if level:
            chunked = pa.chunked_array([_arrow(values[:2]), _arrow(values[2:])])
            assert labels(h.generalize(chunked, level)) == whole
            assert labels(h.generalize(_arrow(values), level)) == whole


@pytest.mark.parametrize("name", ["interval_hierarchy", "category_hierarchy"])
@pytest.mark.parametrize("cells", [[1, True], [True, 1], [1, 1.0], [1.0, 1]])
def test_mixed_families_do_not_depend_on_order(name: str, cells: list[Any]) -> None:
    h = (
        package.interval_hierarchy((5,))
        if name == "interval_hierarchy"
        else package.category_hierarchy([(1, "one")])
    )
    forward = check_hierarchy(h, obj(cells))
    backward = check_hierarchy(h, obj(cells[::-1]))
    assert forward == backward
    assert forward.classes_per_level[0] == 2 - forward.rows_not_covered


def test_level_argument_checked() -> None:
    h = package.interval_hierarchy((5,))
    for level in (True, 1.0, -1, 3):
        with pytest.raises((TypeError, ValueError)):
            h.generalize(pd.Series([1]), level)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        h.generalize([1, 2], 1)  # type: ignore[arg-type, unused-ignore]
