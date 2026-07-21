"""CMS ICD-10-PCS (Procedure Coding System) parser (HC-1 slice 2 follow-on).

Source verified 2026-07-21: CMS "2026 ICD-10-PCS" page (the annual FY2026
release, effective 2025-10-01). The download URL is year-versioned (a new
``<year>-icd-10-pcs-codes-file.zip`` lands every October alongside the
federal fiscal year) -- this parser is wired to the FY2026 annual release
and needs re-pointing to the next FY's URL over time, the same caveat
_icd10cm.py's FY-versioned URL and _msdrg.py's annual-release URL carry.
(An April-1-2026 mid-year R1 revision,
``april-1-2026-icd-10-pcs-codes-file.zip``, also exists; this parser
deliberately targets the annual FY2026 October release, not the R1
revision -- the same annual-vs-mid-year-revision stance _msdrg.py takes
toward v43.0 vs v43.1.)

The FY2026 zip (~663KB) contains 3 members: ``icd10pcs_codes_2026.txt``
(the codes file parsed here), ``codes_addenda_2026.txt`` (a year-to-year
change list, skipped -- this ETL always parses a full corpus snapshot, not
a diff), and ``icd10pcsCodesFile.pdf`` (skipped). Located by case-
insensitive substring ("icd10pcs_codes") plus a ``.txt`` suffix, which
matches only the codes file and not the addenda file (whose name does not
contain that substring), mirroring _hcpcs.py's/_msdrg.py's member-lookup
convention.

Row layout -- verified against the real download bytes 2026-07-21 (79,115
data lines, CRLF-terminated, no header/banner): each line is
``<code><space><description>``, where the code is ALWAYS exactly 7
characters ([0:7]) and the description starts at a fixed offset ([8:]) --
the one delimiter space between them never varies, so this is effectively
fixed-width despite reading as space-delimited. ``.strip()`` on the
description slice also removes the trailing ``\\r``.

Code shape: every character in every one of the 79,115 real codes is one of
``0-9`` or ``A-Z`` EXCLUDING ``I`` and ``O`` (confirmed empirically -- ICD-10-PCS
avoids those two letters everywhere to prevent confusion with the digits 1
and 0), giving the shape guard ``^[0-9A-HJ-NP-Z]{7}$`` below. No real code
violates it; a line that does is a source layout change or truncation, not
a legitimate row to silently skip.

Chapter: unlike _msdrg.py (which omits chapter because ~30 DRGs have a
genuinely blank MDC), every PCS code's first character is its Section --
the top-level PCS grouping -- and it is NEVER blank. Verified empirically:
the real file's first-character distribution covers exactly the 17 known
sections (0-9, B, C, D, F, G, H, X; no code starts with A or E), so
`chapter` can be fully populated for every row, satisfying
``_codeset_loader.py``'s ``_check_corpus_schema`` full-coverage requirement
and enabling ``chapter_preserve`` masking (a Medical/Surgical code masks to
another Medical/Surgical code, an Imaging code to another Imaging code).

Chapter representation: the raw single-character Section code (e.g. "0",
"B", "X"), not the section's English name or an integer. _icd10cm.py's
chapter column holds Roman-numeral strings ("I".."XXII"), not integers, and
``_codeset_loader.py``'s coherence check (``_check_corpus_schema``) and
``code_set.py``'s ``chapter_index``/``chapter_buckets`` treat chapter purely
as an opaque string key -- equality-compared and dict-keyed, never ordered
or arithmetic. A raw section character is exactly that: a stable, already-
unique-per-corpus string key, and it is what the Section itself IS in the
source (no CMS-published section->name mapping ships in the codes file
itself, so hardcoding the 17 English names here would be introducing data
the source doesn't carry). Sections mixing digits and letters ("0" vs "B")
poses no dtype conflict since the column is written as an Arrow string
column, consistent with _icd10cm.py's alphanumeric chapter strings.

Public domain (CMS is the federal-government author of ICD-10-PCS itself,
unlike CPT-4 which is AMA-owned) -- same license class as _msdrg.py and
_hcpcs.py.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date

from .._errors import CodesetParseError
from ._base import ParsedCorpus

PCS_SOURCE_URL = "https://www.cms.gov/files/zip/2026-icd-10-pcs-codes-file.zip"

# Case-insensitive marker for the codes-file member, e.g.
# "icd10pcs_codes_2026.txt" -- distinguishes it from "codes_addenda_2026.txt"
# (the year-to-year change list, which does not contain this substring).
_MEMBER_MARKER = "icd10pcs_codes"

# ICD-10-PCS code shape: exactly 7 characters, each 0-9 or A-Z excluding I
# and O (verified against all 79,115 real FY2026 codes -- see module
# docstring). A line whose first 7 characters don't match this is a source
# layout change or truncated/garbled bytes, not a legitimate row to skip.
_CODE_PATTERN = re.compile(r"^[0-9A-HJ-NP-Z]{7}$")

_CODE_END = 7
_DESC_START = 8

# The 17 valid PCS Section codes (a code's first character), per CMS's
# ICD-10-PCS Reference Manual Section 0 (Medical and Surgical) through
# Section X (New Technology). Verified: the real FY2026 file's first-
# character distribution covers exactly this set, no more and no less.
_KNOWN_SECTIONS = frozenset("0123456789BCDFGHX")


def _find_codes_member(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    if not names:
        raise CodesetParseError("ICD-10-PCS zip archive contains no members.")
    matches = [n for n in names if n.lower().endswith(".txt") and _MEMBER_MARKER in n.lower()]
    if not matches:
        raise CodesetParseError(
            f"ICD-10-PCS zip has no *.txt member containing {_MEMBER_MARKER!r}. "
            f"Present: {sorted(names)}."
        )
    if len(matches) > 1:
        raise CodesetParseError(
            f"ICD-10-PCS zip has more than one *.txt member containing "
            f"{_MEMBER_MARKER!r}, ambiguous: {sorted(matches)}."
        )
    return matches[0]


def _parse_lines(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")
        if not line.strip():
            continue
        code = line[0:_CODE_END]
        if not _CODE_PATTERN.match(code):
            raise CodesetParseError(
                f"ICD-10-PCS data row has code {code!r}, which does not match the "
                "expected 7-char [0-9A-HJ-NP-Z] shape -- likely a source layout "
                "change or garbled bytes, not a legitimate row to silently include."
            )
        section = code[0]
        if section not in _KNOWN_SECTIONS:
            raise CodesetParseError(
                f"ICD-10-PCS code {code!r} has section {section!r}, which is not "
                "one of the 17 known PCS sections -- likely a real new CMS section "
                "(a source change worth failing on) or a source layout change, not "
                "a legitimate row to silently absorb without review."
            )
        description = line[_DESC_START:].strip()
        rows.append({"code": code, "description": description, "chapter": section})
    return rows


class Icd10PcsParser:
    """Parses the CMS ICD-10-PCS codes-file zip into one row per procedure code."""

    name = "icd10pcs"
    source_url = PCS_SOURCE_URL
    # Real FY2026 zip verified 2026-07-21 at ~663KB. Floor set well below so
    # routine annual-release growth never false-positives, but a truncated
    # transfer or a swapped-in HTML error page does.
    min_source_bytes = 300_000
    # Real FY2026 file has 79,115 data rows. Floor set well under so routine
    # annual growth never false-positives, but a badly truncated parse does.
    min_row_count = 75_000

    def parse_archive(self, raw: bytes, *, pulled_on: date) -> ParsedCorpus:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise CodesetParseError(
                f"ICD-10-PCS download is not a valid zip archive: {exc}"
            ) from exc

        member = _find_codes_member(zf)
        try:
            text = zf.read(member).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodesetParseError(
                f"ICD-10-PCS member {member!r} is not valid UTF-8: {exc}"
            ) from exc

        rows = _parse_lines(text)
        if not rows:
            raise CodesetParseError(f"ICD-10-PCS member {member!r} produced zero data rows.")

        return ParsedCorpus(
            rows=rows,
            source="CMS ICD-10-PCS (Procedure Coding System)",
            source_url=PCS_SOURCE_URL,
            license="Public domain (United States Federal Government work; 17 U.S.C. 105)",
            citation=(
                "Centers for Medicare & Medicaid Services. ICD-10-PCS, FY2026 "
                "(effective 2025-10-01). CMS.gov."
            ),
            source_version="FY2026",
            effective_date=date(2025, 10, 1).isoformat(),
        )
