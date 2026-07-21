"""Small, real-data NDC fixtures for the codeset ETL test suite.

Every row below is copied verbatim from a real ``ndctext.zip`` download
(FDA NDC Directory, pulled 2026-07-21) -- NOT the full ~11MB archive, just
enough rows to exercise the three NDC segment-width configurations
(4-4-2, 5-3-2, 5-4-1) plus one same-package-code re-listing duplicate, so
the unit suite never needs network access.
"""

from __future__ import annotations

import io
import zipfile

PRODUCT_HEADER = (
    "PRODUCTID\tPRODUCTNDC\tPRODUCTTYPENAME\tPROPRIETARYNAME\tPROPRIETARYNAMESUFFIX\t"
    "NONPROPRIETARYNAME\tDOSAGEFORMNAME\tROUTENAME\tSTARTMARKETINGDATE\tENDMARKETINGDATE\t"
    "MARKETINGCATEGORYNAME\tAPPLICATIONNUMBER\tLABELERNAME\tSUBSTANCENAME\t"
    "ACTIVE_NUMERATOR_STRENGTH\tACTIVE_INGRED_UNIT\tPHARM_CLASSES\tDEASCHEDULE\t"
    "NDC_EXCLUDE_FLAG\tLISTING_RECORD_CERTIFIED_THROUGH"
)

# (PRODUCTID, PRODUCTNDC, PROPRIETARYNAME, NONPROPRIETARYNAME, DOSAGEFORMNAME,
#  ACTIVE_NUMERATOR_STRENGTH, ACTIVE_INGRED_UNIT) -- real rows, trimmed to the
# columns the parser reads (remaining product.txt columns filled blank).
_PRODUCT_ROWS = [
    (
        "0002-0152_86a64015-7f78-4281-8b7f-579b6c3dc1f7",
        "0002-0152",
        "Zepbound",
        "tirzepatide",
        "INJECTION, SOLUTION",
        "2.5",
        "mg/.5mL",
    ),
    (
        "10014-001_3e786a6c-2517-4df1-e063-6294a90a8cc5",
        "10014-001",
        "Oxygen",
        "Oxygen",
        "GAS",
        "990",
        "mL/L",
    ),
    (
        "10018-8999_48d0d09a-35bd-4501-b481-5bb4aaaaacb2",
        "10018-8999",
        "Oxygen",
        "Oxygen",
        "GAS",
        "990",
        "mL/L",
    ),
    (
        "72043-2500_e1ccd02f-6246-493f-8ce5-a39b29b7bde7",
        "72043-2500",
        "EltaMD UV Clear SPF46",
        "Zinc oxide and Octinoxate sunscreen",
        "LOTION",
        "75; 90",
        "g/1000g; g/1000g",
    ),
]


def _product_line(row: tuple[str, ...]) -> str:
    pid, pndc, prop, nonprop, dosage, strength, unit = row
    fields = [
        pid,
        pndc,
        "HUMAN PRESCRIPTION DRUG",
        prop,
        "",
        nonprop,
        dosage,
        "",
        "20240101",
        "",
        "NDA",
        "",
        "",
        "",
        strength,
        unit,
        "",
        "",
        "N",
        "",
    ]
    return "\t".join(fields)


PACKAGE_HEADER = (
    "PRODUCTID\tPRODUCTNDC\tNDCPACKAGECODE\tPACKAGEDESCRIPTION\t"
    "STARTMARKETINGDATE\tENDMARKETINGDATE\tNDC_EXCLUDE_FLAG\tSAMPLE_PACKAGE"
)

