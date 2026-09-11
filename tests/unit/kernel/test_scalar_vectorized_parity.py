"""Task 5.1: `redact_array`/`truncate_array` vectorized fast path vs the frozen reference.

`_scalar.py`'s public `redact_array` and `truncate_array` are guarded dispatchers: a
closed, non-raising guard admits only a plain `pa.string()` array (or chunked array of
one) with plain-`str` options, and takes a vectorized Arrow compute fast path in that
case; every other shape -- another dtype, an encoded-null layout, invalid UTF-8, a raw
Python list -- falls back to `_redact_array_reference`/`_truncate_array_reference`, the
frozen, byte-identical copy of the pre-vectorization per-row implementation.

This file is a DIFFERENTIAL suite: every case calls both the dispatcher and the
reference on the same input and asserts they agree on logical values, Arrow type, null
mask, return container, and exception class. It never hand-writes an expected value --
the reference computes the answer -- so the only way this suite can go green on a real
divergence is if the divergence happens to be invisible to all five of those checks at
once. `test_route_spy_*` additionally proves, by monkeypatching the reference, which
branch each representative input actually took.
"""

from __future__ import annotations

import contextlib
import decimal
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pytest

from decoy_engine.kernel import _scalar
from decoy_engine.kernel._scalar import (
    _redact_array_reference,
    _truncate_array_reference,
    redact_array,
    truncate_array,
)

# ---------------------------------------------------------------------------
# Differential harness
# ---------------------------------------------------------------------------


def _null_mask(container: Any) -> list[bool]:
    """The logical null mask of a kernel return value, independent of its type."""
    if isinstance(container, pa.ChunkedArray):
        container = container.combine_chunks()
    if isinstance(container, pa.Array):
        return pc.is_null(container).to_pylist()  # type: ignore[attr-defined, unused-ignore]
    return [v is None for v in container]


def _assert_matches_reference(dispatch_fn, reference_fn, values: Any, **kwargs: Any) -> None:
    """Run `reference_fn` as the oracle, then assert `dispatch_fn` agrees exactly.

    Agreement means: same exception class (if the reference raises at all), else same
    logical values, same Arrow type, same null mask, and a `pa.Array` return container
    from the dispatcher (never a list or ChunkedArray, even when the reference's own
    input was one).
    """
    try:
        expected = reference_fn(values, **kwargs)
    except Exception as exc:
        with pytest.raises(type(exc)):
            dispatch_fn(values, **kwargs)
        return

    got = dispatch_fn(values, **kwargs)
    assert isinstance(got, pa.Array), f"dispatcher returned {type(got)!r}, not pa.Array"
    assert got.type == expected.type, f"type mismatch: {got.type} != {expected.type}"
    assert got.to_pylist() == expected.to_pylist()
    assert _null_mask(got) == _null_mask(expected)


def _check_redact(values: Any, **kwargs: Any) -> None:
    _assert_matches_reference(redact_array, _redact_array_reference, values, **kwargs)


def _check_truncate(values: Any, **kwargs: Any) -> None:
    _assert_matches_reference(truncate_array, _truncate_array_reference, values, **kwargs)


# ---------------------------------------------------------------------------
# Exotic Arrow shape builders
# ---------------------------------------------------------------------------


def _dictionary_array() -> pa.Array:
    return pa.array(["a", "b", "a", None]).dictionary_encode()


def _run_end_encoded_array() -> pa.Array:
    return pc.run_end_encode(  # type: ignore[attr-defined, unused-ignore]
        pa.array(["a", "a", "b", None])
    )


def _dense_union_array() -> pa.Array:
    types = pa.array([0, 1, 0], type=pa.int8())
    offsets = pa.array([0, 0, 1], type=pa.int32())
    return pa.UnionArray.from_dense(
        types, offsets, [pa.array(["x", "y"]), pa.array([1, 2])], field_names=["strs", "ints"]
    )


def _sparse_union_array() -> pa.Array:
    types = pa.array([0, 1, 0], type=pa.int8())
    return pa.UnionArray.from_sparse(
        types,
        [pa.array(["x", "q", "y"]), pa.array([9, 2, 9])],
        field_names=["strs", "ints"],
    )


class _StringExtType(pa.ExtensionType):
    """Minimal string-storage extension type, for the "not `is` `pa.string()`" guard."""

    def __init__(self) -> None:
        super().__init__(pa.string(), "test.decoy_string_ext")

    def __arrow_ext_serialize__(self) -> bytes:
        return b""

    @classmethod
    def __arrow_ext_deserialize__(
        cls, storage_type: pa.DataType, serialized: bytes
    ) -> _StringExtType:
        return cls()


