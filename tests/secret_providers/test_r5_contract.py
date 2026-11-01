"""F13 r5: every component of a Vault reference is percent-decoded exactly once, strictly.

Class: validation strictness at a parse boundary (accept-by-default). ``parse_qsl`` already
decodes every query value with ``errors="strict"``; ``_parse_vault`` then ran ``unquote`` on the
field a second time, so ``%25ff`` was rejected (or, once that second decode was lenient, accepted
as U+FFFD) and ``%2525`` read as ``%``: a KV key holding a literal ``%`` could not be named. The
tables pin accept or reject AND the exact decoded value for every encoding shape, so a lenient,
doubled or skipped decode turns a row red. The same boundary let a malformed escape (a ``%`` not
followed by two hex digits) and control characters in the field pass by default; both are pinned
as rejected (RFC 3986 §2.1; the mount and path segments already rejected control characters). Red
baseline against ee12b248: docs/plans/evidence/2026-10-30-f13-r5-red-baseline.txt.
"""

from __future__ import annotations

import pytest

from decoy_engine.keyprovider import MaskSecretError
from decoy_engine.secret_providers import parse_ref

# The field as written in the reference -> its decoded value, or None when the ref is rejected.
FIELD_TABLE: dict[str, tuple[str, str | None]] = {
    "plain-ascii": ("key", "key"),
    "escaped-space": ("f%20g", "f g"),
    "form-plus-is-space": ("a+b", "a b"),  # the query is form-decoded, as parse_qsl defines it
    "escaped-plus": ("a%2Bb", "a+b"),
    "escaped-percent-then-hex": ("%25ff", "%ff"),  # decoded twice: rejected, or U+FFFD if lenient
    "escaped-percent-twice": ("%2525", "%25"),  # decoded twice: "%"
    "escaped-percent-then-ascii-hex": ("%2541", "%41"),  # decoded twice: "A"
    "trailing-escaped-percent": ("100%25", "100%"),
    "valid-multibyte": ("%c3%a9", "é"),
    "literal-multibyte": ("é", "é"),
    "lone-ff-byte": ("%ff", None),
    "lone-lead-byte": ("%c3", None),
    "lone-continuation-byte": ("%a9", None),
    "overlong-slash": ("%c0%af", None),
    "truncated-three-byte": ("%e2%82", None),
    "utf16-surrogate": ("%ed%a0%80", None),
    "bare-percent": ("%", None),  # a "%" only ever starts a two-hex-digit escape
    "percent-non-hex": ("%zz", None),
    "percent-one-hex-digit": ("%4", None),
    "nul": ("%00", None),
    "newline": ("%0a", None),
}

# The same rule for the mount and the path: one strict decode, no form decoding, no bare "%".
LOCATOR_TABLE: dict[str, tuple[str, str | None]] = {
    "plain": ("vault://kv/team/app", "kv/team/app"),
    "escaped-space-in-path": ("vault://kv/a%20b", "kv/a b"),
    "plus-is-literal-in-path": ("vault://kv/a+b", "kv/a+b"),
    "escaped-percent-in-path": ("vault://kv/a%2525b", "kv/a%25b"),  # twice: "a%b"
    "escaped-percent-in-mount": ("vault://k%2525v/a", "k%25v/a"),
    "valid-multibyte-in-mount": ("vault://k%c3%a9v/a", "kév/a"),
    "lone-ff-in-path": ("vault://kv/%ff", None),
    "lone-ff-in-mount": ("vault://%ff/a", None),
    "bare-percent-in-path": ("vault://kv/a%", None),
    "percent-non-hex-in-path": ("vault://kv/a%zzb", None),
    "percent-one-hex-digit-in-mount": ("vault://k%4v/a", None),
    "nul-in-path": ("vault://kv/a%00b", None),
}

QUERY = "?version=3&field=key&encoding=raw"


def _assert_rejected(text: str) -> None:
    with pytest.raises(MaskSecretError) as info:
        parse_ref(text)
    err = info.value
    assert type(err) is MaskSecretError and err.code == "bad_secret_ref"
    assert err.__cause__ is None and err.__context__ is None


def test_the_tables_hold_both_outcomes():
    for table in (FIELD_TABLE, LOCATOR_TABLE):
        outcomes = {decoded is None for _, decoded in table.values()}
        assert outcomes == {True, False}, "a one-sided table proves nothing"


@pytest.mark.parametrize("case", sorted(FIELD_TABLE))
def test_the_field_is_decoded_exactly_once_or_the_ref_is_rejected(case):
    encoded, decoded = FIELD_TABLE[case]
    text = f"vault://kv/team/app?version=3&field={encoded}&encoding=raw"
    if decoded is None:
        _assert_rejected(text)
        return
    ref = parse_ref(text)
    assert ref.field == decoded
    assert (ref.scheme, ref.locator, ref.version, ref.encoding) == (
        "vault",
        "kv/team/app",
        "3",
        "raw",
    )


@pytest.mark.parametrize("case", sorted(LOCATOR_TABLE))
def test_the_mount_and_path_are_decoded_exactly_once_or_the_ref_is_rejected(case):
    prefix, locator = LOCATOR_TABLE[case]
    if locator is None:
        _assert_rejected(prefix + QUERY)
        return
    ref = parse_ref(prefix + QUERY)
    assert ref.locator == locator
    assert (ref.version, ref.field, ref.encoding) == ("3", "key", "raw")