# One package row per product above (covering all 3 real width configs),
# plus a genuine FDA-source duplicate: "72043-2500-1" listed twice with
# different STARTMARKETINGDATE values (real data -- see package.txt).
_PACKAGE_ROWS = [
    (
        "0002-0152_86a64015-7f78-4281-8b7f-579b6c3dc1f7",
        "0002-0152",
        "0002-0152-01",
        "1 VIAL, SINGLE-DOSE in 1 CARTON (0002-0152-01)",
        "20240328",
    ),
    (
        "10014-001_3e786a6c-2517-4df1-e063-6294a90a8cc5",
        "10014-001",
        "10014-001-07",
        ".21 L in 1 CYLINDER (10014-001-07)",
        "19721006",
    ),
    (
        "10018-8999_48d0d09a-35bd-4501-b481-5bb4aaaaacb2",
        "10018-8999",
        "10018-8999-1",
        "1 L in 1 TANK (10018-8999-1)",
        "20050101",
    ),
    (
        "72043-2500_e1ccd02f-6246-493f-8ce5-a39b29b7bde7",
        "72043-2500",
        "72043-2500-1",
        "48 g in 1 BOTTLE (72043-2500-1)",
        "20180110",  # earlier listing -- must lose the dedup to the later one below
    ),
    (
        "72043-2500_e1ccd02f-6246-493f-8ce5-a39b29b7bde7",
        "72043-2500",
        "72043-2500-1",
        "48 g in 1 BOTTLE (72043-2500-1)",
        "20250501",  # later re-listing of the SAME package code -- must win
    ),
]


def _package_line(row: tuple[str, ...]) -> str:
    pid, pndc, pkg, desc, start = row
    return "\t".join([pid, pndc, pkg, desc, start, "", "N", "N"])


#: Expected normalized (11-digit) codes for the 4 distinct packages above.
EXPECTED_CODES = {
    "00002015201",  # 0002-0152-01, 4-4-2 -> pad labeler+package
    "10014000107",  # 10014-001-07, 5-3-2 -> pad product
    "10018899901",  # 10018-8999-1, 5-4-1 -> pad package
    "72043250001",  # 72043-2500-1, 4-4-2 (deduplicated: 2 source rows -> 1)
}


def build_ndc_zip(
    *,
    product_rows: list[tuple[str, ...]] | None = None,
    package_rows: list[tuple[str, ...]] | None = None,
) -> bytes:
    """Build a small, real-data ndctext.zip-shaped archive for tests."""
    product_rows = _PRODUCT_ROWS if product_rows is None else product_rows
    package_rows = _PACKAGE_ROWS if package_rows is None else package_rows

    product_text = "\r\n".join([PRODUCT_HEADER, *(_product_line(r) for r in product_rows)]) + "\r\n"
    package_text = "\r\n".join([PACKAGE_HEADER, *(_package_line(r) for r in package_rows)]) + "\r\n"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("product.txt", product_text)
        zf.writestr("package.txt", package_text)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# ICD-10-CM fixtures
# ---------------------------------------------------------------------------

# Real lines copied verbatim from a live CDC NCHS icd10cm-codes-2026.txt
# download (pulled 2026-07-21), chosen to cover every chapter-range boundary
# in parsers/_icd10cm.py's chapter table (the letter-spanning II and XIX
# ranges, the alphanumeric XV boundary, the out-of-alphabetical-order XXII
# range, and the FY2026 "QA0" addition to XVII) plus one unremarkable code,
# so the unit suite never needs network access.
ICD10CM_LINES = [
    "A000    Cholera due to Vibrio cholerae 01, biovar cholerae",
    "F0150   Vascular dementia, unspecified severity, without behavioral disturbance",
    "C000    Malignant neoplasm of external upper lip",
    "D490    Neoplasm of unspecified behavior of digestive system",
    "D500    Iron deficiency anemia secondary to blood loss (chronic)",
    "O99011  Anemia complicating pregnancy, first trimester",
    "O9A111  Malignant neoplasm complicating pregnancy, first trimester",
    "QA00101 SCN2A-related neurodevelopmental disorder",
    "S0000XA Unspecified superficial injury of scalp, initial encounter",
    "T880XXA Infection following immunization, initial encounter",
    "V0001XA Pedestrian on foot injured in collision with roller-skater, initial encounter",
    "Y990    Civilian activity done for income or pay",
    "Z0000   Encounter for general adult medical examination without abnormal findings",
    "U070    Vaping-related disorder",
]

#: Expected code -> chapter Roman numeral for every line in ICD10CM_LINES.
ICD10CM_EXPECTED_CHAPTERS = {
    "A000": "I",
    "F0150": "V",
    "C000": "II",
    "D490": "II",
    "D500": "III",
    "O99011": "XV",
    "O9A111": "XV",
    "QA00101": "XVII",
    "S0000XA": "XIX",
    "T880XXA": "XIX",
    "V0001XA": "XX",
    "Y990": "XX",
    "Z0000": "XXI",
    "U070": "XXII",
}


