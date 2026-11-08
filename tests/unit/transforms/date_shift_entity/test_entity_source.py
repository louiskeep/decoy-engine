"""Entity source encoding, domain separation, option parsing and the near-miss rule."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from decoy_engine.determinism import derive
from decoy_engine.kernel import canonicalize_derive_source
from decoy_engine.transforms.dates.entity_offset import (
    ENTITY_OFFSET_TAG,
    Anchor,
    AnchorDomain,
    EntityKeyConfig,
    EntityKeyFault,
    FaultKind,
    UnsupportedEntityValue,
    date_shift_anchor,
    domain_separated_source,
    entity_column,
    entity_key_present,
    entity_source,
    near_miss_entity_keys,
    parse_entity_key,
)
from tests.unit.transforms.date_shift_entity._support import NS, SEED

LABEL = b"\xffdate-shift-entity/v1\x00"


def test_vector_offset_matches_a_hand_computation() -> None:
    source = entity_source("p1")
    assert source == LABEL + b"p1"
    digest = derive(SEED, NS, source)
    # Pinned so a change to the label, envelope or reduction cannot pass silently.
    assert digest.hex()[:16] == "17042bb9e401c466"
    assert -180 + int.from_bytes(digest[:8], "big") % 361 == -175


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (123, b"123"),
        (np.int64(123), b"123"),
        ("123", b"123"),
        (Decimal("123"), b"123"),
        (Decimal("123.0"), b"123"),
        (Decimal("1E+2"), b"100"),
        (Decimal("12.5"), b"12.5"),
        (-7, b"-7"),
        ("café", "café".encode()),  # NFC
        (" 123", b" 123"),
        (
            uuid.UUID("1a2b3c4d-1234-5678-9abc-def012345678"),
            b"1a2b3c4d-1234-5678-9abc-def012345678",
        ),
        ("1a2b3c4d-1234-5678-9abc-def012345678", b"1a2b3c4d-1234-5678-9abc-def012345678"),
        (date(2020, 1, 2), b"2020-01-02"),
    ],
)
def test_entity_text(value: object, text: bytes) -> None:
    assert entity_source(value) == LABEL + text


def test_int_string_decimal_agree_and_upper_case_uuid_does_not() -> None:
    assert entity_source(123) == entity_source("123") == entity_source(Decimal("123.0"))
    u = uuid.UUID("1a2b3c4d-1234-5678-9abc-def012345678")
    assert entity_source(u) == entity_source(str(u))
    assert entity_source(u) != entity_source(str(u).upper())


@pytest.mark.parametrize("value", [None, np.nan, pd.NA, pd.NaT, "", "   ", "\t"])
def test_no_entity(value: object) -> None:
    assert entity_source(value) is None


@pytest.mark.parametrize(
    "value",
    [
        True,
        np.bool_(False),
        1.5,
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("sNaN"),
        datetime(2020, 1, 1),
        datetime(2020, 1, 1, tzinfo=timezone.utc),
        pd.Timestamp("2020-01-01"),
        b"123",
    ],
)
def test_unsupported_entity_values(value: object) -> None:
    got = entity_source(value)
    assert isinstance(got, UnsupportedEntityValue)
    assert repr(value) not in got.type_name


def test_label_is_structurally_out_of_reach() -> None:
    assert LABEL[0] == 0xFF and len(LABEL) > 1
    with pytest.raises(UnicodeDecodeError):
        LABEL.decode("utf-8")
    assert domain_separated_source(ENTITY_OFFSET_TAG, b"x") == LABEL + b"x"
    for bad in (b"", b"a\x00b"):
        with pytest.raises(ValueError, match="domain tag"):
            domain_separated_source(bad, b"x")


_CELLS = st.one_of(
    st.text(),
    st.integers(),
    st.booleans(),
    st.decimals(allow_nan=False, allow_infinity=False),
    st.dates(),
    st.datetimes(timezones=st.just(timezone.utc)),
    st.binary(),
)


@settings(max_examples=400, deadline=None, derandomize=True)
@given(_CELLS)
def test_no_canonical_cell_starts_with_the_domain_byte(cell: object) -> None:
    assert not canonicalize_derive_source(cell).startswith(b"\xff")


def test_parse_entity_key() -> None:
    assert parse_entity_key({}) is None
    assert parse_entity_key({"entity_key": None}) is None
    assert parse_entity_key({"entity_key": "pid", "min_days": 9, "max_days": -9}) == (
        EntityKeyConfig("pid", -9, 9)
    )
    assert parse_entity_key({"entity_key": "pid"}) == EntityKeyConfig("pid", -365, 365)
    cases: list[tuple[dict[str, Any], FaultKind, str]] = [
        ({"entity_key": 123}, FaultKind.KEY_INVALID, "entity_key"),
        ({"entity_key": "  "}, FaultKind.KEY_INVALID, "entity_key"),
        ({"entity_key": False}, FaultKind.KEY_INVALID, "entity_key"),
        ({"entity_key": "pid", "group_by": "g"}, FaultKind.WITH_GROUP_BY, "group_by"),
        (
            {"entity_key": "pid", "preserve_values": ["9999-12-31"]},
            FaultKind.WITH_PRESERVE_VALUES,
            "preserve_values",
        ),
        ({"entity_key": "pid", "min_days": True}, FaultKind.BOUND_INVALID, "min_days"),
        ({"entity_key": "pid", "min_days": "5"}, FaultKind.BOUND_INVALID, "min_days"),
        ({"entity_key": "pid", "max_days": 5.0}, FaultKind.BOUND_INVALID, "max_days"),
        ({"entity_key": "pid", "max_days": 106_752}, FaultKind.BOUND_INVALID, "max_days"),
        ({"entity_key": "pid", "min_days": -106_752}, FaultKind.BOUND_INVALID, "min_days"),
    ]
    for cfg, kind, option in cases:
        assert parse_entity_key(cfg) == EntityKeyFault(kind, option), cfg
    assert isinstance(parse_entity_key({"entity_key": "p", "max_days": 106_751}), EntityKeyConfig)


def test_presence_and_anchor() -> None:
    assert entity_key_present({"entity_key": 123})
    assert entity_key_present({"entity_key": ""})
    assert not entity_key_present({"entity_key": None})
    assert not entity_key_present({})
    assert date_shift_anchor({"entity_key": "pid"}) == Anchor("pid", AnchorDomain.ENTITY)
    assert date_shift_anchor({"group_by": "g"}) == Anchor("g", AnchorDomain.VALUE_KEYED_GROUP)
    assert date_shift_anchor({}) is None
    assert entity_column({"provider_config": {"entity_key": "pid"}}) == "pid"
    assert entity_column({"provider_config": {"entity_key": 1}}) is None


@pytest.mark.parametrize(
    "key",
    [
        "entity-key",
        "-entity-key",
        "entity--key",
        "Entity__Key",
        "EntityKey",
        "entitykey",
        "entity_kye",
        "enity_key",
        "entity_keys",
    ],
)
def test_near_miss_keys(key: str) -> None:
    assert near_miss_entity_keys({key: "pid", "min_days": 1}) == (key,)


@pytest.mark.parametrize(
    "key", ["entity_key", "entity_keys_note", "group_by", "identity_key", "entity"]
)
def test_not_near_misses(key: str) -> None:
    assert near_miss_entity_keys({key: "pid"}) == ()