def _string_extension_array() -> pa.Array:
    return pa.ExtensionArray.from_storage(_StringExtType(), pa.array(["a", "b", None]))


def _invalid_utf8_array() -> pa.Array:
    """A real `pa.string()` array whose buffer holds a malformed UTF-8 byte sequence.

    `pa.array([...], type=pa.string())` validates and encodes at construction time, so
    this has to go through an unsafe cast from `binary` to smuggle the bad byte past
    construction -- reproducing the shape a corrupt upstream Parquet/Arrow file could
    hand the kernel.
    """
    binary = pa.array([b"\xff\xfe", b"ok", None], type=pa.binary())
    return pc.cast(binary, pa.string(), safe=False)


_TEXT_VALUES = ["abcdef", None, "xy", "", "café", "😀abc", "a😀b😀c"]

LENGTHS: list[Any] = [
    0,
    1,
    3,
    100,
    2**63,
    10**100,
    -1,
    -5,
    "3",
    3.0,
    np.int64(3),
    True,
    False,
]
KEEPS: list[Any] = ["head", "tail", [], "sideways"]
MASK_CHARS: list[Any] = [
    None,
    "*",
    "",
    "AB",
    "😀",
    "é",  # combining e + acute (NFD) is exercised separately below
    "‍",  # ZWJ
    "️",  # variation selector-16
    "́",  # combining acute accent alone
]


# ---------------------------------------------------------------------------
# truncate_array: full (length x keep x mask_char) matrix over one text array
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("length", LENGTHS, ids=lambda v: f"len={v!r}")
@pytest.mark.parametrize("keep", KEEPS, ids=lambda v: f"keep={v!r}")
@pytest.mark.parametrize("mask_char", MASK_CHARS, ids=lambda v: f"mask={v!r}")
def test_truncate_matrix(length: Any, keep: Any, mask_char: Any) -> None:
    values = pa.array(_TEXT_VALUES)
    _check_truncate(values, length=length, keep=keep, mask_char=mask_char)


def test_truncate_unhashable_keep_does_not_raise_on_membership_check() -> None:
    # `keep in (...)` on a tuple never hashes its members, but `type(keep) is str`
    # short-circuits before it either way; this pins that no TypeError escapes.
    _check_truncate(pa.array(["abc", None]), length=2, keep=[], mask_char=None)


def test_truncate_non_str_mask_char_with_unpaired_surrogate() -> None:
    # A `str` mask_char carrying an unpaired surrogate is admitted by the type-exact
    # guard (`type(x) is str`); whatever the fast path or the reference does with it
    # (succeed, or raise the same encode error) must match exactly.
    _check_truncate(pa.array(["abcdef", None, "xy"]), length=1, keep="head", mask_char="\ud800")
    _check_truncate(pa.array(["abcdef", None, "xy"]), length=1, keep="tail", mask_char="\ud800")


# ---------------------------------------------------------------------------
# truncate_array: dtype sweep (only plain pa.string() may take the fast path)
# ---------------------------------------------------------------------------


def _truncate_dtype_cases() -> list[tuple[str, Any]]:
    cases: list[tuple[str, Any]] = [
        ("bool", pa.array([True, False, None])),
        ("int", pa.array([123, -45, None])),
        ("float", pa.array([1.5, float("nan"), None])),
        ("binary", pa.array([b"abcdef", b"xy", None])),
        ("date", pa.array([18000, None], type=pa.date32())),
        ("timestamp", pa.array([1_700_000_000_000, None], type=pa.timestamp("ms"))),
        ("decimal", pa.array([decimal.Decimal("12.345"), None], type=pa.decimal128(5, 3))),
        ("dictionary", _dictionary_array()),
        ("mixed_list", ["a", 5, None, "bb"]),
        ("string", pa.array(["abcdef", None, "xy"])),
        ("large_string", pa.array(["abcdef", None, "xy"], type=pa.large_string())),
        ("run_end_encoded", _run_end_encoded_array()),
        ("dense_union", _dense_union_array()),
        ("sparse_union", _sparse_union_array()),
        ("string_extension", _string_extension_array()),
    ]
    if hasattr(pa, "string_view"):
        cases.append(("string_view", pa.array(["abcdef", None, "xy"], type=pa.string_view())))
    return cases


@pytest.mark.parametrize(
    ("label", "values"), _truncate_dtype_cases(), ids=lambda x: x if isinstance(x, str) else ""
)
def test_truncate_dtype_sweep(label: str, values: Any) -> None:
    _check_truncate(values, length=2, keep="head", mask_char=None)
    _check_truncate(values, length=2, keep="tail", mask_char="*")


