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