def build_icd10cm_zip(
    lines: list[str] | None = None,
    *,
    member: str = "icd10cm-codes-2026.txt",
) -> bytes:
    """Build a small ``icd10cm-Code Descriptions-*.zip``-shaped archive for tests."""
    lines = ICD10CM_LINES if lines is None else lines
    text = "\r\n".join(lines) + "\r\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, text)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# HCPCS fixtures
# ---------------------------------------------------------------------------

# Lines below reproduce (trimmed to the columns the parser reads) real rows
# from a live CMS HCPCS Level II 2026Q3 (July) ANWEB download, pulled
# 2026-07-21 -- see parsers/_hcpcs.py's module docstring for the verified
# column layout. Real RIC='3' first lines are right-padded to a fixed 80-char
# long-description field (later detail fields sit at fixed offsets after
# it); real RIC='4' continuation lines are NOT re-padded by the export, which
# is exactly the case that breaks a naive "concatenate the raw slices" join
# (see _HCPCS_MULTILINE below) -- so the two line shapes are deliberately
# reproduced here, not both padded for tidiness.


def _hcpcs_first_line(code: str, seq: str, long_desc: str, short_desc: str = "") -> str:
    return code + seq + "3" + long_desc.ljust(80) + short_desc.ljust(28)


def _hcpcs_cont_line(code: str, seq: str, long_desc_chunk: str) -> str:
    # Real continuation lines are right-trimmed by the export -- no padding.
    return code + seq + "4" + long_desc_chunk


def _hcpcs_modifier_line(code: str, seq: str, long_desc: str) -> str:
    # A modifier's 2-char code occupies cols 4-5; cols 1-3 are FILLER
    # (blank) -- see module docstring's COBOL-REDEFINES explanation.
    return "   " + code + seq + "7" + long_desc.ljust(80)


# Single-line procedure codes across several letter prefixes.
_HCPCS_SINGLE_LINE: dict[str, str] = {
    "G0008": "Administration of influenza virus vaccine",
    "J0013": "Esketamine, nasal spray, 1 mg",
    "V2020": "Frames, purchases",
}

# A0080: real 2-line record. The first line's long-desc field is padded to
# 80 (so the natural word-break space survives the raw slice); the single
# continuation line is NOT padded (real export behavior) -- this exercises
# the per-chunk-strip-then-join-with-a-single-space logic on its simplest
# case (exactly one join boundary).
_HCPCS_A0080_LINES = [
    _hcpcs_first_line(
        "A0080",
        "00100",
        "Non-emergency transportation, per mile - vehicle provided by volunteer",
    ),
    _hcpcs_cont_line("A0080", "00200", "(individual or organization), with no vested interest"),
]

# A0384: real 3-line record, chosen specifically because its middle
# continuation chunk ends mid-sentence ("...defibrillation is") with NO
# trailing pad in the real export -- naively concatenating raw slices here
# collapses the word boundary into "ispermitted". This is the case that
# caught the bug during development; kept as its own fixture so a
# regression on the join logic fails loudly.
_HCPCS_A0384_LINES = [
    _hcpcs_first_line(
        "A0384",
        "00100",
        "Bls specialized service disposable supplies; defibrillation (used by als",
    ),
    _hcpcs_cont_line(
        "A0384", "00200", "ambulances and bls ambulances in jurisdictions where defibrillation is"
    ),
    _hcpcs_cont_line("A0384", "00300", "permitted in bls ambulances)"),
]

#: Expected code -> joined long description for every procedure record
#: `build_hcpcs_zip()` emits (modifier records are deliberately excluded).
HCPCS_EXPECTED_DESCRIPTIONS: dict[str, str] = {
    **_HCPCS_SINGLE_LINE,
    "A0080": (
        "Non-emergency transportation, per mile - vehicle provided by volunteer "
        "(individual or organization), with no vested interest"
    ),
    "A0384": (
        "Bls specialized service disposable supplies; defibrillation (used by als "
        "ambulances and bls ambulances in jurisdictions where defibrillation is "
        "permitted in bls ambulances)"
    ),
}


