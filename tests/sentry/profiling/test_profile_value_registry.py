"""Every field reachable from `ColumnProfile` is classified once, and every value rule bites.

The walk is the same one the redactor uses, so a new value-bearing field or a new nested
type cannot reach a saved scan document without a decision in `_value_redaction.py`.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, get_type_hints

import pytest

from decoy_engine.detection.types import DetectorMatch
from decoy_engine.profiling import _value_redaction, profile_document
from decoy_engine.profiling._value_redaction import (
    VALUE_FIELDS,
    VALUE_FREE_FIELDS,
    _walk,
    profile_types,
)
from decoy_engine.profiling.types import (
    ColumnProfile,
    Distribution,
    FieldStats,
    SentinelFlag,
    TopValue,
)


def _reached_pairs() -> set[tuple[str, str]]:
    return {
        (cls.__name__, f.name) for cls in profile_types().values() for f in dataclasses.fields(cls)
    }


def test_registries_are_disjoint() -> None:
    assert set(VALUE_FIELDS) & VALUE_FREE_FIELDS == set()


def test_every_reached_field_is_classified_and_every_entry_is_reached() -> None:
    reached = _reached_pairs()
    assert reached == set(VALUE_FIELDS) | VALUE_FREE_FIELDS


def test_the_walk_reaches_the_nested_types_by_path() -> None:
    paths = {path: cls.__name__ for path, cls in profile_types().items()}
    assert paths[("fields", "*")] == "FieldStats"
    assert paths[("fields", "*", "top_values", "*")] == "TopValue"
    assert paths[("fields", "*", "distribution")] == "Distribution"
    assert paths[("fields", "*", "sentinels", "*")] == "SentinelFlag"
    assert paths[("fields", "*", "detector_matches", "*")] == "DetectorMatch"


def _field_stats_with(name: str, hint: Any) -> type:
    """`FieldStats` with one extra field, under the same class name."""
    hints = get_type_hints(FieldStats)
    return dataclasses.make_dataclass(
        "FieldStats",
        [
            *(
                (f.name, hints[f.name], dataclasses.field(default=None))
                for f in dataclasses.fields(FieldStats)
            ),
            (name, hint, dataclasses.field(default=None)),
        ],
    )


def _root(*extra: tuple[str, Any]) -> type:
    return dataclasses.make_dataclass(
        "ColumnProfile",
        [(name, hint, dataclasses.field(default=None)) for name, hint in extra],
    )


def test_an_unclassified_field_fails_the_walk() -> None:
    _, errors = _walk(_root(("fields", list[_field_stats_with("extra", str)])))
    assert errors == ["unclassified FieldStats.extra"]


def test_a_value_type_outside_fields_fails_the_walk() -> None:
    _, errors = _walk(_root(("fields", list[FieldStats]), ("sample_top", list[TopValue])))
    assert errors == [
        "unclassified ColumnProfile.sample_top",
        "value type TopValue at sample_top.*",
    ]


def test_a_dataclass_in_an_unknown_container_fails_the_walk() -> None:
    hint = dict[str, TopValue]
    _, errors = _walk(_root(("fields", list[_field_stats_with("examples", hint)])))
    assert errors == [
        "unclassified FieldStats.examples",
        "unsupported container FieldStats.examples",
    ]


def test_two_classes_with_one_name_fail_the_walk() -> None:
    impostor = dataclasses.make_dataclass("TopValue", [("note", str)])
    _, errors = _walk(_root(("fields", list[FieldStats]), ("other", impostor)))
    assert "two classes named TopValue" in errors


def _profile_with(type_name: str, field: str, canary: str) -> ColumnProfile:
    fs = FieldStats(
        name="c", inferred_type="string", dtype_raw="object", row_count=1, null_count=0,
        null_rate=0.0, distinct_count=1, unique_rate=1.0, is_likely_unique=True,
        top_values=[TopValue(value="v", count=1, pct=100.0)],
        detector_matches=[DetectorMatch(detector_id="ssn", match_rate=1.0)],
        sentinels=[SentinelFlag(kind="string_sentinel", value="v", count=1, note="n")],
        distribution=Distribution(kind="categorical", data=[100.0], labels=["v"]),
    )  # fmt: skip
    owners: dict[str, Any] = {
        "FieldStats": fs,
        "TopValue": fs.top_values[0],
        "DetectorMatch": fs.detector_matches[0],
        "SentinelFlag": fs.sentinels[0],
        "Distribution": fs.distribution,
    }
    owner = owners[type_name]
    slot_is_list = isinstance(getattr(owner, field), list)
    setattr(owner, field, [canary] if slot_is_list else canary)
    return ColumnProfile(source_label="t.csv", row_count=1, sample_strategy="full", fields=[fs])


@pytest.mark.parametrize("entry", sorted(VALUE_FIELDS), ids="/".join)
def test_every_value_rule_is_applied(entry: tuple[str, str]) -> None:
    canary = f"CANARY-{entry[0]}-{entry[1]}"
    profile = _profile_with(*entry, canary)
    assert canary in json.dumps(profile.to_dict())
    assert canary not in json.dumps(profile_document(profile))
    assert canary in json.dumps(profile_document(profile, include_values=True))


def test_profile_types_refuses_any_walk_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_value_redaction, "_walk", lambda root: ({}, ["unclassified X.y"]))
    with pytest.raises(TypeError, match=r"cannot cover: \['unclassified X.y'\]"):
        profile_types.__wrapped__()


def test_a_value_free_field_the_shape_check_cannot_verify_fails_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    free = _value_redaction.VALUE_FREE_FIELDS | {("FieldStats", "extra")}
    monkeypatch.setattr(_value_redaction, "VALUE_FREE_FIELDS", free)
    _, errors = _walk(_root(("fields", list[_field_stats_with("extra", dict[str, int])])))
    assert errors == ["unchecked shape FieldStats.extra"]