def test_truncate_invalid_utf8_falls_back_and_raises_like_the_reference() -> None:
    _check_truncate(_invalid_utf8_array(), length=2, keep="head", mask_char=None)
    _check_truncate(_invalid_utf8_array(), length=2, keep="tail", mask_char="*")


# ---------------------------------------------------------------------------
# truncate_array: shapes -- sliced, multi-chunk, empty/all-null chunks
# ---------------------------------------------------------------------------


def test_truncate_sliced_array() -> None:
    base = pa.array(["abcdef", "ghij", None, "xy", "z"])
    _check_truncate(base.slice(1, 3), length=2, keep="head", mask_char=None)
    _check_truncate(base.slice(1, 3), length=2, keep="tail", mask_char="*")


def test_truncate_multi_chunk_chunked_array() -> None:
    ca = pa.chunked_array(
        [pa.array(["abcdef", "gh"]), pa.array([], type=pa.string()), pa.array([None, "xy"])]
    )
    _check_truncate(ca, length=2, keep="head", mask_char=None)
    _check_truncate(ca, length=2, keep="tail", mask_char="*")


def test_truncate_empty_array() -> None:
    _check_truncate(pa.array([], type=pa.string()), length=2, keep="head", mask_char=None)


def test_truncate_all_null_chunked_array() -> None:
    ca = pa.chunked_array([pa.array([None, None], type=pa.string())])
    _check_truncate(ca, length=2, keep="tail", mask_char="*")


def test_truncate_empty_chunks_chunked_array() -> None:
    ca = pa.chunked_array(
        [pa.array([], type=pa.string()), pa.array([], type=pa.string()), pa.array(["a", None])]
    )
    _check_truncate(ca, length=1, keep="head", mask_char=None)


# ---------------------------------------------------------------------------
# truncate_array: unicode normalization forms and embedded NULs
# ---------------------------------------------------------------------------


def test_truncate_unicode_nfc_nfd_and_nul() -> None:
    import unicodedata

    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    with_nul = "ab\x00cd"
    values = pa.array([nfc, nfd, with_nul, None])
    for length in (1, 2, 3, 4, 5):
        for keep in ("head", "tail"):
            for mask_char in (None, "*", "😀"):
                _check_truncate(values, length=length, keep=keep, mask_char=mask_char)


# ---------------------------------------------------------------------------
# redact_array: length/type sweep is not applicable (no length arg); dtype +
# encoded-null + shape sweep instead
# ---------------------------------------------------------------------------


def _redact_cases() -> list[tuple[str, Any]]:
    cases: list[tuple[str, Any]] = [
        ("plain_string", pa.array(["a", "b", None])),
        ("real_nan_float", pa.array([1.5, float("nan"), None])),
        ("dictionary", _dictionary_array()),
        ("run_end_encoded", _run_end_encoded_array()),
        ("dense_union", _dense_union_array()),
        ("sparse_union", _sparse_union_array()),
        ("empty_string", pa.array([], type=pa.string())),
        ("all_null_string", pa.array([None, None], type=pa.string())),
        ("null_dense", pa.array(["a", None, "b", None, None, "c"])),
        ("mixed_list", ["a", 5, None]),
        ("string_extension", _string_extension_array()),
    ]
    if hasattr(pa, "string_view"):
        cases.append(("string_view", pa.array(["a", "b", None], type=pa.string_view())))
    return cases


@pytest.mark.parametrize(
    ("label", "values"), _redact_cases(), ids=lambda x: x if isinstance(x, str) else ""
)
def test_redact_dtype_and_shape_sweep(label: str, values: Any) -> None:
    _check_redact(values, redact_with="REDACTED")


def test_redact_non_str_redact_with() -> None:
    values = pa.array(["a", "b", None])
    for redact_with in (0, 1, True, None, 3.5, ["x"]):
        _check_redact(values, redact_with=redact_with)


def test_redact_invalid_utf8_falls_back_and_raises_like_the_reference() -> None:
    _check_redact(_invalid_utf8_array(), redact_with="REDACTED")


def test_redact_unpaired_surrogate_list_input() -> None:
    # A raw Python list is never a `pa.Array`, so the guard always defers regardless
    # of content; this pins that the dispatcher and reference are the same call for
    # a value Arrow could not even construct a `pa.string()` array from.
    _check_redact(["a", "\ud800", None], redact_with="REDACTED")


def test_redact_sliced_and_multi_chunk() -> None:
    base = pa.array(["abcdef", "ghij", None, "xy", "z"])
    _check_redact(base.slice(1, 3), redact_with="X")
    ca = pa.chunked_array(
        [pa.array(["a", "b"]), pa.array([], type=pa.string()), pa.array([None, "c"])]
    )
    _check_redact(ca, redact_with="X")


