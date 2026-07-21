"""ICD-10-CM parser (HC-1 slice 2) -- SCAFFOLDED, not implemented.

Proves the per-source plug-in shape generalizes beyond NDC (this module is a
real, wired ``CorpusParser`` -- registered in ``PARSERS``, carries the real
source URL and file layout verified against a live download) without
committing to the parse itself in this slice. ``parse_archive`` raises
``NotImplementedError`` with the concrete follow-on spec below.

Source verified 2026-07-21: CDC NCHS ICD-10-CM FY2026 publication directory
(https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/2026/),
current-year convention -- the URL is year-versioned, not fixed, so a future
run needs to discover the current year's directory listing rather than
hardcode 2026 forever. That directory lists several files; the plain-text
one this parser targets is:

  ``icd10cm-Code Descriptions-2026.zip`` (~2.2MB) containing
  ``icd10cm-codes-2026.txt`` (verified: 74,719 data lines, no header row).

Row layout (fixed-width, NOT delimited): each line is the code left-padded
in a fixed-width field (observed widths 3-7 chars, right-padded with spaces
to a wider column -- confirmed via a real download: whitespace-split field 1
gives the bare code, e.g. "A000", "A0100") followed by whitespace and the
full description, e.g.::

    A000    Cholera due to Vibrio cholerae 01, biovar cholerae

Follow-on work (not done here):
  - Discover the current FY directory (the "2026" path segment) instead of
    hardcoding it, or accept a year parameter.
  - Parse the fixed-width layout: split each line on the first run of
    whitespace after the code field (``line.split(None, 1)`` handles this
    correctly since codes never contain whitespace and descriptions may).
  - Chapter derivation: ICD-10-CM chapters are RANGE-based (e.g. Chapter 1
    = A00-B99, Chapter 2 = C00-D49), not the seed's "first letter of the
    code" shortcut (which happens to work for the seed's hand-picked sample
    but is not the real ICD-10-CM chapter boundary -- e.g. both "C" and "D"
    codes below D50 are Chapter 2, "Neoplasms", while D50 and above are
    Chapter 3). A real implementation needs the CMS/NCHS chapter range
    table, not a per-letter heuristic.
  - source_version/effective_date: FY2026's federal-fiscal-year convention
    (effective_date "2025-10-01", matching build_codesets.py's FY2024
    precedent), source_version "FY2026".
"""

from __future__ import annotations

from datetime import date

from ._base import ParsedCorpus

ICD10CM_SOURCE_URL = (
    "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/2026/"
    "icd10cm-Code%20Descriptions-2026.zip"
)


class Icd10CmParser:
    """Scaffolded: wired to the real source, not yet parsing it.

    See module docstring for exactly what remains -- fixed-width row
    parsing plus the chapter range table, both real design/verification
    work rather than a mechanical follow-on.
    """

    name = "icd10"
    source_url = ICD10CM_SOURCE_URL
    min_source_bytes = 1_000_000  # real download verified ~2.2MB
    min_row_count = 50_000  # real file verified 74,719 lines

    def parse_archive(self, raw: bytes, *, pulled_on: date) -> ParsedCorpus:
        raise NotImplementedError(
            "ICD-10-CM parsing is scaffolded, not implemented, in HC-1 slice 2's "
            f"first cut. Source (verified live 2026-07-21): {ICD10CM_SOURCE_URL} "
            "-- a zip containing icd10cm-codes-2026.txt, a fixed-width 'code "
            "<whitespace> description' text file (no header, ~74.7k lines). "
            "Remaining work: parse that layout, derive chapter from the real "
            "ICD-10-CM chapter RANGE table (not a per-letter heuristic -- see "
            "this module's docstring), and confirm the current FY directory "
            "path instead of the hardcoded 2026 one. See "
            "docs/backlog/hc-1-codeset-slice2-and-hardening-tail.md."
        )
