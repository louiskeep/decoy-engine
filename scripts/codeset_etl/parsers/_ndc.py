"""FDA National Drug Code (NDC) Directory parser (HC-1 slice 2).

Source: FDA NDC Directory full-release text export, ``ndctext.zip``
(https://www.accessdata.fda.gov/cder/ndctext.zip). Public domain (US
Federal Government work, 17 U.S.C. 105) -- same license as the seed corpus
(``scripts/build_codesets.py``).

Verified against a real download (2026-07-21): a ~10.7MB zip containing two
TAB-delimited files, CRLF line endings, encoded ``cp1252`` (NOT UTF-8 -- the
files contain Windows-1252 punctuation bytes, e.g. curly quotes in labeler
names, that are invalid UTF-8 and crash a strict UTF-8 decode; this is a
known quirk of this specific FDA export, not a generic fallback):

  product.txt (~115.5k rows) -- one row per drug PRODUCT. Columns used here:
    PRODUCTNDC (labeler-product segment pair, e.g. "0002-0152", in one of
    three width configurations: 4-4, 5-3, 5-4), PROPRIETARYNAME,
    NONPROPRIETARYNAME, DOSAGEFORMNAME, ACTIVE_NUMERATOR_STRENGTH,
    ACTIVE_INGRED_UNIT.

  package.txt (~217k rows) -- one row per PACKAGE (a product can ship in
    several package sizes). Columns used here: PRODUCTNDC (joins to
    product.txt), NDCPACKAGECODE (the full 3-segment code, e.g.
    "0002-0152-01"), STARTMARKETINGDATE.

NDC 11-digit normalization: the three segments (labeler-product-package) are
published in one of three width configurations -- 4-4-2, 5-3-2, or 5-4-1 --
and the FDA's own documented convention for a fixed-width "11-digit NDC"
(used for billing/HIPAA transactions) is to zero-pad each segment up to
5-4-2 and concatenate, no separators. This is the SAME code shape the seed
corpus already ships (e.g. "00093052105" -- see ``ndc.parquet``'s existing
rows), so real and seed data are format-compatible.

``chapter`` column: deliberately NOT emitted. The seed corpus's ``chapter``
(A-P) is a Decoy-defined therapeutic-class bucket
(``build_codesets.py``'s own comment: "NOT a source attribute from the FDA
NDC directory"), hand-assigned per drug. FDA's PHARM_CLASSES field is a
free-text, multi-valued EPC/MoA classification list, not a clean
single-letter bucket -- deriving one programmatically is a real
classification design problem, not a mechanical transform, and guessing at
it here risks shipping a misleading "chapter" that looks authoritative.
Scoped out of this slice (chapter_preserve is simply unavailable for the
real NDC corpus until that follow-on lands); see this repo's HC-1 slice-2
backlog doc.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date
from typing import Any

from .._errors import CodesetParseError
from ._base import ParsedCorpus

NDC_SOURCE_URL = "https://www.accessdata.fda.gov/cder/ndctext.zip"

_PRODUCT_MEMBER = "product.txt"
_PACKAGE_MEMBER = "package.txt"

# The three FDA-documented NDC segment-width configurations and their
# zero-pad target (5-4-2 = 11 digits). Any other combination is not a valid
# NDC and is skipped (counted, not silently coerced). Verified against a
# real download: every one of package.txt's ~217k NDCPACKAGECODE values is
# exactly one of these three raw widths (they partition the file exactly,
# no remainder) -- a raw code is never already in the padded 5-4-2 shape,
# so this map intentionally has no identity entry for (5, 4, 2).
_SEGMENT_WIDTHS = {
    (4, 4, 2): (5, 4, 2),
    (5, 3, 2): (5, 4, 2),
    (5, 4, 1): (5, 4, 2),
}


def _decode_text(raw: bytes, *, member: str) -> str:
    """Decode an NDC directory member, tolerating its cp1252 encoding.

    Tries strict UTF-8 first (the common case for a plain-ASCII source
    release); falls back to cp1252 (this export's documented actual
    encoding -- see module docstring) on a decode failure, so a byte that is
    invalid UTF-8 but valid cp1252 (curly quotes, accented labeler names)
    does not abort the whole parse. Normalizes CRLF up front so csv.reader
    never sees a stray trailing ``\\r`` in the last field of a row.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_tsv(text: str) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    rows = list(reader)
    if not rows:
        raise CodesetParseError("NDC source member is empty (no header row).")
    return rows[0], rows[1:]


def _build_product_index(header: list[str], rows: list[list[str]]) -> dict[str, dict[str, str]]:
    """Map raw PRODUCTNDC -> the fields this parser needs, first-row-wins.

    product.txt's PRODUCTNDC is not unique (a small number of products share
    a code across formulation-history rows in the FDA source itself); taking
    the first occurrence in file order is a deterministic, reproducible
    choice given a fixed download (not a random/unordered one).
    """
    try:
        idx = {
            name: header.index(name)
            for name in (
                "PRODUCTNDC",
                "PROPRIETARYNAME",
                "NONPROPRIETARYNAME",
                "DOSAGEFORMNAME",
                "ACTIVE_NUMERATOR_STRENGTH",
                "ACTIVE_INGRED_UNIT",
            )
        }
    except ValueError as exc:
        raise CodesetParseError(f"product.txt is missing an expected column: {exc}") from exc

    out: dict[str, dict[str, str]] = {}
    for row in rows:
        if len(row) <= max(idx.values()):
            continue
        ndc = row[idx["PRODUCTNDC"]].strip()
        if not ndc or ndc in out:
            continue
        out[ndc] = {
            "proprietary_name": row[idx["PROPRIETARYNAME"]].strip(),
            "nonproprietary_name": row[idx["NONPROPRIETARYNAME"]].strip(),
            "dosage_form": row[idx["DOSAGEFORMNAME"]].strip(),
            "strength": row[idx["ACTIVE_NUMERATOR_STRENGTH"]].strip(),
            "unit": row[idx["ACTIVE_INGRED_UNIT"]].strip(),
        }
    return out