def test_redact_all_null_and_empty_chunks() -> None:
    ca = pa.chunked_array([pa.array([None, None], type=pa.string())])
    _check_redact(ca, redact_with="X")
    ca_empty = pa.chunked_array([pa.array([], type=pa.string()), pa.array([], type=pa.string())])
    _check_redact(ca_empty, redact_with="X")


# ---------------------------------------------------------------------------
# Route-spy: prove the fast path is actually taken/declined where the guard says
# ---------------------------------------------------------------------------


def test_route_spy_redact_plain_string_takes_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    original = _scalar._redact_array_reference

    def spy(*args: Any, **kwargs: Any) -> pa.Array:
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(_scalar, "_redact_array_reference", spy)
    result = _scalar.redact_array(pa.array(["a", "b", None]), redact_with="X")
    assert result.to_pylist() == ["X", "X", None]
    assert calls == [], "plain string + str redact_with must skip the reference entirely"


@pytest.mark.parametrize(
    ("values", "kwargs"),
    [
        (pa.array([1, 2, None]), {"redact_with": "X"}),  # non-string dtype
        (pa.array(["a", "b"]).dictionary_encode(), {"redact_with": "X"}),  # dictionary
        (pa.array(["a", "b", None], type=pa.large_string()), {"redact_with": "X"}),  # large_string
        (["a", "b", None], {"redact_with": "X"}),  # raw list
        (pa.array(["a", "b", None]), {"redact_with": 7}),  # non-str redact_with
    ],
)
def test_route_spy_redact_excluded_inputs_take_reference(
    monkeypatch: pytest.MonkeyPatch, values: Any, kwargs: dict
) -> None:
    calls: list[Any] = []
    original = _scalar._redact_array_reference

    def spy(*args: Any, **kw: Any) -> pa.Array:
        calls.append((args, kw))
        return original(*args, **kw)

    monkeypatch.setattr(_scalar, "_redact_array_reference", spy)
    # The call itself may legitimately raise (the reference is a plain per-row loop
    # with no input validation of its own); this test pins routing, not success, so
    # any exception the reference raises is expected and swallowed here.
    with contextlib.suppress(Exception):
        _scalar.redact_array(values, **kwargs)
    assert len(calls) == 1, "excluded input must route through the reference exactly once"


def test_route_spy_truncate_plain_string_takes_fast_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    original = _scalar._truncate_array_reference

    def spy(*args: Any, **kwargs: Any) -> pa.Array:
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(_scalar, "_truncate_array_reference", spy)
    result = _scalar.truncate_array(
        pa.array(["abcdef", None]), length=3, keep="head", mask_char="*"
    )
    assert result.to_pylist() == ["abc***", None]
    assert calls == [], "plain string + valid options must skip the reference entirely"


@pytest.mark.parametrize(
    ("values", "kwargs"),
    [
        (pa.array([1, 2, None]), {"length": 2, "keep": "head", "mask_char": None}),  # non-string
        (
            pa.array(["a", "b"], type=pa.large_string()),
            {"length": 1, "keep": "head", "mask_char": None},
        ),
        (["a", "b", None], {"length": 1, "keep": "head", "mask_char": None}),  # raw list
        (pa.array(["a", "b"]), {"length": 0, "keep": "head", "mask_char": None}),  # length == 0
        (
            pa.array(["a", "b"]),
            {"length": -1, "keep": "head", "mask_char": None},
        ),  # negative length
        (pa.array(["a", "b"]), {"length": True, "keep": "head", "mask_char": None}),  # bool length
        (
            pa.array(["a", "b"]),
            {"length": np.int64(2), "keep": "head", "mask_char": None},
        ),  # numpy int
        (pa.array(["a", "b"]), {"length": 2, "keep": "sideways", "mask_char": None}),  # bad keep
        (pa.array(["a", "b"]), {"length": 2, "keep": [], "mask_char": None}),  # unhashable keep
        (pa.array(["a", "b"]), {"length": 2, "keep": "head", "mask_char": 7}),  # non-str mask_char
    ],
)
def test_route_spy_truncate_excluded_inputs_take_reference(
    monkeypatch: pytest.MonkeyPatch, values: Any, kwargs: dict
) -> None:
    calls: list[Any] = []
    original = _scalar._truncate_array_reference

    def spy(*args: Any, **kw: Any) -> pa.Array:
        calls.append((args, kw))
        return original(*args, **kw)

    monkeypatch.setattr(_scalar, "_truncate_array_reference", spy)
    # As above: some excluded shapes (e.g. a non-str mask_char) make even the
    # reference raise. Routing is what this test checks, not success.
    with contextlib.suppress(Exception):
        _scalar.truncate_array(values, **kwargs)
    assert len(calls) == 1, "excluded input must route through the reference exactly once"
