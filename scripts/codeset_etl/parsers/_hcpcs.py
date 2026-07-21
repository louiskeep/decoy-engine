"""CMS HCPCS Level II parser (HC-1 slice 2 follow-on).

Source verified 2026-07-21: CMS Healthcare Common Procedure Coding System
(HCPCS) quarterly update page
(https://www.cms.gov/medicare/coding-billing/healthcare-common-procedure-system/quarterly-update).
The download URL is quarter-versioned (a new
"<month>-<year>-alpha-numeric-hcpcs-file.zip" lands every January, April,
July, and October) -- this parser is wired to the 2026Q3 (July) release and
needs re-pointing to the current quarter's URL over time, the same caveat
_icd10cm.py's FY-versioned URL carries.

``hcpc2026_jul_anweb_06172026.zip`` (~2.4MB) contains 7 members; only
``HCPC2026_JUL_ANWEB_06172026.txt`` (the "ANWEB" -- Alpha-Numeric Web --
fixed-width export; verified: 16,826 data lines) is parsed. The other six
(the RIF-style ``*recordlayout.txt``, ``proc_notes_*.txt``, three ``*.xlsx``
workbooks, one corrections workbook) are skipped: this repo has no Excel
dependency (pyproject.toml carries no openpyxl), and the plain-text export
holds the same code+description data the workbooks do. Located by substring
("anweb") plus a ``.txt`` suffix rather than a hardcoded full path, since the
filename's date segment changes every release.

Row layout (fixed-width, NOT delimited) -- verified against the REAL
download bytes, not the stale, generic ``HCPC2026_recordlayout.txt`` RIF
contractor-record doc bundled in the zip (its documented byte offsets do not
match this export's actual modifier-record bytes; the offsets below are the
ones that hold up against every line in a real download):

  cols 1-5   (0-indexed [0:5]):  HCPCS code field (5 chars).
  cols 6-10  (0-indexed [5:10]): sequence number (unused here).
  col  11    (0-indexed [10:11]): Record Identification Code (RIC):
    '3' = first line of a PROCEDURE record. The code field holds the real
          5-char alphanumeric code (e.g. "G0008").
    '4' = continuation line of a procedure record's long description (the
          code field repeats the SAME code; no other new data).
    '7' = first line of a MODIFIER record. Per the bundled record-layout
          doc's COBOL REDEFINES, a modifier record's 2-char modifier code
          lives in cols 4-5 only (cols 1-3 are FILLER/blank) -- this is why
          some raw lines look like they have "leading spaces" before a
          2-char code (e.g. "   A1" = modifier "A1", "Dressing for one
          wound"). Modifiers are not billable HCPCS Level II procedure
          codes -- they are 2-char qualifiers attached to a procedure code
          -- so they are out of scope for this corpus and skipped entirely.
    '8' = continuation line of a modifier record. Also skipped.
  cols 12-91 (0-indexed [11:91]): long description chunk, up to 80 chars,
    word-wrapped (never mid-word) but right-trimmed of its OWN padding on
    every line, including non-terminal continuation chunks -- verified: a
    naive "concatenate the raw padded slices" join drops the word-boundary
    space between chunks whose trailing pad was stripped by the export (e.g.
    "...defibrillation is" + "permitted in bls..." -> "ispermitted" without
    per-chunk stripping). Each chunk is stripped individually and the chunks
    for one code are joined with a single space instead.
  cols 92-119 (0-indexed [91:119]): short description (<=28 chars). Not
    used -- the task wants the long/full description when both exist.

8,725 procedure codes (RIC='3' lines) in the verified 2026Q3 download (Q1
2026 cross-check: 8,623), all matching the letter+4-digit shape enforced by
``_CODE_PATTERN`` below (letters A-C, E-V; D excluded). No CPT-4 (Level I,
all-numeric, AMA-copyrighted) or CDT (the D-series, Level II Dental,
ADA-copyrighted) codes appear in this public export; the code-shape check in
``_parse_lines`` fails closed on either -- a numeric code has no leading
letter and a D-code is rejected by the D-excluding letter class -- rather
than silently absorbing a copyrighted code into a public-domain corpus.

Public domain (CMS; unlike the CPT-4/CDT codes that share this numbering
system, HCPCS Level II codes and descriptors carry no third-party
copyright) -- same license class as the FDA NDC directory this ETL already
ships.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import date

from .._errors import CodesetParseError
from ._base import ParsedCorpus

HCPCS_SOURCE_URL = "https://www.cms.gov/files/zip/july-2026-alpha-numeric-hcpcs-file.zip"

# Case-insensitive marker for the fixed-width export member, e.g.
# "HCPC2026_JUL_ANWEB_06172026.txt" -- the date/quarter segment changes every
# release, so this matches by substring rather than a hardcoded full name.
_MEMBER_MARKER = "anweb"

# HCPCS Level II code shape: one letter followed by 4 digits, verified
# against every RIC='3' row in a real download. The letter class is A-V
# EXCLUDING D: the entire D-range in a HCPCS context is CDT (Current Dental
# Terminology, ADA-copyrighted, moved to the ADA), never legitimate
# public-domain HCPCS Level II, so the guard rejects D-shaped codes
# fail-closed rather than admitting an ADA-copyrighted code. The other
# letters stay wide (A-C, E-V) even though some are absent from today's
# export, so a future legitimate HCPCS code in a currently-unused range is
# still accepted -- only D is a copyright concern.
_CODE_PATTERN = re.compile(r"^[A-CE-V][0-9]{4}$")

_CODE_END = 5
_RIC_START, _RIC_END = 10, 11
_LONGDESC_START, _LONGDESC_END = 11, 91


def _find_codes_member(zf: zipfile.ZipFile) -> str:
    names = zf.namelist()
    if not names:
        raise CodesetParseError("HCPCS zip archive contains no members.")
    matches = [n for n in names if n.lower().endswith(".txt") and _MEMBER_MARKER in n.lower()]
    if not matches:
        raise CodesetParseError(
            f"HCPCS zip has no *.txt member containing {_MEMBER_MARKER!r}. "
            f"Present: {sorted(names)}."
        )
    if len(matches) > 1:
        raise CodesetParseError(
            f"HCPCS zip has more than one *.txt member containing "
            f"{_MEMBER_MARKER!r}, ambiguous: {sorted(matches)}."
        )
    return matches[0]


def _parse_lines(text: str) -> dict[str, str]:
    """Parse the fixed-width ANWEB lines into code -> joined long description.

    RIC '3'/'4' build up a procedure code's (possibly multi-line) long
    description; RIC '7'/'8' (modifier records) are skipped -- see module
    docstring. Any other RIC value is left unrecognized on purpose (rather
    than guessed at) and simply contributes no data, the same fail-quiet
    stance the source's own RIC scheme already takes for anything outside
    its four documented values.
    """
    parts_by_code: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.split("\n"):
        if len(line) <= _RIC_END:
            continue
        ric = line[_RIC_START:_RIC_END]
        if ric == "3":
            code = line[0:_CODE_END].strip().upper()
            if not _CODE_PATTERN.match(code):
                raise CodesetParseError(
                    f"HCPCS procedure record has code {code!r}, which does not match "
                    "the expected letter(A-C,E-V)+4-digit shape -- likely a CPT-4 "
                    "(numeric) or CDT (D-series) copyrighted code leaking into this "
                    "export, or a source layout change; refusing to silently include "
                    "it in a public-domain corpus."
                )
            current = code
            chunk = line[_LONGDESC_START:_LONGDESC_END].strip()
            parts_by_code[code] = [chunk] if chunk else []
        elif ric == "4":
            if current is not None:
                chunk = line[_LONGDESC_START:_LONGDESC_END].strip()
                if chunk:
                    parts_by_code[current].append(chunk)
        elif ric == "7":
            current = None  # modifier record: not a procedure code, skip
        # ric == "8" (modifier continuation) and any other value: no-op.
    return {code: " ".join(parts) for code, parts in parts_by_code.items()}


class HcpcsParser:
    """Parses a CMS HCPCS Level II ANWEB quarterly zip into one row per code."""

    name = "hcpcs"
    source_url = HCPCS_SOURCE_URL
    # Real download verified 2026-07-21 at ~2.4MB (2026Q3) / ~2.5MB (2026Q1).
    # Floor set well below either so routine quarter-to-quarter size drift
    # never false-positives, but a truncated transfer or a swapped-in HTML
    # error page does.
    min_source_bytes = 1_500_000
    # Real 2026Q3 download has 8,725 procedure codes (2026Q1: 8,623) -- floor
    # set well under both so the corpus's slow quarterly growth never
    # false-positives, but a badly truncated parse does.
    min_row_count = 7_000

    def parse_archive(self, raw: bytes, *, pulled_on: date) -> ParsedCorpus:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise CodesetParseError(f"HCPCS download is not a valid zip archive: {exc}") from exc

        member = _find_codes_member(zf)
        try:
            text = zf.read(member).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodesetParseError(f"HCPCS member {member!r} is not valid UTF-8: {exc}") from exc

        descriptions = _parse_lines(text)
        if not descriptions:
            raise CodesetParseError(f"HCPCS member {member!r} produced zero data rows.")

        rows = [{"code": code, "description": desc} for code, desc in descriptions.items()]

        return ParsedCorpus(
            rows=rows,
            source="CMS Healthcare Common Procedure Coding System (HCPCS) Level II",
            source_url=HCPCS_SOURCE_URL,
            license="Public domain (United States Federal Government work; 17 U.S.C. 105)",
            citation=(
                "Centers for Medicare & Medicaid Services. HCPCS Level II, "
                "Alpha-Numeric Quarterly Update, 2026Q3 (July). CMS.gov."
            ),
            source_version="2026Q3",
            effective_date=date(2026, 7, 1).isoformat(),
        )
