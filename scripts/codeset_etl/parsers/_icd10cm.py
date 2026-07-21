"""ICD-10-CM parser (HC-1 slice 2).

Source verified 2026-07-21: CDC NCHS ICD-10-CM FY2026 publication directory
(https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/2026/),
current-year convention -- the URL is year-versioned, not fixed, so a future
run needs to discover the current year's directory listing rather than
hardcode 2026 forever (tracked, not solved here).

``icd10cm-Code Descriptions-2026.zip`` (~2.2MB) contains one plain-text
member, ``icd10cm-codes-2026.txt`` (verified: 74,719 data lines, no header
row). This parser locates that member by suffix/basename rather than a
hardcoded full path, since a future FY's zip may nest or rename it slightly.

Row layout (fixed-width, NOT delimited): each line is the code left-padded
in a fixed-width field followed by whitespace and the full description,
e.g. ``A000    Cholera due to Vibrio cholerae 01, biovar cholerae``. Codes
never contain whitespace; descriptions may -- ``line.split(None, 1)`` splits
correctly on the first whitespace run.

Chapter derivation: ICD-10-CM's 22 chapters are RANGE-based over each code's
3-character category (letter + 2 alphanumeric), not a per-letter shortcut
(the seed corpus's "chapter = first letter" bucket is wrong at the C/D and
S/T boundaries -- see ``build_codesets.py``). The range table below is the
CMS/NCHS ICD-10-CM Tabular List's standard 22-chapter category-range table
(CMS ICD-10-CM Official Guidelines for Coding and Reporting, Section I.A;
stable across FY releases), sourced from the same CDC NCHS FY2026
publication directory as the code file itself.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date

from .._errors import CodesetParseError
from ._base import ParsedCorpus

ICD10CM_SOURCE_URL = (
    "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/2026/"
    "icd10cm-Code%20Descriptions-2026.zip"
)

# The codes file's basename changes by FY ("icd10cm-codes-2026.txt"); matched
# by suffix so a differently-nested or differently-year-stamped zip member
# still resolves without a brittle hardcoded full path.
_CODES_MEMBER_SUFFIX = "codes-2026.txt"

# 22-chapter category-range table (inclusive both ends). Categories are a
# code's first 3 characters (e.g. "C00", "D49", "O9A"); note the two
# letter-spanning ranges (II: C00-D49, XIX: S00-T88) and the one
# alphanumeric boundary (XV ends at "O9A", not a plain 2-digit "O99"), plus
# Chapter XXII (U00-U85) sitting out of alphabetical order after Z -- this
# table is intentionally NOT sorted by category, only checked in full below.
#
# Chapter XVII's upper bound is "QA0", not the commonly-published "Q99":
# the real FY2026 download (verified 2026-07-21) contains a "QA0" category
# (13 codes, e.g. "QA00101" "SCN2A-related neurodevelopmental disorder") that
# CMS added under Chapter 17 ("Congenital malformations...") for genetic
# neurodevelopmental disorders -- most public chapter-range summaries predate
# this addition. Bounded at the observed "QA0" rather than a guessed wider
# "QA9" so an unexpected future category still fails closed instead of being
# silently absorbed.
_CHAPTER_RANGES: tuple[tuple[str, str, str], ...] = (
    ("I", "A00", "B99"),
    ("II", "C00", "D49"),
    ("III", "D50", "D89"),
    ("IV", "E00", "E89"),
    ("V", "F01", "F99"),
    ("VI", "G00", "G99"),
    ("VII", "H00", "H59"),
    ("VIII", "H60", "H95"),
    ("IX", "I00", "I99"),
    ("X", "J00", "J99"),
    ("XI", "K00", "K95"),
    ("XII", "L00", "L99"),
    ("XIII", "M00", "M99"),
    ("XIV", "N00", "N99"),
    ("XV", "O00", "O9A"),
    ("XVI", "P00", "P96"),
    ("XVII", "Q00", "QA0"),
    ("XVIII", "R00", "R99"),
    ("XIX", "S00", "T88"),
    ("XX", "V00", "Y99"),
    ("XXI", "Z00", "Z99"),
    ("XXII", "U00", "U85"),
)


def _category_key(category: str) -> tuple[str, str]:
    """(letter, 2-char suffix) sort key for a 3-char ICD-10-CM category.

    Raw string comparison of the 3-char category happens to already sort
    correctly here (ASCII digits are below uppercase letters, so "O99" <
    "O9A"), but splitting into (letter, suffix) makes the letter comparison
    -- the thing that actually decides every chapter boundary in this table,
    including the two that cross letters -- explicit rather than incidental.
    """
    return category[0], category[1:3]


def _chapter_for_category(category: str) -> str | None:
    """Look up the chapter Roman numeral for a 3-char code category.

    Linear scan over 22 entries per row (~1.6M comparisons for the full
    ~74.7k-row file) rather than a sorted binary search: the table is not
    monotonic by category (Chapter XXII, U00-U85, sits after Z alphabetically
    but is checked last), so a binary search would need its own correctness
    argument for no real benefit at this row count.
    """
    key = _category_key(category)
    for numeral, low, high in _CHAPTER_RANGES:
        if _category_key(low) <= key <= _category_key(high):
            return numeral
    return None


def _find_codes_member(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    if not names:
        raise CodesetParseError("ICD-10-CM zip archive contains no members.")
    matches = [n for n in names if n.lower().endswith(_CODES_MEMBER_SUFFIX)]
    if not matches:
        raise CodesetParseError(
            f"ICD-10-CM zip has no member ending in {_CODES_MEMBER_SUFFIX!r}. "
            f"Present: {sorted(names)}."
        )
    if len(matches) > 1:
        raise CodesetParseError(
            f"ICD-10-CM zip has more than one member ending in "
            f"{_CODES_MEMBER_SUFFIX!r}, ambiguous: {sorted(matches)}."
        )
    return matches[0]


class Icd10CmParser:
    """Parses the CDC NCHS ICD-10-CM code-descriptions zip into one row per code."""

    name = "icd10"
    source_url = ICD10CM_SOURCE_URL
    min_source_bytes = 1_000_000  # real download verified ~2.2MB
    min_row_count = 50_000  # real file verified 74,719 lines

    def parse_archive(self, raw: bytes, *, pulled_on: date) -> ParsedCorpus:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise CodesetParseError(
                f"ICD-10-CM download is not a valid zip archive: {exc}"
            ) from exc

        member = _find_codes_member(zf)
        try:
            text = zf.read(member).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodesetParseError(
                f"ICD-10-CM member {member!r} is not valid UTF-8: {exc}"
            ) from exc

        rows: list[dict[str, str]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            parts = line.split(None, 1)
            code = parts[0].strip()
            description = parts[1].rstrip() if len(parts) > 1 else ""
            if len(code) < 3:
                raise CodesetParseError(
                    f"ICD-10-CM code {code!r} is shorter than a 3-char category -- "
                    "unexpected row shape for this source."
                )
            category = code[:3].upper()
            chapter = _chapter_for_category(category)
            if chapter is None:
                raise CodesetParseError(
                    f"ICD-10-CM code {code!r} (category {category!r}) does not fall "
                    "into any of the 22 known chapter ranges -- likely a chapter "
                    "range-table bug or an unexpected code format from a source "
                    "layout change, not a legitimate code to silently drop."
                )
            rows.append({"code": code, "description": description, "chapter": chapter})

        if not rows:
            raise CodesetParseError(f"ICD-10-CM member {member!r} produced zero data rows.")

        return ParsedCorpus(
            rows=rows,
            source="CDC/NCHS ICD-10-CM (International Classification of Diseases, "
            "10th Revision, Clinical Modification)",
            source_url=ICD10CM_SOURCE_URL,
            license="Public domain (United States Federal Government work; 17 U.S.C. 105)",
            citation="Centers for Disease Control and Prevention, National Center "
            "for Health Statistics. ICD-10-CM FY2026. CDC.gov.",
            # FY2026's federal-fiscal-year convention (FY2026 = Oct 1 2025 -
            # Sep 30 2026), matching build_codesets.py's FY2024 precedent.
            source_version="FY2026",
            effective_date=date(2025, 10, 1).isoformat(),
        )