def build_hcpcs_zip(
    *,
    extra_lines: list[str] | None = None,
    member: str = "HCPC2026_JUL_ANWEB_06172026.txt",
    include_modifier: bool = True,
) -> bytes:
    """Build a small ``hcpc*_anweb_*.zip``-shaped archive for tests.

    Includes every procedure record in HCPCS_EXPECTED_DESCRIPTIONS plus, by
    default, one real modifier record (RIC='7', code "A1") to exercise the
    modifier-skip path -- callers that pass `extra_lines` replace nothing,
    they only append (used for the malformed/edge-case tests).
    """
    lines = [_hcpcs_first_line(code, "00100", desc) for code, desc in _HCPCS_SINGLE_LINE.items()]
    lines += _HCPCS_A0080_LINES
    lines += _HCPCS_A0384_LINES
    if include_modifier:
        lines.append(_hcpcs_modifier_line("A1", "00100", "Dressing for one wound"))
    if extra_lines:
        lines += extra_lines

    text = "\r\n".join(lines) + "\r\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, text)
        # A second, real non-ANWEB member (the bundled record-layout doc) --
        # present in every real download alongside the ANWEB export, so the
        # member-lookup test fixture matches what a real zip actually
        # contains, not an idealized single-file archive.
        zf.writestr("HCPC2026_recordlayout.txt", "not the codes file\r\n")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# MS-DRG fixtures
# ---------------------------------------------------------------------------

# Verbatim rows from the real CMS ICD-10 MS-DRG Definitions Manual v43.0
# Appendix A (pulled 2026-07-21) -- see parsers/_msdrg.py's module docstring
# for the verified fixed-width column layout. Kept as raw strings (not built
# field-by-field) since the exact fixed-width padding is the thing under
# test: offsets [0:3] code, [4:6] MDC, [8:9] type, [11:] description.

# Banner/prose/header lines that precede the data in a real download -- none
# starts with 3 digits, so the parser's `line[0:3].isdigit()` filter skips
# every one without any separate header-detection logic.
MSDRG_BANNER_LINES = [
    "ICD-10 MS-DRG DEFINITIONS MANUAL VERSION 43.0",
    "",
    "Appendix A - List of MS-DRGs, Diagnosis Codes and Procedure Codes",
    "",
    "DRG MDC MS Description",
]

# Surgical row with an MDC.
_MSDRG_001 = "001     P  Heart Transplant or Implant of Heart Assist System with MCC"
# Surgical row with an MDC (two-digit MDC field populated, unlike 001/014).
_MSDRG_020 = (
    "020 01  P  Intracranial Vascular Procedures with Principal Diagnosis Hemorrhage with MCC"
)
# Medical row with an MDC.
_MSDRG_014 = "014     M  Allogeneic Bone Marrow Transplant"
# The two ungroupable rows: blank MDC AND blank type.
_MSDRG_998 = "998        Principal Diagnosis Invalid as Discharge Diagnosis"
_MSDRG_999 = "999        Ungroupable"

MSDRG_DATA_LINES = [_MSDRG_001, _MSDRG_014, _MSDRG_020, _MSDRG_998, _MSDRG_999]

#: Expected code -> description for every row `build_msdrg_zip()` emits.
MSDRG_EXPECTED_DESCRIPTIONS: dict[str, str] = {
    "001": "Heart Transplant or Implant of Heart Assist System with MCC",
    "014": "Allogeneic Bone Marrow Transplant",
    "020": "Intracranial Vascular Procedures with Principal Diagnosis Hemorrhage with MCC",
    "998": "Principal Diagnosis Invalid as Discharge Diagnosis",
    "999": "Ungroupable",
}


def build_msdrg_zip(
    *,
    extra_lines: list[str] | None = None,
    member: str = "appendix_A.txt",
) -> bytes:
    """Build a small MS-DRG Definitions Manual zip archive for tests.

    Includes the banner/header lines that must be skipped plus every row in
    MSDRG_EXPECTED_DESCRIPTIONS -- callers that pass `extra_lines` only
    append (used for the malformed/edge-case tests), never replace.
    """
    lines = list(MSDRG_BANNER_LINES) + list(MSDRG_DATA_LINES)
    if extra_lines:
        lines += extra_lines

    text = "\r\n".join(lines) + "\r\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(member, text)
        # A second, real non-Appendix-A member (an MDC definitions file) --
        # present in every real download alongside Appendix A, so the
        # member-lookup test fixture matches what a real zip actually
        # contains, not an idealized single-file archive.
        zf.writestr("MDC_01_definitions.txt", "not the appendix A file\r\n")
    return buf.getvalue()
