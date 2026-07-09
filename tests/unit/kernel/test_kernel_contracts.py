"""Option 4 Phase 0 kernel contract tests."""

from __future__ import annotations

import math

import pyarrow as pa
import pytest

from decoy_engine.execution._fk_keys import NULL_FK_KEY, fk_key_value
from decoy_engine.generation.pool._errors import GenerationError
from decoy_engine.kernel import canonicalize_derive_source, hash_array, redact_array, truncate_array

_SEED = (0x55).to_bytes(8, "big")


def test_hash_array_matches_frozen_pandas_snapshot() -> None:
    text_values = pa.array(["alice", "bob", "", None], from_pandas=True)
    int_values = pa.array([42, None], from_pandas=True)
    bool_values = pa.array([True, None], from_pandas=True)

    assert hash_array(text_values, seed=_SEED, namespace="ids").to_pylist() == [
        "7ad63ec83b23d0e64567f43828bbed7f9cb2fed9dcea5ccae673ea1cbdaf9d0a",
        "6e7264e5d6614602464575d589ca17f2bae8cc76bf3f93944c6bdb75ea2b764a",
        "9a92b3b1ec4f7ba9b5ca6dcdb98188a1af9dde3543136c471201e083162b4041",
        None,
    ]
    assert hash_array(int_values, seed=_SEED, namespace="ids").to_pylist() == [
        "9ceda639d8bcb053d74c44cbbf5355148abdfb695e7c2c5da19139f45defd2d7",
        None,
    ]
    assert hash_array(bool_values, seed=_SEED, namespace="ids").to_pylist() == [
        "8755a01ac28bc82f33202d957844f0b61b2be4249722872f862b9593bbd92565",
        None,
    ]


def test_redact_and_truncate_preserve_nulls() -> None:
    values = pa.array(["abcdef", None, "xy"], from_pandas=True)

    assert redact_array(values, redact_with="X").to_pylist() == ["X", None, "X"]
    assert truncate_array(values, length=3, keep="head").to_pylist() == ["abc", None, "xy"]
    assert truncate_array(values, length=2, keep="tail", mask_char="*").to_pylist() == [
        "****ef",
        None,
        "xy",
    ]


def test_derive_canonicalization_rejects_float_but_fk_match_key_accepts_it() -> None:
    with pytest.raises(GenerationError) as exc:
        canonicalize_derive_source(123.0)
    assert exc.value.code == "float_canonicalization_unsupported"

    assert fk_key_value(123) == fk_key_value(123.0)
    assert fk_key_value(123.5) == 123.5


def test_fk_match_key_has_null_sentinel_for_arrow_join_semantics() -> None:
    assert fk_key_value(None) is NULL_FK_KEY
    assert fk_key_value(math.nan) is NULL_FK_KEY
    # Codex round-4 Finding A: the oracle's parent_map is a plain Python dict,
    # where `True`/`False` already hash and compare equal to `1`/`0` (bool is
    # an int subtype). fk_key_value normalizes bool to int so `fk_join_key`'s
    # string encoding (which cannot rely on Python's object equality) reaches
    # the same collision instead of tagging bool and int keys distinctly.
    assert fk_key_value(True) == 1
    assert type(fk_key_value(True)) is int
    assert fk_key_value(False) == 0
    assert type(fk_key_value(False)) is int
    assert fk_key_value(True) == fk_key_value(1)
    assert fk_key_value(False) == fk_key_value(0)
    # Not a blanket collapse: a bool key stays distinct from an int it is not
    # equal to.
    assert fk_key_value(True) != fk_key_value(2)
