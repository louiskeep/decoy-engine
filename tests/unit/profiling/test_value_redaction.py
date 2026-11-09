"""Saved profile documents carry no cell value of a value-sensitive column by default.

The fixture puts a distinct canary in every `VALUE_FIELDS` slot, and the precondition
fails if the profiler stopped filling one, so a green run cannot come from an empty slot.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any, get_type_hints

import pandas as pd
import pytest

from decoy_engine.profiling import (
    is_value_sensitive,
    profile_columns,
    profile_document,
    redact_profile_dict,
)
from decoy_engine.profiling._value_redaction import (
    POLICY_RAW,
    POLICY_REDACTED,
    REDACTED,
    VALUE_FIELDS,
    VALUE_FREE_FIELDS,
    Rule,
    _conforms,
    profile_types,
)

_N = 20
_FIRST = [
    "Abel", "Brina", "Cosmo", "Delia", "Ewan", "Fiora", "Gideon", "Hester", "Ivo", "Juno",
    "Kester", "Lorna", "Milo", "Nerys", "Otto", "Perpetua", "Quincy", "Rhoda", "Silas", "Tamsin",
]  # fmt: skip
_SSNS = [f"{101 + i:03d}-{23 + i:02d}-{4501 + i:04d}" for i in range(_N - 1)]

CANARIES = (
    "Quixwort",
    *_SSNS,
    "SSNMISS",
    "31337",
    "98765",
    "50155.5875",  # the balance mean
    "1987-03",
    "1991-06",
    "1900-01-01",
    "NOTADATE",
)


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "full_name": [f"{f} Quixwort" for f in _FIRST],
            "ssn": [*_SSNS, "SSNMISS-CANARY"],
            "balance": [31337.25, *[40000.5 + 1000 * i for i in range(_N - 2)], 98765.5],
            "visit_date": [
                *[f"1987-03-{d:02d}" for d in range(1, _N - 1)],
                "1900-01-01",
                "NOTADATE-CANARY",
            ],
            "admitted": pd.date_range("1991-06-01", periods=_N, freq="D"),
        }
    )


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    return profile_columns(_frame(), "patients.csv").to_dict()


def _at(node: Any, steps: tuple[str, ...]) -> Iterator[Any]:
    if node is None:
        return
    if not steps:
        yield node
    elif steps[0] == "*":
        for item in node:
            yield from _at(item, steps[1:])
    else:
        yield from _at(node.get(steps[0]), steps[1:])


def slot_values(doc: dict[str, Any], type_name: str, field: str) -> list[Any]:
    """Every value stored in `type_name.field` anywhere in a well-formed profile document."""
    return [
        container[field]
        for path, cls in profile_types().items()
        if cls.__name__ == type_name
        for container in _at(doc, path)
    ]


def _leaks(doc: dict[str, Any]) -> list[str]:
    text = json.dumps(doc)
    return [c for c in CANARIES if c in text]


def test_every_fixture_column_is_value_sensitive(raw: dict[str, Any]) -> None:
    assert [f["name"] for f in raw["fields"] if not is_value_sensitive(f)] == []


@pytest.mark.parametrize("slot", sorted(VALUE_FIELDS), ids="/".join)
def test_fixture_fills_every_value_slot_with_a_canary(
    raw: dict[str, Any], slot: tuple[str, str]
) -> None:
    filled = [json.dumps(v) for v in slot_values(raw, *slot)]
    assert any(c in text for text in filled for c in CANARIES), f"no canary reached {slot}"


def test_profile_document_removes_every_canary(raw: dict[str, Any]) -> None:
    profile = profile_columns(_frame(), "patients.csv")
    doc = profile_document(profile)
    assert _leaks(doc) == []
    assert doc["value_policy"] == POLICY_REDACTED
    assert doc["redacted_columns"] == ["full_name", "ssn", "balance", "visit_date", "admitted"]


def test_include_values_is_to_dict_plus_the_two_keys(raw: dict[str, Any]) -> None:
    profile = profile_columns(_frame(), "patients.csv")
    doc = profile_document(profile, include_values=True)
    assert _leaks(doc) == _leaks(profile.to_dict())
    expected = {**profile.to_dict(), "value_policy": POLICY_RAW}
    expected["redacted_columns"] = [f["name"] for f in expected["fields"]]
    assert doc == expected


def test_a_column_that_is_not_value_sensitive_keeps_its_values() -> None:
    frame = pd.DataFrame(
        {"status": ["active", "inactive"] * 10, "full_name": [f"{f} Quixwort" for f in _FIRST]}
    )
    doc = profile_document(profile_columns(frame, "members.csv"))
    status = next(f for f in doc["fields"] if f["name"] == "status")
    assert not is_value_sensitive(status)
    assert {t["value"] for t in status["top_values"]} == {"active", "inactive"}
    assert status["mode_value"] in {"active", "inactive"}
    assert sorted(status["distribution"]["labels"]) == ["active", "inactive"]
    assert doc["redacted_columns"] == ["full_name"]


def test_redaction_is_a_fixed_point_and_never_mutates_its_input(raw: dict[str, Any]) -> None:
    before = copy.deepcopy(raw)
    once = redact_profile_dict(raw)
    assert raw == before
    assert redact_profile_dict(once) == once


def test_a_forged_marker_over_raw_values_is_still_redacted(raw: dict[str, Any]) -> None:
    forged = {**raw, "value_policy": POLICY_REDACTED}
    assert _leaks(redact_profile_dict(forged)) == []


def test_include_values_keeps_an_existing_marker(raw: dict[str, Any]) -> None:
    redacted = redact_profile_dict(raw)
    shown = redact_profile_dict(redacted, include_values=True)
    assert shown["value_policy"] == POLICY_REDACTED
    assert redact_profile_dict(raw, include_values=True)["value_policy"] == POLICY_RAW


def test_redaction_keeps_shapes_lengths_and_counts(raw: dict[str, Any]) -> None:
    doc = redact_profile_dict(raw)
    kinds = set()
    for before, after in zip(raw["fields"], doc["fields"], strict=True):
        dist = after["distribution"]
        kinds.add(dist["kind"])
        assert len(dist["labels"]) == len(dist["data"]) == len(before["distribution"]["data"])
        assert dist["data"] == before["distribution"]["data"]
        assert [(t["count"], t["pct"]) for t in after["top_values"]] == [
            (t["count"], t["pct"]) for t in before["top_values"]
        ]
        assert all(t["value"] == REDACTED for t in after["top_values"])
    assert {"categorical", "numeric", "date"} <= kinds


def test_sentinel_value_and_note_are_both_replaced() -> None:
    frame = pd.DataFrame(
        {
            "member_ssn": [*_SSNS, "tbd"],
            "balance": [-999.0, *[40000.5 + 1000 * i for i in range(_N - 1)]],
            "visit_date": [*[f"1987-03-{d:02d}" for d in range(1, _N)], "1900-01-01"],
        }
    )
    profile = profile_columns(frame, "visits.csv")
    raw_flags = {(f.name, s.kind): s for f in profile.fields for s in f.sentinels}
    assert {k for _, k in raw_flags} == {"string_sentinel", "numeric_sentinel", "date_sentinel"}
    for flag in raw_flags.values():
        assert flag.value in flag.note or flag.kind == "numeric_sentinel"
    redacted = [s for f in profile_document(profile)["fields"] for s in f["sentinels"]]
    assert {(s["value"], s["note"]) for s in redacted} == {(REDACTED, REDACTED)}
    kept = [
        s for f in profile_document(profile, include_values=True)["fields"] for s in f["sentinels"]
    ]
    assert sorted((s["value"], s["note"]) for s in kept) == sorted(
        (s.value, s.note) for s in raw_flags.values()
    )


def test_a_loaded_sentinel_is_replaced_without_a_fixed_value_list() -> None:
    field = {
        "name": "code",
        "detector_matches": [],
        "pii_score": 0.0,
        "is_likely_unique": True,
        "sentinels": [
            {"kind": "string_sentinel", "value": "CANARY-S", "count": 1, "note": "has CANARY-S"}
        ],
    }
    doc = redact_profile_dict({"fields": [field]})
    assert doc["fields"][0]["sentinels"][0] == {
        "kind": "string_sentinel", "value": REDACTED, "count": 1, "note": REDACTED,
    }  # fmt: skip


_SAFE = {"detector_matches": [], "pii_score": 0.0, "is_likely_unique": False}


@pytest.mark.parametrize(
    "override",
    [
        {"detector_matches": {}},
        {"detector_matches": None},
        {"pii_score": False},
        {"pii_score": 1},
        {"pii_score": float("nan")},
        {"pii_score": float("inf")},
        {"pii_score": 1.5},
        {"is_likely_unique": 0},
        {"is_likely_unique": None},
    ],
    ids=repr,
)
def test_an_ill_typed_predicate_input_fails_closed(override: dict[str, Any]) -> None:
    assert not is_value_sensitive(_SAFE)
    assert is_value_sensitive({**_SAFE, **override})


@pytest.mark.parametrize("missing", sorted(_SAFE))
def test_a_missing_predicate_input_fails_closed(missing: str) -> None:
    assert is_value_sensitive({k: v for k, v in _SAFE.items() if k != missing})


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ({"top_values": "CANARY"}, REDACTED),
        (
            {"top_values": ["CANARY", {"value": "CANARY", "count": 1}]},
            [REDACTED, {"value": REDACTED, "count": 1}],
        ),
        ({"distribution": "CANARY"}, REDACTED),
        (
            {"distribution": {"kind": "categorical", "labels": "CANARY"}},
            {"kind": "categorical", "labels": REDACTED},
        ),
        ({"distribution": {"labels": ["CANARY", 7]}}, {"labels": [REDACTED, REDACTED]}),
        ({"sentinels": [None, "CANARY"]}, [None, REDACTED]),
        ({"mode_value": {"nested": "CANARY"}}, None),
    ],
    ids=lambda v: json.dumps(v)[:40],
)
def test_off_shape_value_locations_become_the_placeholder(
    field: dict[str, Any], expected: Any
) -> None:
    doc = redact_profile_dict({"fields": [{"name": "x", **field}]})
    (key,) = field
    assert doc["fields"][0][key] == expected
    assert "CANARY" not in json.dumps(doc)


def test_a_non_object_field_entry_is_replaced() -> None:
    doc = redact_profile_dict({"fields": ["CANARY", 3]})
    assert doc["fields"] == [REDACTED, REDACTED]
    assert doc["redacted_columns"] == [REDACTED, REDACTED]


def test_a_non_list_fields_value_is_replaced_and_counted() -> None:
    doc = redact_profile_dict({"fields": {"name": "CANARY"}})
    assert doc["fields"] == REDACTED
    assert doc["redacted_columns"] == [REDACTED]


_SENSITIVE = {"detector_matches": [], "pii_score": 1.0, "is_likely_unique": True}


def test_an_unknown_key_in_a_sensitive_column_is_replaced() -> None:
    field = {
        "name": "ssn",
        **_SENSITIVE,
        "raw_sample": "CANARY",
        "top_values": [{"value": "CANARY", "count": 1, "pct": 1.0, "first_seen": "CANARY"}],
    }
    doc = redact_profile_dict({"fields": [field]})
    out = doc["fields"][0]
    assert out["raw_sample"] == REDACTED
    assert out["top_values"] == [
        {"value": REDACTED, "count": 1, "pct": 1.0, "first_seen": REDACTED}
    ]
    assert "CANARY" not in json.dumps(doc)


def _every_owner() -> dict[str, Any]:
    field = {
        "name": "ssn",
        **_SENSITIVE,
        "top_values": [{"value": "v", "count": 1, "pct": 1.0}],
        "detector_matches": [{"detector_id": "ssn", "match_rate": 1.0}],
        "sentinels": [{"kind": "k", "value": "v", "count": 1, "note": "n"}],
        "distribution": {"kind": "categorical", "data": [1.0], "labels": ["v"]},
        "detection_trail": [{"signal": "s"}],
    }
    return {"source_label": "t.csv", "fields": [field]}


_OWNER_PATHS: dict[str, tuple[Any, ...]] = {
    "ColumnProfile": (),
    "FieldStats": ("fields", 0),
    "TopValue": ("fields", 0, "top_values", 0),
    "DetectorMatch": ("fields", 0, "detector_matches", 0),
    "SentinelFlag": ("fields", 0, "sentinels", 0),
    "Distribution": ("fields", 0, "distribution"),
    "DetectionSignal": ("fields", 0, "detection_trail", 0),
}


@pytest.mark.parametrize("slot", sorted(VALUE_FREE_FIELDS), ids="/".join)
def test_an_ill_typed_value_free_slot_is_replaced(slot: tuple[str, str]) -> None:
    # A hand-edited file can put a cell in any slot; only the declared type keeps it.
    type_name, key = slot
    canary = f"CANARY-{type_name}-{key}"
    classes = {cls.__name__: cls for cls in profile_types().values()}
    hint = get_type_hints(classes[type_name])[key]
    value: Any = {"x": canary} if _conforms(canary, hint) else canary
    doc = _every_owner()
    owner: Any = doc
    for step in _OWNER_PATHS[type_name]:
        owner = owner[step]
    owner[key] = value
    assert canary in json.dumps(doc)
    assert canary not in json.dumps(redact_profile_dict(doc))


def _float_slots() -> list[tuple[str, str]]:
    classes = {cls.__name__: cls for cls in profile_types().values()}
    out = []
    for type_name, key in sorted(VALUE_FREE_FIELDS):
        hint = get_type_hints(classes[type_name])[key]
        takes_floats = _conforms(0.5, hint) or _conforms([0.5], hint)
        if takes_floats and not (_conforms("s", hint) or _conforms(["s"], hint)):
            out.append((type_name, key))
    return out


@pytest.mark.parametrize("number", [31337, float("nan"), float("inf")], ids=repr)
@pytest.mark.parametrize("slot", _float_slots(), ids="/".join)
def test_a_number_the_profiler_never_writes_in_a_float_slot_is_replaced(
    slot: tuple[str, str], number: float
) -> None:
    # 31337 could be an id or a balance; a float slot only ever receives finite floats from the profiler.
    type_name, key = slot
    doc = _every_owner()
    owner: Any = doc
    for step in _OWNER_PATHS[type_name]:
        owner = owner[step]
    owner[key] = [number] if isinstance(owner.get(key), list) else number
    out: Any = redact_profile_dict(doc)
    for step in _OWNER_PATHS[type_name]:
        out = out[step]
    assert out[key] == REDACTED


def _real_frames() -> list[pd.DataFrame]:
    n = 24
    return [
        _frame(),
        pd.DataFrame(
            {
                "email": [f"user{i}@example.com" for i in range(n)],
                "account": list(range(100_000, 100_000 + n)),
                "score": [i / 7 if i % 5 else None for i in range(n)],
                "blank": [None] * n,
                "mixed": [i if i % 2 else f"x{i}" for i in range(n)],
                "when": pd.date_range("1991-06-01", periods=n, freq="D"),
            }
        ),
    ]


@pytest.mark.parametrize("index", range(2))
def test_a_real_profile_loses_only_value_slots(index: int) -> None:
    # Guards the float rule above against redacting what the profiler itself writes.
    value_keys = {key for _, key in VALUE_FIELDS}
    doc = profile_document(profile_columns(_real_frames()[index], "real.csv"))
    assert doc["redacted_columns"]
    stack: list[Any] = [doc]
    while stack:
        node = stack.pop()
        items = (
            node.items()
            if isinstance(node, dict)
            else enumerate(node)
            if isinstance(node, list)
            else ()
        )
        for key, child in items:
            if child == REDACTED or (isinstance(child, list) and REDACTED in child):
                assert key in value_keys or isinstance(key, int), key
            stack.append(child)


def test_a_well_typed_sensitive_column_keeps_its_value_free_slots() -> None:
    out = redact_profile_dict(_every_owner())["fields"][0]
    assert out["pii_score"] == 1.0 and out["is_likely_unique"] is True
    assert out["top_values"][0]["count"] == 1 and out["detector_matches"][0]["match_rate"] == 1.0
    assert out["distribution"]["data"] == [1.0] and out["detection_trail"] == [{"signal": "s"}]


def test_an_unknown_key_outside_a_sensitive_column_follows_its_scope() -> None:
    doc = {"fields": [{"name": "status", **_SAFE, "note": "kept"}], "dump": ["CANARY"]}
    redacted = redact_profile_dict(doc)
    assert redacted["fields"][0]["note"] == "kept"
    assert redacted["dump"] == REDACTED
    assert redact_profile_dict(doc, include_values=True)["dump"] == ["CANARY"]


@pytest.mark.parametrize("flag", ["false", 1, 0, None, "True"], ids=repr)
def test_include_values_must_be_a_bool(flag: Any) -> None:
    with pytest.raises(TypeError, match="include_values must be a bool"):
        redact_profile_dict({"fields": []}, include_values=flag)


def _nested(depth: int) -> list[Any]:
    root: list[Any] = []
    node = root
    for _ in range(depth):
        child: list[Any] = []
        node.append(child)
        node = child
    return root


def _depth(node: list[Any]) -> int:
    depth = 0
    while node:
        node, depth = node[0], depth + 1
    return depth


def test_a_deeply_nested_document_is_copied_without_recursion() -> None:
    trail = _nested(5000)
    doc = {"fields": [{"name": "status", **_SAFE, "detection_trail": trail}]}
    out = redact_profile_dict(doc)
    copied = out["fields"][0]["detection_trail"]
    assert copied is not trail
    assert _depth(copied) == 5000
    assert _depth(trail) == 5000


@pytest.mark.parametrize(
    "inputs",
    [
        {"detector_matches": [{}], "pii_score": 0.0, "is_likely_unique": False},
        {"detector_matches": [], "pii_score": 0.6, "is_likely_unique": False},
        {"detector_matches": [], "pii_score": 0.0, "is_likely_unique": True},
    ],
    ids=["detector-only", "threshold-only", "unique-only"],
)
def test_each_predicate_branch_alone_is_sensitive(inputs: dict[str, Any]) -> None:
    assert is_value_sensitive(inputs)


_SHAPE = {
    Rule.NULL: lambda v: v is None,
    Rule.EMPTY_LIST: lambda v: v == [],
    Rule.PLACEHOLDER: lambda v: v == REDACTED,
    Rule.PLACEHOLDER_EACH: lambda v: isinstance(v, list) and set(v) <= {REDACTED},
}


@pytest.mark.parametrize("slot", sorted(VALUE_FIELDS), ids="/".join)
def test_each_value_slot_takes_its_rule_shape(raw: dict[str, Any], slot: tuple[str, str]) -> None:
    after = slot_values(redact_profile_dict(raw), *slot)
    assert after, f"no {slot} slot in the fixture"
    assert all(v is None or _SHAPE[VALUE_FIELDS[slot]](v) for v in after)
