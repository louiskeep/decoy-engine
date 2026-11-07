"""Every failure path raises without a cell value and without an exception chain."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import Any

import pandas as pd
import pytest

from decoy_engine.anonymize.hierarchies import (
    Lattice,
    band_hierarchy,
    category_hierarchy,
    check_hierarchy,
    date_hierarchy,
    interval_hierarchy,
    prefix_hierarchy,
)

from ._cols import obj

SECRETS = ("SECRET-7731", "7731", "1977")
WHEN = datetime.datetime(1977, 3, 1, tzinfo=datetime.timezone.utc)
WHEN_NY = datetime.datetime(1977, 3, 1, tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))

CASES: dict[str, Callable[[], Any]] = {
    "interval uncovered": lambda: interval_hierarchy((5,)).generalize(obj(["SECRET-7731"]), 1),
    "band uncovered": lambda: band_hierarchy(((1,),)).generalize(obj(["SECRET-7731"]), 1),
    "prefix uncovered": lambda: prefix_hierarchy(5).generalize(obj([7731.25, "SECRET-7731"]), 1),
    "date uncovered": lambda: date_hierarchy().generalize(obj(["SECRET-7731"]), 1),
    "category uncovered": lambda: category_hierarchy([("a", "x")]).generalize(
        obj(["SECRET-7731", [7731]]), 1
    ),
    "mixed zones": lambda: date_hierarchy().generalize(obj([WHEN, WHEN_NY]), 1),
    "mixed zones check": lambda: check_hierarchy(date_hierarchy(), obj([WHEN_NY, WHEN])),
    "interval width": lambda: interval_hierarchy((7731.25,)),  # type: ignore[arg-type]
    "interval multiple": lambda: interval_hierarchy((7731, 7732)),
    "band order": lambda: band_hierarchy(((7731.25, 1),)),
    "band subset": lambda: band_hierarchy(((1, 7731), (7732,))),
    "prefix keep": lambda: prefix_hierarchy(5, keep=(7731,)),
    "date level": lambda: date_hierarchy(("SECRET-7731",)),
    "category duplicate": lambda: category_hierarchy([("SECRET-7731", "x"), ("SECRET-7731", "y")]),
    "category tree": lambda: category_hierarchy(
        [("a", "SECRET-7731", "t1"), ("b", "SECRET-7731", "t2")]
    ),
    "category key type": lambda: category_hierarchy([(7731.25, "x")]),  # type: ignore[list-item]
    "level": lambda: interval_hierarchy((5,)).generalize(pd.Series([1]), 7731),
    "node": lambda: Lattice({"a": interval_hierarchy((5,))}).successors((7731,)),
    "apply": lambda: Lattice({"a": interval_hierarchy((5,))}).apply(
        pd.DataFrame({"a": ["SECRET-7731"]}), (1,)
    ),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_value_free(name: str) -> None:
    with pytest.raises((TypeError, ValueError)) as exc:
        CASES[name]()
    message = str(exc.value)
    for secret in SECRETS:
        assert secret not in message, name
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
