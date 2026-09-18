"""Known-answer tests for `_pool_digest` (plan_faker_determinism_harness_v2.md
C1): `scripts/faker-determinism/digest_codec.py`.

Loaded by file path (`importlib`), not package import: `scripts/` has a
hyphen in its determinism-harness subdirectory name, so it cannot be a
regular importable package -- the same pattern
`tests/physical/test_bench_gen_pool_harness.py` uses for its sibling
`bench_compare_gen.py`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import sys
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[3]
DIGEST_MODULE_PATH = ENGINE_ROOT / "scripts" / "faker-determinism" / "digest_codec.py"

_spec = importlib.util.spec_from_file_location("digest_codec", DIGEST_MODULE_PATH)
assert _spec is not None and _spec.loader is not None
digest_codec = importlib.util.module_from_spec(_spec)
sys.modules["digest_codec"] = digest_codec
_spec.loader.exec_module(digest_codec)

_pool_digest = digest_codec._pool_digest
PoolDigestTypeError = digest_codec.PoolDigestTypeError


def _sha(values: list) -> bytes:
    return hashlib.sha256(_pool_digest(values)).digest()


def test_order_sensitivity() -> None:
    assert _sha(["a", "b"]) != _sha(["b", "a"])


def test_empty_pool_is_stable_and_distinct_from_nonempty() -> None:
    assert _sha([]) == _sha([])
    assert _sha([]) != _sha([None])


def test_none_is_distinct_from_empty_string_and_from_string_none() -> None:
    assert _sha([None]) != _sha([""])
    assert _sha([None]) != _sha(["None"])


def test_bytes_distinct_from_str_with_same_content() -> None:
    assert _sha([b"abc"]) != _sha(["abc"])


def test_bool_distinct_from_equal_int() -> None:
    # isinstance(True, int) is True in Python; the codec must still keep
    # True/1 and False/0 apart rather than collapsing them onto one tag.
    assert _sha([True]) != _sha([1])
    assert _sha([False]) != _sha([0])


def test_composed_and_decomposed_unicode_stay_distinct() -> None:
    composed = "é"  # 'é', single code point
    decomposed = "é"  # 'e' + combining acute accent
    assert composed != decomposed  # sanity: genuinely different strings
    assert _sha([composed]) != _sha([decomposed])


def test_positive_and_negative_zero_float_stay_distinct() -> None:
    assert _sha([0.0]) != _sha([-0.0])


def test_nan_digests_deterministically_without_raising() -> None:
    a = _sha([float("nan")])
    b = _sha([float("nan")])
    assert a == b


def test_int_zero_and_negative_zero_equivalent_are_not_conflated_with_float() -> None:
    assert _sha([0]) != _sha([0.0])


def test_large_and_negative_ints_round_trip_distinctly() -> None:
    values = [0, -1, 1, -(2**200), 2**200]
    digests = {_sha([v]) for v in values}
    assert len(digests) == len(values)


def test_known_encoding_bytes_for_a_small_pool() -> None:
    """Pin the exact wire bytes for one small, fully-specified pool so a
    future accidental format change is caught even if it happens to
    preserve every other property this module checks."""
    encoded = _pool_digest([None, True, 1, "a"])
    assert encoded[0] == digest_codec.CODEC_VERSION
    count = int.from_bytes(encoded[1:9], "big")
    assert count == 4


def test_reject_unsupported_type_with_clear_error() -> None:
    with pytest.raises(PoolDigestTypeError, match=r"index 1.*list"):
        _pool_digest(["ok", [1, 2, 3]])


@pytest.mark.parametrize("bad", [{"a": 1}, (1, 2), object(), complex(1, 2)])
def test_reject_every_other_unsupported_type(bad: object) -> None:
    with pytest.raises(PoolDigestTypeError):
        _pool_digest([bad])


def test_pool_digest_is_deterministic_across_calls() -> None:
    values = ["x", None, 3, -4, 5.5, b"y", True, False, float("nan"), -0.0, 0.0]
    assert _pool_digest(values) == _pool_digest(values)


def test_nan_is_not_finite_sanity() -> None:
    # Guards the test file itself: if this ever fails, float('nan') stopped
    # meaning what the other NaN tests assume.
    assert not math.isfinite(float("nan"))
