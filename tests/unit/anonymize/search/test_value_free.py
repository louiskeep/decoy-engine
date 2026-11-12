"""Results and errors carry no cell value and no exception chain."""

from __future__ import annotations

import dataclasses
import datetime
import zoneinfo
from collections.abc import Callable, Sequence
from typing import Any

import pandas as pd
import pytest

from decoy_engine.anonymize.hierarchies import (
    Hierarchy,
    Lattice,
    category_hierarchy,
    date_hierarchy,
    prefix_hierarchy,
)
from decoy_engine.anonymize.search import SearchResult, apply_search, search_lattice
from decoy_engine.quality.anonymity import (
    EntropyLDiversity,
    EqualTCloseness,
    KAnonymity,
    OrderedTCloseness,
)

SENTINEL = "SECRET-7731"


def _table() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "zip": ["12345", "12399", "12345", "12399"],
            "dx": [SENTINEL, "b", SENTINEL, "b"],
            "note": [SENTINEL] * 4,
        }
    )


def _lattice() -> Lattice:
    return Lattice({"zip": prefix_hierarchy(5, keep=(3,))})


def _assert_clean(call: Callable[[], Any], kind: type[Exception]) -> str:
    with pytest.raises(kind) as info:
        call()
    message = str(info.value)
    assert SENTINEL not in message
    assert "7731" not in message
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    return message


def test_result_repr_has_no_cell_value() -> None:
    result = search_lattice(_table(), _lattice(), (KAnonymity(2), EntropyLDiversity("dx", 2)))
    assert result.found
    assert SENTINEL not in repr(result) and SENTINEL not in str(result)
    assert "123" not in repr(result)


def test_result_refuses_non_builtin_fields() -> None:
    result = search_lattice(_table(), _lattice(), (KAnonymity(2),))
    fields = dataclasses.asdict(result) | {"models": result.models}
    for name, bad in [
        ("rows", __import__("numpy").int64(4)),
        ("metric", SENTINEL),
        ("columns", (SENTINEL, 1)),
        ("node", ("1",)),
    ]:
        with pytest.raises(TypeError):
            SearchResult(**(fields | {name: bad}))


def test_uncovered_value() -> None:
    table = _table()
    table.loc[1, "zip"] = SENTINEL[:5] + "77"
    message = _assert_clean(lambda: search_lattice(table, _lattice(), (KAnonymity(2),)), ValueError)
    assert "'zip'" in message and "1 rows" in message


def test_missing_and_overlapping_columns() -> None:
    _assert_clean(
        lambda: search_lattice(_table(), _lattice(), (EqualTCloseness("nope", 0.5),)), ValueError
    )
    message = _assert_clean(
        lambda: search_lattice(
            _table(), _lattice(), (EntropyLDiversity("dx", 2), EqualTCloseness("zip", 0.5))
        ),
        ValueError,
    )
    assert "'zip'" in message


def test_ordered_t_on_text() -> None:
    _assert_clean(
        lambda: search_lattice(_table(), _lattice(), (OrderedTCloseness("dx", 0.5),)), TypeError
    )


def test_unhashable_sensitive_cells() -> None:
    table = _table()
    table["dx"] = pd.Series([[SENTINEL], ["b"], [SENTINEL], ["b"]], dtype=object)
    _assert_clean(
        lambda: search_lattice(table, _lattice(), (EntropyLDiversity("dx", 2),)), TypeError
    )


def test_mixed_time_zones_refused_by_search_and_apply() -> None:
    """V2-3.2 puts equal instants in different zones in one class, so the
    representatives alone would hide the mix; the full-column check refuses it."""
    utc = datetime.datetime(2024, 4, 1, 3, 30, tzinfo=datetime.timezone.utc)
    ny = utc.astimezone(zoneinfo.ZoneInfo("America/New_York"))
    table = pd.DataFrame(
        {"seen": pd.Series([utc, ny, utc, ny], dtype=object), "dx": ["a", "b", "a", "b"]}
    )
    lattice = Lattice({"seen": date_hierarchy()})
    _assert_clean(lambda: search_lattice(table, lattice, (KAnonymity(2),)), TypeError)
    one_zone = table.copy()
    one_zone["seen"] = pd.Series([utc, utc, utc, utc], dtype=object)
    result = search_lattice(one_zone, lattice, (KAnonymity(2),))
    _assert_clean(lambda: apply_search(table, lattice, result), TypeError)


def test_bad_arguments() -> None:
    lat = _lattice()
    _assert_clean(lambda: search_lattice([SENTINEL], lat, (KAnonymity(2),)), TypeError)
    _assert_clean(lambda: search_lattice(_table(), lat, ()), ValueError)
    _assert_clean(lambda: search_lattice(_table(), lat, (SENTINEL,)), TypeError)  # type: ignore[arg-type]
    _assert_clean(
        lambda: search_lattice(_table(), lat, (KAnonymity(2),), metric=SENTINEL),  # type: ignore[arg-type]
        TypeError,
    )
    _assert_clean(
        lambda: search_lattice(_table(), lat, (KAnonymity(2),), suppression_limit=float("nan")),
        ValueError,
    )
    _assert_clean(
        lambda: search_lattice(_table(), lat, (KAnonymity(2),), suppression_limit=1.5), ValueError
    )
    _assert_clean(
        lambda: search_lattice(_table(), lat, (KAnonymity(2),), suppression_limit=True), TypeError
    )


def test_category_keys_never_appear() -> None:
    table = pd.DataFrame({"dx": [SENTINEL, "flu", "flu"]})
    lattice = Lattice({"dx": category_hierarchy([("flu", "resp")])})
    _assert_clean(lambda: search_lattice(table, lattice, (KAnonymity(2),)), ValueError)


def test_model_subclasses_are_refused() -> None:
    """A subclass could carry a cell value into the result or an error."""

    @dataclasses.dataclass(frozen=True, slots=True)
    class Leaky(KAnonymity):
        note: str = SENTINEL

    message = _assert_clean(lambda: search_lattice(_table(), _lattice(), (Leaky(2),)), TypeError)
    assert "subclasses" in message
    result = search_lattice(_table(), _lattice(), (KAnonymity(2),))
    fields = {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
    with pytest.raises(TypeError):
        SearchResult(**(fields | {"models": (Leaky(2),)}))


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("rows", 4.0),
        ("rows", True),
        ("k_min", 2.0),
        ("loss", 1),
        ("found", 1),
        ("suppression_limit", 0),
    ],
)
def test_result_fields_have_exact_types(name: str, bad: object) -> None:
    result = search_lattice(_table(), _lattice(), (KAnonymity(2),))
    fields = {f.name: getattr(result, f.name) for f in dataclasses.fields(result)}
    with pytest.raises(TypeError):
        SearchResult(**(fields | {name: bad}))


class _RefusesLabels(Hierarchy):
    """Covers ints; refuses to label level 1 with a value-free TypeError."""

    __slots__ = ()
    kind = "test-refuses"

    @property
    def height(self) -> int:
        return 2

    def _covers(self, value: Any) -> bool:
        return isinstance(value, int)

    def _labels(self, values: Sequence[Any], level: int) -> list[str]:
        raise TypeError("labels unavailable")


def test_hierarchy_error_type_is_kept() -> None:
    table = pd.DataFrame({"n": [1, 2, 3]})
    message = _assert_clean(
        lambda: search_lattice(table, Lattice({"n": _RefusesLabels()}), (KAnonymity(1),)),
        TypeError,
    )
    assert "'n'" in message
