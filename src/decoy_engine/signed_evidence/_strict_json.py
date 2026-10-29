"""Strict JSON intake for every document the verifier trusts or inspects.

Pattern: RFC 8259 (JSON) with the hardening RFC 8785 section 3.2 and RFC 8725 assume of a
verifier: strict UTF-8 (no BOM), duplicate-member rejection at every depth, no trailing content,
no non-finite numbers, no unpaired surrogates, and size/depth/node limits applied before the
recursive decoder runs. Floats are refused outright: this profile has none, so JCS number
formatting is never load-bearing for a signed value. The stdlib decoder does the parsing; this
module only configures it and walks the result.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ._errors import SignedEvidenceError

MAX_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 200_000
MAX_SAFE_INT = 2**53 - 1

StrictJsonError = SignedEvidenceError

_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[\[\]{}]', re.DOTALL)
_SURROGATE = re.compile("[\ud800-\udfff]")


def _err(code: str) -> SignedEvidenceError:
    return SignedEvidenceError(code)


def _check_depth(text: str) -> None:
    depth = 0
    for m in _TOKEN.finditer(text):
        c = m.group(0)[0]
        if c in "[{":
            depth += 1
            if depth > MAX_DEPTH:
                raise _err("TOO_DEEP")
        elif c in "]}":
            depth = max(0, depth - 1)


def _no_float(_: str) -> Any:
    raise _err("FLOAT_NOT_ALLOWED")


def _no_constant(_: str) -> Any:
    raise _err("NON_FINITE_NUMBER")


def _int(text: str) -> int:
    if text == "-0" or len(text) > 17:
        raise _err("INTEGER_OUT_OF_DOMAIN")
    value = int(text)
    if abs(value) > MAX_SAFE_INT:
        raise _err("INTEGER_OUT_OF_DOMAIN")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _err("DUPLICATE_MEMBER")
        out[key] = value
    return out


def _walk(root: Any) -> None:
    nodes = 0
    stack = [root]
    while stack:
        node = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise _err("TOO_MANY_NODES")
        if isinstance(node, dict):
            for key, value in node.items():
                if _SURROGATE.search(key):
                    raise _err("INVALID_UNICODE")
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str) and _SURROGATE.search(node):
            raise _err("INVALID_UNICODE")


def parse_strict(data: bytes | bytearray | memoryview, *, max_bytes: int = MAX_BYTES) -> Any:
    raw = bytes(data)
    if len(raw) > max_bytes:
        raise _err("TOO_LARGE")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise _err("BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _err("NOT_UTF8") from None
    _check_depth(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=_no_float,
            parse_int=_int,
            parse_constant=_no_constant,
        )
    except SignedEvidenceError:
        raise
    except RecursionError:
        raise _err("TOO_DEEP") from None
    except ValueError:
        raise _err("NOT_JSON") from None
    _walk(value)
    return value
