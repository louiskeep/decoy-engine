"""Property tests over the canonical form: one value, one byte string."""

from __future__ import annotations

import json
import random
import re

from hypothesis import given, settings
from hypothesis import strategies as hst

from decoy_engine.signed_evidence import _canonical as can
from decoy_engine.signed_evidence import _strict_json as sj

_TEXT = hst.text(
    alphabet=hst.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=12
)
_SCALARS = hst.one_of(
    hst.none(), hst.booleans(), hst.integers(-sj.MAX_SAFE_INT, sj.MAX_SAFE_INT), _TEXT
)
JSON = hst.recursive(
    _SCALARS,
    lambda inner: hst.one_of(
        hst.lists(inner, max_size=4), hst.dictionaries(_TEXT, inner, max_size=4)
    ),
    max_leaves=12,
)


def _shuffled(node, rng: random.Random):
    if isinstance(node, dict):
        items = [(k, _shuffled(v, rng)) for k, v in node.items()]
        rng.shuffle(items)
        return dict(items)
    if isinstance(node, list):
        return [_shuffled(v, rng) for v in node]
    return node


@settings(max_examples=300, deadline=None)
@given(JSON, hst.integers(0, 2**32))
def test_member_order_never_changes_the_canonical_bytes(doc, seed) -> None:
    assert can.canonical_bytes(doc) == can.canonical_bytes(_shuffled(doc, random.Random(seed)))


@settings(max_examples=300, deadline=None)
@given(JSON)
def test_strict_parse_of_canonical_bytes_round_trips_and_is_idempotent(doc) -> None:
    raw = can.canonical_bytes(doc)
    assert sj.parse_strict(raw) == doc
    assert can.canonical_bytes(sj.parse_strict(raw)) == raw


@settings(max_examples=200, deadline=None)
@given(hst.dictionaries(_TEXT, hst.integers(0, 9), min_size=2, max_size=8))
def test_object_keys_are_ordered_by_utf16_code_units(doc) -> None:
    pairs = json.loads(can.canonical_bytes(doc), object_pairs_hook=list)
    keys = [k for k, _ in pairs]
    assert keys == sorted(keys, key=lambda k: k.encode("utf-16-be"))


@settings(max_examples=200, deadline=None)
@given(JSON)
def test_canonical_form_has_no_insignificant_whitespace_or_bom(doc) -> None:
    raw = can.canonical_bytes(doc)
    assert not raw.startswith(b"\xef\xbb\xbf")
    without_strings = re.sub(r'"(?:[^"\\]|\\.)*"', '""', raw.decode("utf-8"))
    assert not re.search(r"\s", without_strings), "whitespace outside a string literal"
