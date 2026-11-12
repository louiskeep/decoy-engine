"""No cell value in the document, its repr, or any error; every string leaf is a named type."""

from __future__ import annotations

import json
import types
from typing import Annotated, Any, Literal, Union, get_args, get_origin

import pandas as pd
import pytest
from pydantic import BaseModel, Strict

import decoy_engine.anonymize.report._model as model_module
from decoy_engine.anonymize.hierarchies import Lattice, interval_hierarchy, prefix_hierarchy
from decoy_engine.anonymize.report import (
    RiskUtilityReport,
    RiskUtilityReportRead,
    build_risk_utility_report,
)
from decoy_engine.quality.anonymity import EntropyLDiversity, KAnonymity
from decoy_engine.run_result import ValueFree

from ._helpers import release

SENTINEL = "SENTINELdx"
ZIP_SENTINEL = "Q7Q"


def _named_kinds() -> set[str]:
    kinds = set()
    for obj in vars(model_module).values():
        if get_origin(obj) is Annotated:
            kinds |= {m.kind for m in get_args(obj)[1:] if isinstance(m, ValueFree)}
    return kinds


def _problems(ann: Any, meta: tuple[Any, ...], path: str, seen: set[type]) -> list[str]:
    if any(isinstance(m, ValueFree) for m in meta):
        return []
    origin = get_origin(ann)
    if origin is Annotated:
        base, *extra = get_args(ann)
        return _problems(base, meta + tuple(extra), path, seen)
    if origin is Union or origin is types.UnionType or origin in (tuple, list):
        return [p for a in get_args(ann) if a is not Ellipsis for p in _problems(a, meta, path, seen)]
    if origin is Literal or ann is type(None):
        return []
    strict = any(isinstance(m, Strict) and m.strict for m in meta)
    if ann in (bool, int):
        return [] if strict else [f"{path}: lax {ann.__name__}"]
    if ann is float:
        finite = any(getattr(m, "allow_inf_nan", True) is False for m in meta)
        return [] if strict and finite else [f"{path}: lax or non-finite float"]
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        if ann in seen:
            return []
        seen.add(ann)
        return [
            p
            for name, field in ann.model_fields.items()
            for p in _problems(field.annotation, tuple(field.metadata), f"{path}.{name}", seen)
        ]
    return [f"{path}: unvetted leaf {ann!r}"]


@pytest.mark.parametrize("model", [RiskUtilityReport, RiskUtilityReportRead])
def test_every_leaf_is_value_free(model: type[BaseModel]) -> None:
    assert _problems(model, (), model.__name__, set()) == []


def test_the_walk_sees_a_free_string() -> None:
    class Leaky(BaseModel):
        note: str

    assert _problems(Leaky, (), "Leaky", set()) == ["Leaky.note: unvetted leaf <class 'str'>"]


def test_the_value_free_kinds_are_pinned() -> None:
    assert _named_kinds() == {"schema_tag", "column_name"}


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "age": [21, 22, 23, 24, 31, 33, 35, 38],
            "zip": [ZIP_SENTINEL, ZIP_SENTINEL, "122", "122", "130", "131", ZIP_SENTINEL, "131"],
            "dx": [SENTINEL, "b", SENTINEL, "c", "b", SENTINEL, "c", "c"],
        }
    )


def _assert_clean(text: str) -> None:
    assert SENTINEL not in text
    assert ZIP_SENTINEL not in text


def test_document_and_repr_hold_no_cell() -> None:
    frame = _frame()
    lattice, result, released = release(frame, (KAnonymity(2), EntropyLDiversity("dx", 1)), 0.25)
    report = build_risk_utility_report(
        released, lattice, result, risk_threshold=0.2, population=pd.concat([frame, frame])
    )
    _assert_clean(json.dumps(report.to_payload()))
    _assert_clean(repr(report))
    _assert_clean(str(report))


def _raises(call: Any, kind: Any = (TypeError, ValueError)) -> None:
    with pytest.raises(kind) as caught:
        call()
    _assert_clean(str(caught.value))
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_errors_hold_no_cell() -> None:
    frame = _frame()
    lattice, result, released = release(frame, (KAnonymity(2),), 0.25)
    build = lambda **kw: build_risk_utility_report(released, lattice, result, risk_threshold=0.2, **kw)
    # population missing the sentinel class
    _raises(lambda: build(population=frame[frame["zip"] != ZIP_SENTINEL]))
    # population with another type family in a quasi-identifier
    widened = frame.assign(age=frame["age"].astype(float))
    _raises(lambda: build(population=widened))
    # population value a hierarchy does not cover
    _raises(lambda: build(population=frame.assign(zip=[SENTINEL] * len(frame))))
    # an edited released table
    edited = released.table.copy()
    edited["dx"] = SENTINEL + "x"
    from decoy_engine.anonymize.search import Released

    forged = Released(
        table=edited,
        suppressed=released.suppressed,
        generalized_including_suppressed=released.generalized_including_suppressed,
    )
    _raises(lambda: build_risk_utility_report(forged, lattice, result, risk_threshold=0.2))


@pytest.mark.parametrize("name", ["zip/code", "a\nb", "x" * 129])
def test_a_column_name_that_cannot_be_written_is_refused_without_the_name(name: str) -> None:
    frame = _frame().rename(columns={"zip": name})
    lattice = Lattice({"age": interval_hierarchy((5, 10)), name: prefix_hierarchy(3, keep=(2, 1))})
    _, result, released = release(frame, (KAnonymity(2),), 0.25, the_lattice=lattice)
    with pytest.raises(ValueError, match="^a column name cannot be written to the report$") as caught:
        build_risk_utility_report(released, lattice, result, risk_threshold=0.2)
    assert caught.value.__context__ is None


def test_a_sensitive_column_name_that_cannot_be_written_is_refused() -> None:
    frame = _frame().rename(columns={"dx": "dx://x"})
    lattice, result, released = release(frame, (EntropyLDiversity("dx://x", 1),), 0.25)
    with pytest.raises(ValueError, match="^a column name cannot be written to the report$"):
        build_risk_utility_report(released, lattice, result, risk_threshold=0.2)
