"""Strict JSON intake: the only parser behind statements, envelopes, trust policies and catalogs."""

from __future__ import annotations

import pytest

from decoy_engine.signed_evidence import _strict_json as sj


def _code(data: bytes, **kw) -> str:
    with pytest.raises(sj.StrictJsonError) as ei:
        sj.parse_strict(data, **kw)
    return ei.value.code


def test_accepts_plain_documents() -> None:
    assert sj.parse_strict(b'{"a":[1,"x",null,true,{"b":-3}]}') == {
        "a": [1, "x", None, True, {"b": -3}]
    }


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b"\xef\xbb\xbf{}", "BOM"),
        (b"{\xff}", "NOT_UTF8"),
        (b'{"a":1}x', "NOT_JSON"),
        (b"", "NOT_JSON"),
        (b'{"a":1,"a":2}', "DUPLICATE_MEMBER"),
        (b'{"o":{"k":1,"k":1}}', "DUPLICATE_MEMBER"),
        (b'[{"k":1,"k":2}]', "DUPLICATE_MEMBER"),
        (b'{"a":1.5}', "FLOAT_NOT_ALLOWED"),
        (b'{"a":1e3}', "FLOAT_NOT_ALLOWED"),
        (b'{"a":NaN}', "NON_FINITE_NUMBER"),
        (b'{"a":Infinity}', "NON_FINITE_NUMBER"),
        (b'{"a":-Infinity}', "NON_FINITE_NUMBER"),
        (b'{"a":9007199254740992}', "INTEGER_OUT_OF_DOMAIN"),
        (b'{"a":-9007199254740992}', "INTEGER_OUT_OF_DOMAIN"),
        (b'{"a":-0}', "INTEGER_OUT_OF_DOMAIN"),
        (b'{"a":"\\ud800"}', "INVALID_UNICODE"),
        (b'{"\\udc00":1}', "INVALID_UNICODE"),
        (b'{"a":"\\ud83d\\ude00"}', None),  # a valid surrogate PAIR is fine
    ],
)
def test_rejections(data: bytes, code: str | None) -> None:
    if code is None:
        assert sj.parse_strict(data) == {"a": "\U0001f600"}
    else:
        assert _code(data) == code


def test_max_safe_integer_boundary() -> None:
    assert sj.parse_strict(b'{"a":9007199254740991}') == {"a": 9007199254740991}
    assert sj.parse_strict(b'{"a":-9007199254740991}') == {"a": -9007199254740991}


def test_depth_limit_is_enforced_before_parsing_deep_input() -> None:
    ok = b"[" * sj.MAX_DEPTH + b"]" * sj.MAX_DEPTH
    sj.parse_strict(ok)
    assert _code(b"[" * (sj.MAX_DEPTH + 1) + b"]" * (sj.MAX_DEPTH + 1)) == "TOO_DEEP"
    # A hostile depth must not reach the recursive JSON decoder (RecursionError).
    assert _code(b"[" * 200_000) == "TOO_DEEP"


def test_brackets_inside_strings_do_not_count_toward_depth() -> None:
    sj.parse_strict(b'{"a":"' + b"[" * 500 + b'"}')
    sj.parse_strict(b'{"a":"\\"' + b"[" * 500 + b'"}')


def test_size_and_node_limits() -> None:
    assert _code(b'{"a":"' + b"x" * 100 + b'"}', max_bytes=50) == "TOO_LARGE"
    many = b"[" + b",".join([b"0"] * (sj.MAX_NODES + 1)) + b"]"
    assert _code(many, max_bytes=len(many) + 1) == "TOO_MANY_NODES"


def test_error_never_echoes_input_content() -> None:
    canary = b"CANARY-SECRET-VALUE-8841"
    for data in (
        b'{"' + canary + b'":1,"' + canary + b'":2}',
        b'{"a":' + canary + b"}",
        b'{"a":"' + canary + b'\\ud800"}',
    ):
        with pytest.raises(sj.StrictJsonError) as ei:
            sj.parse_strict(data)
        assert canary.decode() not in str(ei.value)
        assert canary.decode() not in repr(ei.value)