def _normalize_package_code(raw_code: str) -> str | None:
    """Zero-pad a raw NDCPACKAGECODE ("0002-0152-01") to the 11-digit form.

    Returns None (caller counts and skips) when the code is not exactly
    3 numeric segments in one of the three FDA-documented width
    configurations -- a defensive floor against a malformed row, not an
    expected case for a well-formed FDA export.
    """
    parts = raw_code.strip().split("-")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    widths = tuple(len(p) for p in parts)
    target = _SEGMENT_WIDTHS.get(widths)  # type: ignore[arg-type]
    if target is None:
        return None
    return "".join(p.zfill(w) for p, w in zip(parts, target, strict=True))


def _describe(product: dict[str, str] | None) -> str:
    if product is None:
        return ""
    name = product["proprietary_name"] or product["nonproprietary_name"]
    strength = f"{product['strength']}{product['unit']}" if product["strength"] else ""
    parts = [p for p in (name, strength, product["dosage_form"]) if p]
    return " ".join(parts)


class NdcParser:
    """Parses ``ndctext.zip`` into one row per (11-digit-normalized) package."""

    name = "ndc"
    source_url = NDC_SOURCE_URL
    # Real download is ~10.7MB (verified 2026-07-21); a truncated/short
    # transfer or an HTML error page swapped in for the zip lands far below
    # this floor.
    min_source_bytes = 5_000_000
    # Real package.txt is ~217k rows, ~217.0k after the (rare) same-package
    # duplicate-listing dedup below; floor set well under that so routine
    # growth of the live directory never false-positives, but a badly
    # truncated or empty parse does.
    min_row_count = 100_000

    def parse_archive(self, raw: bytes, *, pulled_on: date) -> ParsedCorpus:
        try:
            zf = zipfile.ZipFile(io.BytesIO(raw))
        except zipfile.BadZipFile as exc:
            raise CodesetParseError(f"NDC download is not a valid zip archive: {exc}") from exc

        names = set(zf.namelist())
        missing = {_PRODUCT_MEMBER, _PACKAGE_MEMBER} - names
        if missing:
            raise CodesetParseError(
                f"NDC zip is missing expected member file(s) {sorted(missing)}. "
                f"Present: {sorted(names)}."
            )

        product_text = _decode_text(zf.read(_PRODUCT_MEMBER), member=_PRODUCT_MEMBER)
        package_text = _decode_text(zf.read(_PACKAGE_MEMBER), member=_PACKAGE_MEMBER)

        product_header, product_rows = _read_tsv(product_text)
        product_index = _build_product_index(product_header, product_rows)

        package_header, package_rows = _read_tsv(package_text)
        try:
            pndc_i = package_header.index("PRODUCTNDC")
            pkg_i = package_header.index("NDCPACKAGECODE")
            start_i = package_header.index("STARTMARKETINGDATE")
        except ValueError as exc:
            raise CodesetParseError(f"package.txt is missing an expected column: {exc}") from exc

        # Keyed by the RAW package code so the FDA source's own occasional
        # re-listing duplicates (same package re-marketed with a later
        # STARTMARKETINGDATE) collapse deterministically to the most recent
        # listing, not an arbitrary one.
        chosen: dict[str, tuple[str, dict[str, Any]]] = {}
        skipped_malformed = 0
        for row in package_rows:
            if len(row) <= max(pndc_i, pkg_i, start_i):
                skipped_malformed += 1
                continue
            raw_pkg_code = row[pkg_i].strip()
            normalized = _normalize_package_code(raw_pkg_code)
            if normalized is None:
                skipped_malformed += 1
                continue
            start_date = row[start_i].strip()
            product = product_index.get(row[pndc_i].strip())
            candidate_row = {
                "code": normalized,
                "description": _describe(product),
            }
            existing = chosen.get(normalized)
            if existing is None or start_date >= existing[0]:
                chosen[normalized] = (start_date, candidate_row)

        rows = [entry[1] for entry in chosen.values()]

        return ParsedCorpus(
            rows=rows,
            source="FDA National Drug Code (NDC) Directory",
            source_url=NDC_SOURCE_URL,
            license="Public domain (United States Federal Government work; 17 U.S.C. 105)",
            citation=(
                "U.S. Food and Drug Administration. National Drug Code Directory, "
                "full-release text export (ndctext.zip). FDA.gov."
            ),
            # NDC is continuously updated with no discrete numbered release
            # (unlike ICD-10-CM's "FY2024" or HCPCS's "Q1 2024"); the
            # source_version/effective_date anchor to the actual pull date,
            # explicitly labeled as a snapshot rather than an FDA-issued
            # release id -- see build_codesets.py's identical caveat on the
            # seed corpus this replaces.
            source_version=f"pulled-{pulled_on.isoformat()}",
            effective_date=pulled_on.isoformat(),
        )
