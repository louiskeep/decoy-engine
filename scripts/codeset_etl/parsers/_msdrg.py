"""CMS MS-DRG Classifications and Software parser (HC-1 slice 2 follow-on).

Source verified 2026-07-21: CMS "MS-DRG Classifications and Software" page
under the Acute Inpatient PPS (Prospective Payment System). The download
URL is annual-release-versioned (a new
``icd10-ms-drg-definitions-manual-files-v<major>.0.zip`` lands every October
alongside the federal fiscal year) -- this parser is wired to the FY2026
annual release (v43.0, effective 2025-10-01) and needs re-pointing to the
next FY's URL over time, the same caveat _icd10cm.py's FY-versioned URL and
_hcpcs.py's quarter-versioned URL carry. (v43.1, an April-2026 R1 mid-year
revision, also exists; this parser deliberately targets the annual v43.0
release, not the R1 revision.)

The FY2026 zip (~4.6MB) contains 9 members; only "Appendix A: List of
MS-DRGs Version 43.0" (verified: an ``appendix_a.txt`` member, ~50KB, 783
total lines incl. banner/header/blank) is parsed. The other 8 (MDC
definitions and further appendix reference files) are skipped -- Appendix A
alone carries the code/MDC/type/description mapping this corpus needs.
Located by case-insensitive substring ("appendix_a") plus a ``.txt`` suffix
rather than a hardcoded full name, mirroring _hcpcs.py's member-lookup
convention.

Row layout (fixed-width, NOT delimited) -- verified against real bytes
2026-07-21; re-verify empirically against a fresh download before relying on
these offsets for a future release, the same stance _hcpcs.py's module
docstring documents. Banner/prose text and a "DRG MDC MS Description"
column-header line precede the data. A line is a DATA row iff its first 3
characters are all digits (``line[0:3].isdigit()``); every banner/prose/
header line fails that test, so no separate header-line-counting is needed:

  [0:3]  DRG code: 3 digit characters, leading zeros preserved (a
    categorical string, e.g. "001", "020", "998" -- never parsed as an int).
  [4:6]  MDC ("01".."25"), OR BLANK for multi-MDC DRGs (e.g. 001, 014) and
    the two ungroupable DRGs (998, 999).
  [8:9]  Type: "M" (medical) / "P" (surgical), OR BLANK for 998/999. Not
    read here -- the corpus schema this parser produces is code/description
    only (see the chapter note below), and this field drives no branching.
  [11:]  Description (the MS-DRG title): free text to end of line, one line
    per DRG -- unlike HCPCS's ANWEB export, Appendix A never word-wraps a
    description across lines.

772 DRG data rows in the verified v43.0 download (of 783 total lines; the
remaining ~11 are the banner/prose/column-header lines skipped above).

Chapter: OMITTED, unlike _icd10cm.py's per-row chapter. MDC is the natural
chapter analog, but ~30 DRGs (the multi-MDC and ungroupable rows above) have
a blank MDC -- and both places a "chapter" column would flow through enforce
FULL, non-null coverage the moment the column exists at all:
``transforms/_codeset_loader.py``'s ``_check_corpus_schema`` raises
``code_set_corpus_incoherent_chapter`` on any null/empty chapter cell once
"chapter" is present in the schema, and ``_write.py``'s
``write_normalized_corpus`` raises the ETL-side equivalent
(``CodesetValidationError``) at write time for the same reason. Neither
supports a per-row-optional chapter, so stamping "chapter" here would force
an artificial value onto the ~30 blank-MDC DRGs rather than leaving them
genuinely absent -- following _hcpcs.py's precedent of omitting a column
this source cannot uniformly populate.

No third-party-copyright concern for MS-DRG codes/descriptions (CMS is the
federal-government author of the manual itself, unlike the AMA-owned CPT-4
codes _hcpcs.py's letter-class shape guard excludes) -- the shape guard
below validates only the numeric ``^[0-9]{3}$`` code shape, it is not a
copyright filter.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date

from .._errors import CodesetParseError
from ._base import ParsedCorpus

MSDRG_SOURCE_URL = "https://www.cms.gov/files/zip/icd10-ms-drg-definitions-manual-files-v43.zip"

# Case-insensitive marker for the Appendix A member, e.g. "appendix_A.txt".
_MEMBER_MARKER = "appendix_a"

# DRG code shape: exactly 3 ASCII digits. Deliberately an explicit [0-9]
# class rather than \d: str.isdigit() (the pre-filter below) accepts a wider
# Unicode "digit" set (e.g. superscripts, other-script decimal digits) than
# a clean ASCII code ever legitimately contains, so a line that passes the
# isdigit() pre-filter but fails this stricter check signals a source
# layout change or garbled bytes, not a real DRG code.
_CODE_PATTERN = re.compile(r"^[0-9]{3}$")

_CODE_END = 3
_DESC_START = 11


def _find_appendix_a_member(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    if not names:
        raise CodesetParseError("MS-DRG zip archive contains no members.")
    matches = [n for n in names if n.lower().endswith(".txt") and _MEMBER_MARKER in n.lower()]
    if not matches:
        raise CodesetParseError(
            f"MS-DRG zip has no *.txt member containing {_MEMBER_MARKER!r}. "
            f"Present: {sorted(names)}."
        )
    if len(matches) > 1:
        raise CodesetParseError(
            f"MS-DRG zip has more than one *.txt member containing "
            f"{_MEMBER_MARKER!r}, ambiguous: {sorted(matches)}."
        )
    return matches[0]


def _parse_lines(text: str) -> list[dict[str, str]]:
    """Parse Appendix A's fixed-width lines into code/description rows.

    A line is a DATA row iff its first 3 characters are all digits; every
    banner/prose/header line (including the "DRG MDC MS Description" column
    header) fails that test and is skipped without any separate
    header-detection logic.
    """
    rows: list[dict[str, str]] = []
    for raw_line in text.split("\n"):
        line = raw_line.rstrip("\r")
        if len(line) < _CODE_END or not line[0:_CODE_END].isdigit():
            continue
        code = line[0:_CODE_END]
        if not _CODE_PATTERN.match(code):
            raise CodesetParseError(
                f"MS-DRG data row has code {code!r}, which does not match the "
                "expected 3-digit shape -- likely a source layout change or "
                "garbled bytes, not a legitimate row to silently include."
            )
        description = line[_DESC_START:].strip()
        rows.append({"code": code, "description": description})
    return rows


class MsDrgParser:
    """Parses CMS MS-DRG Definitions Manual Appendix A into one row per DRG."""

    name = "msdrg"
    source_url = MSDRG_SOURCE_URL
    # Real v43.0 zip verified 2026-07-21 at ~4.6MB. Floor set well below so
    # routine annual-release growth never false-positives, but a truncated
    # transfer or a swapped-in HTML error page does.
    min_source_bytes = 2_000_000
    # Real v43.0 Appendix A has ~740 DRG data rows. Floor set well under so
    # routine annual growth never false-positives, but a badly truncated
    # parse does.
    min_row_count = 700

    def parse_archive(self, raw: bytes, *, pulled_on: date) -> ParsedCorpus:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise CodesetParseError(f"MS-DRG download is not a valid zip archive: {exc}") from exc

        member = _find_appendix_a_member(zf)
        try:
            text = zf.read(member).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodesetParseError(f"MS-DRG member {member!r} is not valid UTF-8: {exc}") from exc

        rows = _parse_lines(text)
        if not rows:
            raise CodesetParseError(f"MS-DRG member {member!r} produced zero data rows.")

        return ParsedCorpus(
            rows=rows,
            source="CMS Medicare Severity Diagnosis-Related Groups (MS-DRG)",
            source_url=MSDRG_SOURCE_URL,
            license="Public domain (United States Federal Government work; 17 U.S.C. 105)",
            citation=(
                "Centers for Medicare & Medicaid Services. ICD-10 MS-DRG "
                "Definitions Manual, Version 43.0 (FY2026), Appendix A. CMS.gov."
            ),
            source_version="v43.0",
            # FY2026 annual release's federal-fiscal-year start (matches
            # _icd10cm.py's FY2026 effective_date convention).
            effective_date=date(2025, 10, 1).isoformat(),
        )
