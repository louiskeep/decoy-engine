"""`parse_ref` refuses with no inherited chain, even inside a caller's handler.

`parse_ref` is exported and a caller may run it while handling its own exception (a decode
error holding secret bytes, say). Python would attach that exception as `__context__` of the
refusal, and a formatted traceback would print it ("During handling of the above exception").
The six admission routes already raise bare (`test_boundary_caller_context.py`); this is the
exported parser's cell.
"""

from __future__ import annotations

import traceback

import pytest

from decoy_engine.keyprovider import MaskSecretError
from decoy_engine.secret_providers import parse_ref

CANARY = "PARSE-REF-CALLER-CANARY-hunter2"

BAD_REFS = {
    "not_a_str": 12345,
    "bytes": b"env:NAME",
    "no_scheme": "no-scheme-at-all",
    "empty": "",
    "nul": "env:NA\x00ME",
    "bad_vault": "vault://mount",
    "bad_encoding": "env:NAME?encoding=rot13",
}


def _texts(exc: BaseException) -> list[str]:
    out: list[str] = []
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        link = stack.pop()
        if link is None or id(link) in seen:
            continue
        seen.add(id(link))
        out += [str(link), repr(link), repr(link.args), repr(getattr(link, "__notes__", None))]
        stack += [link.__cause__, link.__context__]
    out.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    return out


def _secret_bearing() -> BaseException:
    try:
        (CANARY.encode() + b"\xff").decode("utf-8")
    except UnicodeDecodeError as exc:
        exc.add_note(CANARY) if hasattr(exc, "add_note") else None
        return exc
    raise AssertionError("unreachable")


@pytest.mark.parametrize("case", sorted(BAD_REFS))
def test_parse_ref_inside_a_callers_handler_carries_nothing_of_it(case: str) -> None:
    held = _secret_bearing()
    try:
        raise held
    except UnicodeDecodeError:
        with pytest.raises(MaskSecretError) as info:
            parse_ref(BAD_REFS[case])
    exc = info.value
    assert exc.__cause__ is None
    assert exc.__context__ is None
    assert exc.__suppress_context__
    for text in _texts(exc):
        assert CANARY not in text, text
        assert "During handling" not in text, text


def test_parse_ref_inside_a_handler_whose_exception_carries_the_canary_in_args() -> None:
    try:
        raise ValueError(f"vault token {CANARY}")
    except ValueError:
        with pytest.raises(MaskSecretError) as info:
            parse_ref("not a ref")
    for text in _texts(info.value):
        assert CANARY not in text, text


def test_a_good_ref_still_parses_inside_a_handler() -> None:
    try:
        raise ValueError(CANARY)
    except ValueError:
        ref = parse_ref("env:DECOY_OK_NAME")
    assert ref.scheme == "env"
