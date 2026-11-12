"""Rule M3 of ``canary-span-normalization/v1``: whitespace collapse, then guarded NFC.

Sources, cited per the established-methodology rule:

- UAX 44 ``White_Space`` (``PropList.txt``): the 25 code points below, pinned so the set never
  follows an interpreter's database. Each maximal run becomes one U+0020; nothing is trimmed.
- UAX 15 NFC through ``unicodedata.normalize``, under the Unicode Normalization Stability
  Policy: NFC of a string of code points assigned in Unicode 13.0 (Python 3.10) is the same in
  14.0 (3.11) and 15.0 (3.12). It is not stable for code points assigned later: NFC of
  ``a U+0316 U+1AC1`` reorders the marks on 3.10, where U+1AC1 is unassigned, and not on 3.12.
- ``POST_13_0_NORMALIZATION_SENSITIVE``: the 171 code points assigned after Unicode 13.0 (in
  the 15.0 database of Python 3.12, ``Cn`` in the 13.0 database of Python 3.10) whose canonical
  combining class is non-zero or which have a decomposition. Generated once by the script in
  ``docs/records/2026-11-02-f34-e1-build.md`` and frozen here as a literal; it is never
  computed at import, because the 3.10 database cannot see these code points.

Text holding a table code point is not normalized (``normalize_m3`` returns ``None``): the
caller rejects such a canary and leaves such a cell uncompared, so M3 output is equal on every
supported interpreter (F34 invariant I4). No case folding, compatibility forms or trimming (L2).
"""

from __future__ import annotations

import re
import unicodedata

# UAX 44 White_Space: U+0009..U+000D, U+0020, U+0085, U+00A0, U+1680, U+2000..U+200A, U+2028,
# U+2029, U+202F, U+205F, U+3000.
WHITE_SPACE: frozenset[int] = frozenset(
    {
        0x0009, 0x000A, 0x000B, 0x000C, 0x000D, 0x0020, 0x0085, 0x00A0, 0x1680,
        0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200A,
        0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
    }
)  # fmt: skip

# fmt: off
POST_13_0_NORMALIZATION_SENSITIVE: frozenset[int] = frozenset(
    {
        0x00898, 0x00899, 0x0089A, 0x0089B, 0x0089C, 0x0089D, 0x0089E, 0x0089F,
        0x008CA, 0x008CB, 0x008CC, 0x008CD, 0x008CE, 0x008CF, 0x008D0, 0x008D1,
        0x008D2, 0x00C3C, 0x01715, 0x01AC1, 0x01AC2, 0x01AC3, 0x01AC4, 0x01AC5,
        0x01AC6, 0x01AC7, 0x01AC8, 0x01AC9, 0x01ACA, 0x01ACB, 0x01ACC, 0x01ACD,
        0x01ACE, 0x01DFA, 0x0A7F2, 0x0A7F3, 0x0A7F4, 0x10781, 0x10782, 0x10783,
        0x10784, 0x10785, 0x10787, 0x10788, 0x10789, 0x1078A, 0x1078B, 0x1078C,
        0x1078D, 0x1078E, 0x1078F, 0x10790, 0x10791, 0x10792, 0x10793, 0x10794,
        0x10795, 0x10796, 0x10797, 0x10798, 0x10799, 0x1079A, 0x1079B, 0x1079C,
        0x1079D, 0x1079E, 0x1079F, 0x107A0, 0x107A1, 0x107A2, 0x107A3, 0x107A4,
        0x107A5, 0x107A6, 0x107A7, 0x107A8, 0x107A9, 0x107AA, 0x107AB, 0x107AC,
        0x107AD, 0x107AE, 0x107AF, 0x107B0, 0x107B2, 0x107B3, 0x107B4, 0x107B5,
        0x107B6, 0x107B7, 0x107B8, 0x107B9, 0x107BA, 0x10EFD, 0x10EFE, 0x10EFF,
        0x10F82, 0x10F83, 0x10F84, 0x10F85, 0x11070, 0x11F41, 0x11F42, 0x1E030,
        0x1E031, 0x1E032, 0x1E033, 0x1E034, 0x1E035, 0x1E036, 0x1E037, 0x1E038,
        0x1E039, 0x1E03A, 0x1E03B, 0x1E03C, 0x1E03D, 0x1E03E, 0x1E03F, 0x1E040,
        0x1E041, 0x1E042, 0x1E043, 0x1E044, 0x1E045, 0x1E046, 0x1E047, 0x1E048,
        0x1E049, 0x1E04A, 0x1E04B, 0x1E04C, 0x1E04D, 0x1E04E, 0x1E04F, 0x1E050,
        0x1E051, 0x1E052, 0x1E053, 0x1E054, 0x1E055, 0x1E056, 0x1E057, 0x1E058,
        0x1E059, 0x1E05A, 0x1E05B, 0x1E05C, 0x1E05D, 0x1E05E, 0x1E05F, 0x1E060,
        0x1E061, 0x1E062, 0x1E063, 0x1E064, 0x1E065, 0x1E066, 0x1E067, 0x1E068,
        0x1E069, 0x1E06A, 0x1E06B, 0x1E06C, 0x1E06D, 0x1E08F, 0x1E2AE, 0x1E4EC,
        0x1E4ED, 0x1E4EE, 0x1E4EF,
    }
)
# fmt: on

_WHITE_SPACE_RUN = re.compile("[" + "".join(f"\\u{cp:04x}" for cp in sorted(WHITE_SPACE)) + "]+")
_SENSITIVE_CHARS = frozenset(chr(cp) for cp in POST_13_0_NORMALIZATION_SENSITIVE)


def holds_post_13_0(text: str) -> bool:
    """True if ``text`` holds a code point whose NFC may differ between interpreters."""
    return not _SENSITIVE_CHARS.isdisjoint(text)


def collapse_whitespace(text: str) -> str:
    return _WHITE_SPACE_RUN.sub(" ", text)


def normalize_m3(text: str) -> str | None:
    """M3 form of ``text``, or ``None`` when it holds a post-13.0 table code point."""
    if holds_post_13_0(text):
        return None
    return unicodedata.normalize("NFC", collapse_whitespace(text))
